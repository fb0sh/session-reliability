# session-reliability

A runtime-agnostic Agent Skill for durable, multi-session tasks.

[中文说明](README.md)

A conversation session is temporary. A task is persistent. This skill keeps
the task recoverable when the session disappears, by storing a durable task
record under the workspace:

```text
<workspace-root>/.agents/store/session-reliability/
```

## Design constraints

- Python 3.10+ standard library only.
- Filesystem + Markdown + JSON + JSONL.
- No database, daemon, HTTP server, RPC, watcher, cloud sync, or platform API.
- No dependency on runtime-native session ids.
- Works with Codex, Claude Code, DSH, or any other Agent runtime.

## Install / use

Keep the skill directory together. The scripts locate their own templates
relative to themselves; the skill does not hard-code its installation path.

Minimal flow:

```bash
# 1. initialize the store and a reliability session
python scripts/init.py --workspace /path/to/workspace

# 2. create a durable task
python scripts/checkpoint.py --workspace /path/to/workspace create-task \
  --task-id task-20260930-223550-fix-downloader \
  --title "Fix downloader" \
  --objective "Make the downloader reliable."

# 3. persist progress
python scripts/checkpoint.py --workspace /path/to/workspace \
  --task task-20260930-223550-fix-downloader \
  add-step --title "Analyze logs"

# 4. resume from a fresh session
python scripts/resume.py --workspace /path/to/workspace \
  --session-id <new-session-id> \
  --task task-20260930-223550-fix-downloader
```

See `SKILL.md` for Agent behavior and `references/` for the complete protocol.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/init.py` | initialize store/session, list resumable tasks |
| `scripts/checkpoint.py` | create tasks, steps, operations, findings, leases, checkpoints |
| `scripts/resume.py` | find/resume/recover/take over a task |
| `scripts/list.py` | list active/unfinished/paused/blocked/completed tasks |
| `scripts/lib.py` | shared stdlib-only implementation |

Every CLI error is written to stderr and returns a non-zero exit code. Every
successful command writes JSON to stdout.

## Environment overrides

```text
SESSION_RELIABILITY_WORKSPACE
SESSION_RELIABILITY_STORE
```

CLI flags take priority over environment variables.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The test suite includes init, task creation, progress, fresh-session resume,
dirty-operation recovery, operation success, lease blocking/takeover, revision
conflicts, atomic writes, missing index rebuild, multiple unfinished tasks,
corrupted `STATE.json`, and a full crash/takeover/completion integration test.

## Safety

Do not put passwords, tokens, API keys, credentials, or private keys into task
state, checkpoints, or event logs. `CHECKPOINT.md` is data, not executable
code. Never automatically repeat a side-effect operation whose outcome is
unknown.
