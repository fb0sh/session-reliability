# CLI Reference

This file is the exact command reference for the bundled session-reliability
scripts.  Resolve paths from the loaded skill installation; do not assume the
current working directory is the skill root.

## Contents

- [Initialize](#initialize)
- [List tasks](#list-tasks)
- [Create a task](#create-a-task)
- [Steps](#steps)
- [Findings and decisions](#findings-and-decisions)
- [Requirements, constraints, objective](#requirements-constraints-objective)
- [Blockers](#blockers)
- [Checkpoint](#checkpoint)
- [Operation / side-effect safety](#operation--side-effect-safety)
- [Resume and takeover](#resume-and-takeover)
- [Lease and session attachment](#lease-and-session-attachment)
- [Status changes and completion](#status-changes-and-completion)
- [Index maintenance](#index-maintenance)
- [Revision guard](#revision-guard)
- [Output and errors](#output-and-errors)

Conceptual placeholders:

```text
<SKILL_ROOT>      directory containing the loaded session-reliability/SKILL.md
<WORKSPACE_ROOT>  user's current workspace/project
<SESSION_ID>      reliability session id, for example sr-20260930-223501-a7f3
<TASK_ID>         task id, for example task-20260930-223550-fix-downloader
<N>               positive integer revision
```

These are placeholders, not environment variables.

For task mutations, `--session-id "<SESSION_ID>"` is normally required whenever
the task has an owner.  The reliability session id is returned by `init.py`
and must be reused for the lifetime of the current conversation.

Common options:

```text
--workspace PATH
--store PATH
--task TASK_ID
--session-id SESSION_ID
--expected-revision N
```

`--workspace` is usually required when the script is not run with the workspace
as its current directory.  `--store` overrides the default runtime store.

## Initialize

Create or refresh the runtime store and a reliability session, then report
unfinished tasks.

```bash
python "<SKILL_ROOT>/scripts/init.py" --workspace "<WORKSPACE_ROOT>"
python "<SKILL_ROOT>/scripts/init.py" --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>"
python "<SKILL_ROOT>/scripts/init.py" --workspace "<WORKSPACE_ROOT>" \
  --native-session-id "<RUNTIME_SESSION_ID>"
```

Options:

```text
--workspace PATH
--store PATH
--session-id SESSION_ID
--native-session-id NATIVE_ID
```

Stdout is JSON containing at least `session_id`, `store`, `active_task`, and
`resumable_tasks`.

If `--session-id` is omitted but `--native-session-id` is present, `init.py`
reuses the unique existing reliability session with that native id.  Zero
matches creates a new `sr-*` session; multiple matches fail with
`session_identity_conflict`.

## List tasks

```bash
python "<SKILL_ROOT>/scripts/list.py" --workspace "<WORKSPACE_ROOT>" --unfinished
python "<SKILL_ROOT>/scripts/list.py" --workspace "<WORKSPACE_ROOT>" --active
python "<SKILL_ROOT>/scripts/list.py" --workspace "<WORKSPACE_ROOT>" --paused
python "<SKILL_ROOT>/scripts/list.py" --workspace "<WORKSPACE_ROOT>" --blocked
python "<SKILL_ROOT>/scripts/list.py" --workspace "<WORKSPACE_ROOT>" --completed
python "<SKILL_ROOT>/scripts/list.py" --workspace "<WORKSPACE_ROOT>" --all
python "<SKILL_ROOT>/scripts/list.py" --workspace "<WORKSPACE_ROOT>" \
  --status in_progress
```

Options:

```text
--active
--paused
--blocked
--unfinished
--completed
--all
--status {pending,in_progress,blocked,paused,completed,failed,abandoned}
```

`--unfinished` covers `pending`, `in_progress`, `blocked`, and `paused`.

## Create a task

```bash
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>" \
  create-task \
  --task-id "<TASK_ID>" \
  --title "Fix downloader" \
  --objective "Make the downloader reliable." \
  --requirement "Handle 404 responses." \
  --constraint "Use Python stdlib only." \
  --success-criterion "A fresh session can resume safely."
```

Options:

```text
--task-id TASK_ID
--title TITLE
--objective OBJECTIVE
--requirement REQUIREMENT       repeatable
--constraint CONSTRAINT         repeatable
--success-criterion CRITERION   repeatable
--session-id SESSION_ID
--lease-seconds SECONDS
```

If `--session-id` is supplied, the new task is attached to that session and an
initial lease is written.  If it is omitted, the task is unowned; bind it with
`attach-session` or `resume` before any ordinary mutation.

## Steps

```bash
# add
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  add-step --title "Analyze logs"

# start
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  start-step --step step-1

# complete
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  complete-step --step step-1 --summary "Root cause found"

# fail
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  fail-step --step step-1 --summary "Could not reproduce"

# block
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  block-step --step step-1 --reason "Dependency unavailable"

# set current step
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  set-current-step --step step-2

# clear current step
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  set-current-step --clear

# replace next actions
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  set-next-actions --action "Implement fix" --action "Run tests"
```

`add-step` accepts optional `--step-id` and `--status`.

## Findings and decisions

```bash
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  record-finding --summary "404 responses are not handled"

python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  record-decision --summary "Use retry with backoff"
```

## Requirements, constraints, objective

```bash
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  update-requirements \
  --objective "Updated objective" \
  --requirement "New requirement" \
  --constraint "New constraint" \
  --success-criterion "New success criterion"
```

`--requirement`, `--constraint`, and `--success-criterion` are repeatable.

## Blockers

```bash
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  add-blocker --summary "Waiting for package index"

python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  resolve-blocker --summary "Waiting for package index"

python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  resolve-blocker --all
```

## Checkpoint

```bash
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  checkpoint \
  --important-context "Log file: /var/log/app.log" \
  --blocker "Waiting for external API"
```

Options:

```text
--important-context TEXT   repeatable
--blocker TEXT            repeatable
--clear-blockers
```

Every durable mutation also refreshes `CHECKPOINT.md`; the explicit
`checkpoint` subcommand records a checkpoint event and optional context.

## Operation / side-effect safety

```bash
# Persist intent before executing the external side effect.
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  start-operation --description "Install package X"

# Inspect result, then record a known outcome.
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  finish-operation \
  --operation-id op-1 \
  --outcome succeeded \
  --result-summary "Package confirmed installed"

python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  finish-operation \
  --operation-id op-1 \
  --outcome failed \
  --result-summary "Install failed"

# Uncertain outcome: record unknown, inspect external state later.
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  finish-operation \
  --operation-id op-1 \
  --outcome outcome_unknown \
  --result-summary "Could not determine whether the package installed"

# Alias for outcome_unknown
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  unknown-operation \
  --operation-id op-1 \
  --result-summary "State uncertain"
```

`start-operation` also accepts `--operation-id` and
`--side-effect` / `--no-side-effect`.

Ordinary owner mutations automatically renew the task lease and refresh the
session heartbeat.  `renew-lease` only extends the current owner's lease; it
cannot change ownership or bind an unowned task.

`finish-operation` usually uses `--operation-id <op-id>`; if omitted, the
current active operation is resolved when unambiguous.

## Resume and takeover

```bash
# Resume a specific task.
python "<SKILL_ROOT>/scripts/resume.py" \
  --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>" \
  --task "<TASK_ID>"

# Resume the most recently updated unfinished task.
python "<SKILL_ROOT>/scripts/resume.py" \
  --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>" \
  --latest

# Take over even if another session still has a valid lease.
python "<SKILL_ROOT>/scripts/resume.py" \
  --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>" \
  --task "<TASK_ID>" \
  --force-takeover
```

`resume.py` returns a recovery payload and updates the session's
`active_task`.  If a running operation is discovered, it is conservatively
marked `outcome_unknown` and the task stays dirty until explicitly finished.

`--force-takeover` is appropriate when the user explicitly confirms that the
previous session failed, became unavailable, or cannot continue.  It records
`TASK_TAKEOVER` with `forced=true` even if the previous lease is still valid.

## Lease and session attachment

```bash
# Renew the lease for a session.
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  renew-lease --lease-seconds 900

# Attach a session to a task.
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  attach-session

# Forced attach/takeover.
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  attach-session --force

# Detach the current session.
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  detach-session
```

## Status changes and completion

```bash
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  set-status --status blocked --note "Waiting for access"

python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" --session-id "<SESSION_ID>" --task "<TASK_ID>" \
  set-status --status completed
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

Completion is rejected while the task is dirty or has unresolved operations.

## Index maintenance

```bash
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" \
  rebuild-index
```

## Revision guard

Any mutating command can pass an expected revision:

```bash
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>" \
  --task "<TASK_ID>" \
  --expected-revision <N> \
  set-next-actions --action "Continue"
```

If the on-disk revision changed, the command exits with a revision conflict
instead of overwriting newer state.

## Output and errors

- Successful commands write JSON to stdout.
- Errors write a human-readable message to stderr.
- Non-zero exit codes signal validation, conflict, lease, corruption, and
  not-found errors.
- `index.json` is a discovery cache; always treat `STATE.json` as
  authoritative.
