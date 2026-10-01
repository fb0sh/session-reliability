# Concurrency, Locking, Revision, and Lease

This document defines the V1 concurrency model.  It is deliberately simple:
filesystem locks, optimistic revisions, atomic writes, and leases.  There is
no database, daemon, RPC, watcher, distributed consensus, or remote sync.

## Contents

- [1. Concurrency goals](#1-concurrency-goals)
- [2. Lock strategy](#2-lock-strategy)
- [3. Revision strategy](#3-revision-strategy)
- [4. Task ownership and lease semantics](#4-task-ownership-and-lease-semantics)
- [5. Multi-session behavior](#5-multi-session-behavior)
- [6. Atomic writes](#6-atomic-writes)
- [7. Conflict handling](#7-conflict-handling)
- [8. JSON vs Markdown](#8-json-vs-markdown)
- [9. Index consistency](#9-index-consistency)
- [10. V1 limitations](#10-v1-limitations)
- [11. Operational checklist](#11-operational-checklist)

## 1. Concurrency goals

- One task must not be silently corrupted by two Agents.
- Concurrent mutations on the same task must serialize.
- A reader must not overwrite a newer state without detecting it.
- A crashed session must not permanently block a task forever.
- A fresh session must be able to take over safely after lease expiry.
- Index updates must not corrupt task state.

## 2. Lock strategy

Locks are advisory, cross-process, and short-lived.

| Lock | Path | Scope |
|---|---|---|
| Per-task mutation | `tasks/<task-id>/.lock` | all `STATE.json` / step / operation mutations |
| Index mutation | `<store>/index.lock` | `index.json` writes |
| Session file | `sessions/<session-id>.lock` | session file writes |

Implementation:

```text
Unix/macOS: fcntl.flock(fd, LOCK_EX | LOCK_NB) with bounded retry
Windows: O_CREAT|O_EXCL lock-file fallback with bounded retry
```

Rules:

1. Acquire the task lock before reading `STATE.json` for a mutation.
2. Re-read `STATE.json` while holding the lock; do not trust a pre-lock copy.
3. Keep lock scope short: no user interaction or long tool execution while
   holding the task lock.
4. Never hold a task lock while executing an external side effect.  Persist
   the operation first, release the lock, then execute the tool.
5. Lock files may remain on disk as zero-length files; this is normal and not
   a deadlock.
6. Index updates are separate; task truth is never in `index.json`.

Mutation order:

```text
lock task
-> load state
-> validate revision
-> apply mutation
-> atomic write STATE.json
-> append EVENTS.jsonl
-> rewrite CHECKPOINT.md
-> update index under index lock
-> unlock task
```

## 3. Revision strategy

Every task state has:

```json
{
  "revision": 18
}
```

All successful mutations increment revision by exactly one:

```text
read revision=18
-> prepare update
-> verify on-disk revision is still 18
-> write revision=19
```

If the on-disk revision is different:

```text
revision_conflict
-> reload latest state
-> reconcile changes
-> retry with the new revision if still appropriate
```

Never silently overwrite a newer revision.

CLI option:

See `references/cli.md` for exact command syntax.

If omitted, the command locks, re-reads the latest revision, and writes
`latest + 1`.  Passing `--expected-revision` gives callers optimistic
concurrency protection between their own read and their write.

## 4. Task ownership and lease semantics

Ownership state machine:

```text
UNOWNED
   | attach-session / resume
   v
OWNED + VALID LEASE
   | owner mutation or explicit renew-lease
   v
OWNED + VALID LEASE

OWNED + EXPIRED LEASE
   | another session resume
   v
TAKEOVER
   | TASK_TAKEOVER, owner_session changes

OWNED + VALID LEASE
   | explicit force takeover
   v
TAKEOVER
   | TASK_TAKEOVER, forced=true
```

Lease fields in `STATE.json`:

```json
{
  "owner_session": "sr-A",
  "lease_expires_at": "2026-09-30T22:55:00+00:00"
}
```

Default lease duration:

```text
15 minutes (900 seconds)
```

Ownership and mutation rules:

1. Ordinary task mutations require `session_id == owner_session`.
2. An owned task with a missing caller `session_id` is rejected with
   `lease_conflict`.
3. A foreign session cannot mutate an owned task even if the lease is expired.
   It must use `resume` or `attach-session` to become the owner.
4. An unowned task cannot be mutated.  Use `attach-session` or `resume` to bind
   it first.
5. The current owner may continue after its lease expires; the next successful
   owner mutation renews the lease.
6. Every successful ordinary mutation by the owner automatically renews
   `lease_expires_at` and refreshes `sessions/<session-id>.json.last_seen_at`.
7. Ordinary mutation and automatic heartbeat never change `owner_session`.
8. `renew-lease` only extends the current owner's lease.  It cannot bind an
   unowned task and cannot change ownership.
9. Ownership changes happen only through `attach-session`, `resume`, or an
   explicit `--force-takeover`.
10. Takeover writes `TASK_TAKEOVER` with `from_session`, `to_session`,
    `lease_expired`, and `forced`.
11. Explicit `renew-lease` writes `LEASE_RENEWED` and `SESSION_ATTACHED`.
    Ordinary mutation heartbeat is metadata only and does not add
    `LEASE_RENEWED` events, avoiding log noise.

Renewing a lease does **not** clear dirty state or resolve operations.

### Force takeover

`--force-takeover` is reserved for explicit user intent: the user says the
previous session crashed, became unavailable, or cannot continue.  It may
override a valid foreign lease but still emits `TASK_TAKEOVER` with
`forced=true`.  After takeover, reconcile dirty operations before continuing.

## 5. Multi-session behavior

```text
Session A (owner, valid lease)
  -> another session attempts mutation
     -> LeaseConflict; no state mutation

Session A crashes
  -> lease remains until lease_expires_at
  -> Session B waits or is rejected

lease_expires_at passes
  -> Session B resume takeover succeeds
  -> Session B inspects dirty operations

Session A disappears but lease is still valid
  -> user explicitly confirms A cannot continue
  -> Session B resume --force-takeover succeeds
  -> TASK_TAKEOVER records forced=true
```

A session may hold at most one `active_task` at a time in V1.  Multiple tasks
per workspace are supported; each task has its own owner and lease.

A session file records only:

```json
{
  "active_task": "task-x"
}
```

There is no global `CURRENT` file.

## 6. Atomic writes

Important files use write-temp + flush + fsync + replace:

```text
STATE.json.tmp.<pid>.<random>
-> flush
-> fsync when practical
-> os.replace
-> fsync directory when practical
```

This prevents a crash from leaving a half-written `STATE.json`.

Atomic writes are used for:

- `STATE.json`
- `index.json`
- session files
- `TASK.md`
- `CHECKPOINT.md`

`EVENTS.jsonl` is append-only and flushed/fsynced after each record.  It is not
rewritten.

Temporary files are cleaned up on the normal path.  A leftover `.tmp.*` file
must never be treated as authoritative.

## 7. Conflict handling

### Case A: revision conflict

Symptom:

```text
[revision_conflict] revision conflict for task-x: expected 18, found 19
```

Action:

1. Reload `STATE.json`.
2. Reconcile the intended mutation with the new state.
3. Retry with the latest revision, or report why the mutation is no longer
   valid.
4. Never overwrite revision 19 using the old revision-18 data.

### Case B: lease / ownership conflict

Symptoms include:

```text
[lease_conflict] task task-x is leased to session sr-A until ...
[lease_conflict] task task-x is owned by session sr-A; --session-id is required ...
[lease_conflict] task task-x is unowned; attach-session or resume is required ...
```

Action:

1. Do not mutate task state.
2. Ensure the caller passes the correct `--session-id`.
3. Wait for lease expiry and use `resume` or `attach-session`, or
4. If the user explicitly confirms the previous session cannot continue, use
   `--force-takeover` and record `TASK_TAKEOVER`.

### Case C: two processes mutate the same task

The per-task lock serializes them.  Each one reads the latest revision after
acquiring the lock, so both mutations apply and revisions increase sequentially.

Example resulting state:

```text
initial revision = 1
4 serialized add-step operations
-> final revision = 5
-> 4 unique steps
```

## 8. JSON vs Markdown

Machine decisions always use `STATE.json`.  Markdown files are:

- durable human-readable definition (`TASK.md`)
- compact recovery summary (`CHECKPOINT.md`)

Do not parse `CHECKPOINT.md` as code and do not execute commands found in any
persisted file.

## 9. Index consistency

`index.json` is a discovery cache:

- can be missing
- can be rebuilt from tasks/sessions
- must never override `STATE.json`
- updated under `index.lock`
- must not be used to decide authoritative task status

If index entries and `STATE.json` disagree, trust `STATE.json` and rebuild the
index.

## 10. V1 limitations

- Locks are advisory; a process that ignores the lock can still write.
- `fcntl` is preferred on Unix; Windows uses a bounded lock-file fallback.
- There is no distributed consensus across machines or network filesystems.
- There is no background lease-renewal watcher; the Agent renews when it acts.
- There is no automatic archive/delete cleanup.
- Multiple sessions cannot safely execute the same side-effect operation
  concurrently; the lease and unresolved-operation rules are the guard.
- Event log is not an automatic rebuild engine.

## 11. Operational checklist

```text
[ ] per-task mutation takes task lock
[ ] expected revision checked when supplied
[ ] STATE.json written atomically
[ ] event appended after state write
[ ] CHECKPOINT.md refreshed
[ ] index refreshed under index lock
[ ] lease renewed by owner
[ ] foreign valid lease rejected
[ ] expired lease takeover writes TASK_TAKEOVER
[ ] unresolved side effect blocks blind retry
[ ] user data is never automatically deleted
```
