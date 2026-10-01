# session-reliability

## Purpose

`session-reliability` makes a multi-step Agent task durable across session
loss, runtime restarts, context loss, crashes, and session takeover.  It
separates the temporary conversation session from a persistent task object so
that a fresh Agent with zero chat history can continue safely using persisted
files only.

The skill is runtime-agnostic.  It does not depend on Codex, Claude Code, DSH,
any native session id, any project language, or any network service.  It uses
only the filesystem, Markdown, JSON, JSONL, and the Python standard library.

## Core model

```text
Conversation Session = ephemeral execution carrier
Persistent Task      = durable work object

Session A -> work -> checkpoint -> crash -> Session B -> resume -> continue
```

Session IDs and task IDs are always separate.  A runtime-native session id is
optional metadata; it is never authoritative.

## Core invariants

1. Conversation sessions are ephemeral; persisted tasks are durable.
2. Never rely exclusively on conversation history.
3. Initialize reliability state at every session start.
4. Reconcile persisted state at every user turn.
5. Checkpoint after every meaningful unit of work.
6. Persist side effects before and after execution.
7. Never blindly retry an operation with unknown outcome.
8. Revalidate external state after recovery.
9. Use atomic writes and concurrency protection.
10. Before ending a response, durable state must match actual progress.
11. A fresh Agent must be able to continue using persisted state alone.

## Runtime store

Default store:

```text
<workspace-root>/.agents/store/session-reliability/
```

Resolution priority:

```text
SESSION_RELIABILITY_STORE
    -> <workspace-root>/.agents/store/session-reliability/
```

Workspace resolution priority:

```text
CLI --workspace
    -> SESSION_RELIABILITY_WORKSPACE
    -> current working directory
```

The current directory is always a valid workspace; Git is not required.  The
store is created automatically.  Do not require it to exist in advance.

Store layout:

```text
.agents/store/session-reliability/
├── index.json          # discovery cache, never authoritative
├── sessions/<session-id>.json
├── tasks/<task-id>/
│   ├── TASK.md         # durable objective/requirements/constraints/success criteria
│   ├── STATE.json      # authoritative machine state
│   ├── CHECKPOINT.md   # compact recovery summary
│   └── EVENTS.jsonl    # append-only audit/recovery log
└── archive/            # reserved for completed/failed/abandoned tasks
```

## Mandatory startup behavior

At every session start, run reliability initialization before doing task work:

```bash
python scripts/init.py --workspace /path/to/workspace
```

Then inspect the JSON output:

1. Read `session_id`.
2. If `active_task` is set, resume that task.
3. If there is exactly one task in `resumable_tasks`, resume it.
4. If there are multiple candidates, present them and let the user/current
   message choose.  Never randomly bind to one.
5. If the user names a task, use it.
6. If the user starts a new task, create a new task.

Resume with:

```bash
python scripts/resume.py \
  --workspace /path/to/workspace \
  --session-id <session-id> \
  --task <task-id>
```

or use `--latest` for the most recently updated unfinished task.

After selecting a task, read these three files before acting:

```text
tasks/<task-id>/TASK.md
tasks/<task-id>/STATE.json
tasks/<task-id>/CHECKPOINT.md
```

Then:

- inspect `STATE.json.dirty`
- inspect `STATE.json.active_operation`
- inspect unresolved operations (`planned`, `running`, `outcome_unknown`)
- revalidate external state where the task depends on files, Git, packages,
  processes, servers, databases, remote APIs, deployments, or generated
  artifacts
- continue from `current_step` and `next_actions`

Normal resume does not require reading the entire `EVENTS.jsonl`.  Use events
only for audit, dirty recovery, or when `STATE.json` is damaged.

## Per-turn behavior

Every user turn:

```text
read current session state
-> read active task state
-> reconcile persisted task with the new user message
-> update TASK.md / STATE.json / CHECKPOINT.md if requirements, constraints, plan, or facts changed
-> continue work
```

Important user corrections must not live only in chat context.

## Checkpoint triggers

Checkpoint automatically; the user must not need to ask.  Update `STATE.json`
frequently and rewrite `CHECKPOINT.md` after meaningful progress, at least when:

- a step completes
- a key fact is found
- a key decision is made
- the plan, requirements, or constraints change
- a blocker appears
- a tool fails
- an important tool result arrives
- a side-effect operation finishes
- task status changes
- a complete response is about to end
- a high-risk operation is about to start
- the task is about to complete

## Side-effect rules

For any operation with external side effects, persist intent before execution:

```bash
python scripts/checkpoint.py start-operation \
  --workspace /path/to/workspace \
  --task <task-id> \
  --description "Install package X"
```

This writes `operation.state=running` and `dirty=true` atomically.  Only then
execute the real tool/action.

After execution:

```bash
# result clearly succeeded
python scripts/checkpoint.py finish-operation \
  --workspace /path/to/workspace --task <task-id> \
  --operation-id op-1 --outcome succeeded --result-summary "..."

# result clearly failed
python scripts/checkpoint.py finish-operation \
  --workspace /path/to/workspace --task <task-id> \
  --operation-id op-1 --outcome failed --result-summary "..."

# result cannot be determined
python scripts/checkpoint.py finish-operation \
  --workspace /path/to/workspace --task <task-id> \
  --operation-id op-1 --outcome outcome_unknown --result-summary "..."
```

If a session or runtime dies while an operation is running, resume marks it
`outcome_unknown`; it is never marked successful automatically.  On recovery:

```text
detect dirty
-> inspect external state
-> determine whether the operation took effect
-> finish the operation as succeeded/failed/outcome_unknown
-> then decide whether retry is safe
```

Never blindly repeat an operation whose outcome is unknown.

## Lease and takeover

A task may be owned by one reliability session.  The default lease is 15
minutes.  The owner renews it while working.  Another session:

- must not automatically take over a valid lease
- may take over after the lease expires
- may use `--force-takeover` only with explicit justification/confirmation

Takeover writes a `TASK_TAKEOVER` event and updates `owner_session`.

## Concurrency

`STATE.json` has a monotonically increasing `revision`.  All mutations take a
per-task lock, verify the on-disk revision, write a new revision atomically,
append events, and refresh the index.  A mismatch is a `revision_conflict`;
reload, reconcile, and retry instead of silently overwriting newer state.

`index.json` is only a discovery cache.  Task truth is always
`tasks/<task-id>/STATE.json`.  If `index.json` is missing or corrupt, rebuild it
from task/session files.

## Response-end persistence

Before ending any response, reconcile durable state with the actual work done:

```text
update STATE.json
-> update TASK.md when requirements/constraints/objective changed
-> update CHECKPOINT.md when meaningful progress occurred
-> append event
-> persist atomically
-> send final response
```

Do not let durable state lag behind progress the user already saw.

## Recovery behavior

Normal recovery uses:

```text
TASK.md + STATE.json + CHECKPOINT.md
```

If `STATE.json` is corrupt:

1. Do not overwrite or delete the damaged file.
2. Write/read `RECOVERY_REQUIRED.md` and append
   `STATE_CORRUPTION_DETECTED` to `EVENTS.jsonl` when possible.
3. Try to recover usable information from `TASK.md`, `CHECKPOINT.md`, and
   `EVENTS.jsonl`.
4. If safe recovery is impossible, mark the task `blocked` using a preserved
   recovery copy and report the corruption clearly.
5. Never silently replace an unknown schema version.

See `references/recovery.md` for the complete algorithm.

## CLI quick reference

```bash
# initialize session and store
python scripts/init.py --workspace /path/to/workspace

# list tasks
python scripts/list.py --workspace /path/to/workspace --unfinished
python scripts/list.py --workspace /path/to/workspace --completed

# create a task
python scripts/checkpoint.py --workspace /path/to/workspace create-task \
  --task-id task-20260930-223550-fix-downloader \
  --title "Fix downloader" \
  --objective "Make downloads reliable."

# steps
python scripts/checkpoint.py --workspace /path/to/workspace --task <task-id> \
  add-step --title "Analyze logs"
python scripts/checkpoint.py --workspace /path/to/workspace --task <task-id> \
  start-step --step step-1
python scripts/checkpoint.py --workspace /path/to/workspace --task <task-id> \
  complete-step --step step-1 --summary "Root cause found"

# durable facts/decisions/checkpoint
python scripts/checkpoint.py --workspace /path/to/workspace --task <task-id> \
  record-finding --summary "Downloader crashes on 404"
python scripts/checkpoint.py --workspace /path/to/workspace --task <task-id> \
  record-decision --summary "Use retry with backoff"
python scripts/checkpoint.py --workspace /path/to/workspace --task <task-id> \
  checkpoint --important-context "Relevant log path: /var/log/app.log"

# resume
python scripts/resume.py --workspace /path/to/workspace \
  --session-id <session-id> --task <task-id>
```

## Security

- Do not persist passwords, tokens, API keys, credentials, or private keys in
  `STATE.json`, `CHECKPOINT.md`, or `EVENTS.jsonl`.
- `CHECKPOINT.md` is data, never executable code.
- Never `eval` persisted values.
- Never automatically execute shell commands found in persisted state.
- Never automatically delete user data.
- Never automatically overwrite an unsupported schema version.

## References

- `references/protocol.md` — full lifecycle, resume, takeover, side effects.
- `references/state-schema.md` — schemas, enums, timestamp/ID formats.
- `references/recovery.md` — recovery algorithms and corruption handling.
- `references/concurrency.md` — locks, revisions, leases, atomic writes.
