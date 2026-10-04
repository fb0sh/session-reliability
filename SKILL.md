---
name: session-reliability
description: >-
  Durable task tracking for non-trivial, multi-step project work. Use
  proactively for any software project, feature, bug fix, refactor, migration,
  investigation, or automation that touches files, runs commands or tools,
  modifies external state, spans multiple turns, or may be interrupted. Also
  use when starting or continuing multi-step work, and when the user asks to
  continue, resume, recover, pick up, or check progress on prior work — meaning
  unfinished work from an earlier session, not a conversational thread. Do not
  wait for the user to explicitly ask for persistence. Not for a single one-off
  file conversion, a one-line question, or small talk.
compatibility: Requires filesystem read/write access and Python 3.10+.
---

# Session Reliability

Keep durable task progress outside conversation history so another session can
continue interrupted work safely.

## Skill and workspace roots

Treat the directory containing the loaded `SKILL.md` as `SKILL_ROOT`.

Treat the user's current work directory as `WORKSPACE_ROOT`.

Bundled scripts and assets are read from `SKILL_ROOT`. Runtime task state is
written under `WORKSPACE_ROOT`:

```text
<WORKSPACE_ROOT>/.agents/store/session-reliability/
```

unless `SESSION_RELIABILITY_STORE` overrides it.

Do not assume `SKILL_ROOT` and `WORKSPACE_ROOT` are the same directory. Resolve
bundled script paths from the skill installation, and pass the workspace
explicitly when invoking them.

## Start or resume

When this skill activates for a workspace:

1. Run the bundled `scripts/init.py`, passing the workspace explicitly.
2. Inspect the returned active task and resumable tasks.
3. Resume the current task when it matches the user's work.
4. If multiple resumable tasks match, present candidates and let the user
   message decide; do not randomly bind to one.
5. Keep the reliability `session_id` returned by initialization for the lifetime
   of the current conversation and pass it to subsequent task mutations.
6. Create a durable task before beginning substantial new multi-step work.
7. For recovered work, read `TASK.md`, `STATE.json`, and `CHECKPOINT.md`.
8. If `dirty`, `active_operation`, or unresolved state exists, follow
   `references/recovery.md` before continuing.

Read `references/cli.md` for exact bundled-script invocations.

## During work

Treat conversation context as transient and persisted task state as durable.

Use the bundled state-management scripts instead of editing runtime JSON by
hand whenever possible.

Checkpoint after meaningful progress such as:

- completing, starting, failing, or blocking a step
- discovering a fact that affects later work
- making an important decision
- changing requirements, constraints, or the plan
- encountering a blocker or important tool failure
- completing a side-effecting operation

Keep `current_step` and `next_actions` synchronized with actual work. Persist
important user corrections so they survive the current conversation.

Mark a step `in_progress` before you begin it, and complete it when it is done.
Completing a step that was never started leaves its start time empty, so an
interruption during that window resumes from an earlier step than the work
actually reached.

## Side effects

A side effect is an action that changes state outside the conversation and is
not safely repeatable: running a non-idempotent script, publishing or sending
something, installing, deleting, mutating remote state, or starting and
stopping a service. Editing the files you are working on is ordinary step work,
not a side effect — recording every write costs more than it saves, and the
step record already covers it.

Record the operation *before* you act. After a crash, nobody can tell from the
workspace alone whether the action ran; a persisted `running` operation is what
lets a later session stop and inspect instead of guessing, and a wrong guess on
a non-idempotent action duplicates its effect.

Before an external side effect, persist the operation as running and mark the
task dirty.

After the result is known, record success or failure and clear dirty state.

If the outcome is uncertain, record `outcome_unknown`. Revalidate external
state before deciding whether retry is safe.

Never blindly retry an operation whose outcome is unknown. Read
`references/recovery.md` for interrupted-operation handling.

## Before finishing a turn

Before sending a response after meaningful task work:

1. Reconcile persisted state with work actually completed.
2. Update current step and next actions.
3. Refresh the recovery checkpoint when meaningful progress changed.
4. Ensure progress already described to the user is durable.
5. Append an event for significant lifecycle or side-effect changes.

Do not leave durable state behind the progress the user has already seen.

## Recovery

Normal recovery reads:

```text
TASK.md + STATE.json + CHECKPOINT.md
```

`EVENTS.jsonl` is read only for dirty recovery, state corruption, or audit.

If `STATE.json` is corrupt, preserve the original file, use the recovery
sidecar and event tail when available, and report the corruption clearly. If
safe recovery is impossible, block the task rather than inventing state.

Read `references/recovery.md` for normal, dirty, uncertain-outcome, and
corruption recovery.

## Multiple sessions

Keep session identity separate from task identity. Do not depend on a
runtime-native session id.

Honor task leases and revision checks. Do not silently overwrite a newer task
revision. A session may take over an expired lease and must emit a takeover
event.

Only the current owner may mutate a task while its lease is valid.  Successful
owner mutations renew the lease automatically using the task's persisted
`lease_duration_seconds` (default 900).  An unowned task must be bound through
attach/resume before mutation.

If the user confirms the previous session failed, became unavailable, or
cannot continue, use explicit force takeover even when its lease is still
valid.  Task takeover transfers task ownership only; it does not imply that the
previous reliability session itself is invalid.  Reconcile dirty and uncertain
external operations before continuing.

Read `references/concurrency.md` for lease, takeover, lock, revision, and
multi-session rules.  Read `references/recovery.md` for crash recovery.

## References

Load only what the current situation requires:

- `references/protocol.md` — task/session lifecycle, checkpoint semantics, and event roles.
- `references/cli.md` — exact bundled script commands, flags, and output behavior.
- `references/recovery.md` — resume, dirty operations, uncertain outcomes, and corruption.
- `references/concurrency.md` — leases, takeover, locks, revisions, and atomic writes.
- `references/state-schema.md` — persisted schemas, enums, IDs, and timestamps.

## Invariants

- Persistent task state must survive conversation loss.
- Conversation history cannot be the sole record of meaningful progress.
- Persisted state must reflect progress already reported to the user.
- Record external side effects before and after execution.
- Inspect uncertain side effects before retrying them.
- Keep session identity separate from task identity.
- Keep recovery possible without the previous conversation.
- Do not persist secrets or execute persisted text as commands.
