# Session Reliability Protocol

This document defines the V1 lifecycle rules implemented by `session-reliability`.

## 1. Entity relationship

```text
Session (sessions/<session-id>.json)
   |
   +-- active_task (nullable)
             |
             v
Task (tasks/<task-id>/)
   +-- TASK.md          durable definition
   +-- STATE.json       authoritative current state
   +-- CHECKPOINT.md    compact recovery summary
   +-- EVENTS.jsonl     append-only audit/recovery log
          +-- Steps
          +-- Operations
          +-- Facts / Decisions / Blockers
```

Session IDs and task IDs are separate.  `native_session_id` is optional
metadata and must not be required by any logic.

## 2. Workspace and store resolution

```text
resolve workspace root:
  1. CLI --workspace
  2. SESSION_RELIABILITY_WORKSPACE
  3. cwd

resolve store:
  1. SESSION_RELIABILITY_STORE
  2. <workspace-root>/.agents/store/session-reliability/
```

`init.py` creates the store, index, `sessions/`, `tasks/`, and `archive/` when
needed.

## 3. Session lifecycle

```mermaid
flowchart LR
    A[new process/session start] --> B[run init.py]
    B --> C{session id supplied?}
    C -- no --> D[generate sr-YYYYMMDD-HHMMSS-xxxx]
    C -- yes --> E[load existing session or create missing]
    D --> F[write sessions/session-id.json]
    E --> F
    F --> G[touch last_seen_at]
    G --> H[inspect unfinished tasks]
    H --> I[session may attach to a task]
    I --> J[active]
    J --> K[lease expires / runtime closes]
    K --> L[idle / closed / expired]
```

Commands:

```bash
python scripts/init.py --workspace /work
python scripts/init.py --workspace /work --session-id sr-20260930-223501-a7f3
python scripts/init.py --workspace /work --native-session-id runtime-123
```

`init.py` never randomly binds a task when multiple unfinished tasks exist.

## 4. Task lifecycle

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> in_progress
    pending --> paused
    pending --> blocked
    in_progress --> paused
    in_progress --> blocked
    in_progress --> completed
    in_progress --> failed
    paused --> in_progress
    blocked --> in_progress
    failed --> in_progress
    pending --> abandoned
    paused --> abandoned
    blocked --> abandoned
    completed --> [*]
    failed --> [*]
    abandoned --> [*]
```

Allowed statuses:

```text
pending
in_progress
blocked
paused
completed
failed
abandoned
```

Task creation:

```bash
python scripts/checkpoint.py --workspace /work create-task \
  --task-id task-20260930-223550-fix-downloader \
  --title "Fix downloader" \
  --objective "Make the downloader reliable." \
  --requirement "Handle 404 responses." \
  --constraint "Use Python stdlib only." \
  --success-criterion "Two sessions complete the task safely."
```

Completion:

```bash
python scripts/checkpoint.py --workspace /work --task task-20260930-223550-fix-downloader \
  set-status --status completed
```

Completion is refused while `dirty=true` or unresolved operations exist.

## 5. User turn lifecycle

```mermaid
sequenceDiagram
    participant U as User
    participant A as Agent
    participant S as Store
    U->>A: new message
    A->>S: read active session file
    A->>S: read active STATE.json / TASK.md / CHECKPOINT.md
    A->>A: reconcile persisted state with message
    alt requirements/constraints/objective changed
        A->>S: update TASK.md + STATE.json + event
    end
    alt plan/progress changed
        A->>S: mutate STATE.json + CHECKPOINT.md + event
    end
    A->>S: perform work
    A->>S: persist progress before final response
    A->>U: final response
```

Commands used during a turn:

```bash
# add/start/complete work
python scripts/checkpoint.py --workspace /work --task <id> add-step --title "Analyze logs"
python scripts/checkpoint.py --workspace /work --task <id> start-step --step step-1
python scripts/checkpoint.py --workspace /work --task <id> complete-step --step step-1 --summary "..."

# update durable definition
python scripts/checkpoint.py --workspace /work --task <id> update-requirements \
  --requirement "New requirement." --constraint "New constraint."

# record facts, decisions, next actions
python scripts/checkpoint.py --workspace /work --task <id> record-finding --summary "..."
python scripts/checkpoint.py --workspace /work --task <id> record-decision --summary "..."
python scripts/checkpoint.py --workspace /work --task <id> set-next-actions --action "..."
```

## 6. Session start and task selection

```text
run init.py
  |
  +-- session.active_task exists?
  |       -> resume that task
  |
  +-- exactly one resumable task?
  |       -> resume it
  |
  +-- multiple resumable tasks?
  |       -> present candidates; do not auto-bind
  |
  +-- user explicitly names a task?
  |       -> use it
  |
  +-- user starts a new task?
          -> create new task
```

`init.py` output shape:

```json
{
  "session_id": "sr-20260930-223501-a7f3",
  "native_session_id": null,
  "session_file": "...",
  "session_created": true,
  "active_task": null,
  "resumable_tasks": []
}
```

## 7. Resume lifecycle

```mermaid
flowchart TD
    A[resume.py] --> B[resolve session]
    B --> C[choose task: --task, --latest, or auto-single]
    C --> D[load STATE.json]
    D --> E{lease owned by another session?}
    E -- valid and no force --> F[LeaseConflict; no takeover]
    E -- expired/missing --> G[write TASK_TAKEOVER, become owner]
    E -- owner is self --> H[renew lease]
    G --> I[inspect dirty/active_operation]
    H --> I
    I --> J{running/planned operation?}
    J -- yes --> K[mark outcome_unknown, keep dirty=true]
    J -- no --> L[reconcile dirty based on unresolved ops]
    K --> M[rewrite CHECKPOINT.md]
    L --> M
    M --> N[session.active_task = task]
    N --> O[emit recovery JSON]
```

Resume command:

```bash
python scripts/resume.py --workspace /work --session-id sr-B --task task-x
python scripts/resume.py --workspace /work --session-id sr-B --latest
```

Resume JSON includes at least:

```json
{
  "task_id": "task-x",
  "status": "in_progress",
  "current_step": "step-2",
  "next_actions": ["..."],
  "dirty": false,
  "checkpoint_path": "...",
  "task_path": "...",
  "task_md_path": "...",
  "state_path": "..."
}
```

The CLI also includes `task_md` and `checkpoint_md` content so a fresh Agent
can read a compact summary without a second call.

## 8. Checkpoint lifecycle

```mermaid
sequenceDiagram
    participant A as Agent
    participant C as checkpoint.py
    participant S as Store
    A->>C: mutate command
    C->>S: acquire per-task lock
    C->>S: load STATE.json
    C->>C: validate expected revision if supplied
    C->>S: apply mutation in memory
    C->>S: atomic write STATE.json (revision + 1)
    C->>S: append EVENTS.jsonl
    C->>S: rewrite CHECKPOINT.md
    C->>S: refresh index.json
    C->>S: release lock
    C-->>A: JSON summary
```

`STATE.json` is updated on every durable mutation.  `CHECKPOINT.md` is
re-rendered after every mutation so it cannot lag behind the response.

## 9. Side-effect lifecycle

```mermaid
stateDiagram-v2
    [*] --> planned: optional
    planned --> running: start-operation
    running --> succeeded: result clearly ok
    running --> failed: result clearly failed
    running --> outcome_unknown: crash / ambiguous
    outcome_unknown --> succeeded: after external inspection
    outcome_unknown --> failed: after external inspection
    outcome_unknown --> outcome_unknown: still cannot determine
```

Before a side-effect tool call:

```text
start-operation
-> operation.state = running
-> dirty = true
-> atomic save
-> execute real tool
```

After the tool call:

```text
clear success   -> finish-operation --outcome succeeded
clear failure   -> finish-operation --outcome failed
ambiguous/crash -> finish-operation --outcome outcome_unknown
```

Resume conservatively changes `running` to `outcome_unknown`; it never changes
it to `succeeded`.

## 10. Takeover lifecycle

```mermaid
sequenceDiagram
    participant A as Session A
    participant B as Session B
    participant T as Task STATE
    A->>T: owner_session=A, lease_expires_at=T
    Note over A,T: A crashes before lease expires
    B->>T: resume
    alt lease still valid
        T-->>B: LeaseConflict, no takeover
    else lease expired
        T->>T: TASK_TAKEOVER, owner_session=B
        T-->>B: resume summary with dirty/operation warnings
    end
```

Forced takeover is available only as an explicit override:

```bash
python scripts/resume.py --workspace /work --session-id sr-B --task task-x --force-takeover
```

## 11. Completion lifecycle

```mermaid
flowchart LR
    A[all steps completed] --> B[finish all operations]
    B --> C[dirty=false]
    C --> D[set-status completed]
    D --> E[EVENT TASK_COMPLETED]
    E --> F[CHECKPOINT current position = Task completed]
    F --> G[index status = completed]
```

Task data is never automatically deleted.  Archiving is reserved for V1:

```text
tasks/<task-id>/ -> archive/<task-id>/
```

## 12. Event log

`EVENTS.jsonl` is append-only, one JSON object per line.  The implementation
supports at least:

```text
SESSION_CREATED
SESSION_ATTACHED
TASK_CREATED
TASK_ATTACHED
TASK_DETACHED
STEP_STARTED
STEP_COMPLETED
STEP_FAILED
FINDING_RECORDED
DECISION_RECORDED
OPERATION_STARTED
OPERATION_SUCCEEDED
OPERATION_FAILED
OPERATION_OUTCOME_UNKNOWN
CHECKPOINT_CREATED
TASK_PAUSED
TASK_BLOCKED
TASK_COMPLETED
TASK_FAILED
TASK_ABANDONED
TASK_TAKEOVER
```

Additional implementation event types include `STEP_ADDED`, `STEP_BLOCKED`,
`BLOCKER_ADDED`, `BLOCKER_RESOLVED`, `NEXT_ACTIONS_SET`,
`CURRENT_STEP_CHANGED`, `TASK_STATUS_CHANGED`, `REQUIREMENTS_UPDATED`,
`LEASE_RENEWED`, and `STATE_CORRUPTION_DETECTED`.

Events are for audit, dirty recovery, state-corruption assistance, and
understanding what a previous Agent did.  A normal resume does not read the
entire event log.
