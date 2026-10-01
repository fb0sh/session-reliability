# session-reliability

A runtime-agnostic Agent Skill for durable, multi-session tasks.

[中文说明](README.md)

A conversation session is temporary. A task is persistent. This skill keeps
the task recoverable when the session disappears, by storing a durable task
record under the workspace:

```text
<WORKSPACE_ROOT>/.agents/store/session-reliability/
```

## Runtime activation

Skill activation is controlled by the host Agent Runtime.

Runtimes that support global, startup, or always-on skills can load this skill
proactively at session start.

In other runtimes, the skill `description` is designed to trigger for:

- multi-step, multi-tool-call, multi-turn work
- long refactors, migrations, fixes, and investigations
- side-effecting work that may be interrupted
- continue, resume, recover, pick up, or check previous work

This skill does not claim an unconditional cross-runtime session-start hook.
It guarantees that once activated and working, durable state can be recovered
in a genuinely new session.

## Design constraints

- Python 3.10+ standard library only.
- Filesystem + Markdown + JSON + JSONL.
- No database, daemon, HTTP server, RPC, watcher, cloud sync, or platform API.
- No dependency on runtime-native session ids.
- Works with Codex, Claude Code, DSH, or any other Agent runtime.

## Repository layout

```text
session-reliability/
├── SKILL.md
├── README.md
├── README.en.md
├── scripts/
│   ├── __init__.py
│   ├── lib.py
│   ├── init.py
│   ├── checkpoint.py
│   ├── resume.py
│   └── list.py
├── references/
│   ├── protocol.md
│   ├── recovery.md
│   ├── concurrency.md
│   ├── state-schema.md
│   └── cli.md
├── assets/
│   ├── TASK.md
│   ├── CHECKPOINT.md
│   ├── state.json
│   └── session.json
└── tests/
```

`SKILL.md` is the Agent execution entry point. Detailed protocols, recovery
algorithms, concurrency rules, schemas, and CLI details live in `references/`
and are loaded only when needed. `assets/` contains template resources used by
the scripts.

## Install / use

Keep the skill directory together. Bundled scripts and assets are read from
`SKILL_ROOT`; the skill does not hard-code its installation path and does not
assume cwd equals the skill root.

Runtime state is written to:

```text
<WORKSPACE_ROOT>/.agents/store/session-reliability/
```

Minimal flow:

```bash
# 1. initialize the store and a reliability session
python "<SKILL_ROOT>/scripts/init.py" --workspace "<WORKSPACE_ROOT>"

# 2. create a durable task
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>" \
  create-task \
  --task-id task-20260930-223550-fix-downloader \
  --title "Fix downloader" \
  --objective "Make the downloader reliable."

# 3. persist progress
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>" \
  --task task-20260930-223550-fix-downloader \
  add-step --title "Analyze logs"

# 4. resume from a fresh session
python "<SKILL_ROOT>/scripts/resume.py" \
  --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>" \
  --task task-20260930-223550-fix-downloader
```

See `SKILL.md` for Agent behavior and `references/cli.md` for exact commands.

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

## Session ownership and crash takeover

A task with an owner accepts mutations only from that owner session while its
lease is valid.

Successful owner mutations renew the lease automatically using the task's
persisted `lease_duration_seconds` (default 900), so a custom `--lease-seconds`
value is not reset to the default by later heartbeats.  They also refresh the
owning session's `last_seen_at`.

Another session cannot mutate the task directly.  It must wait for lease
expiry and take over, or become owner through an explicit attach/resume flow.

If the user explicitly confirms that the previous session crashed, became
unavailable, or cannot continue, a new session may use explicit force takeover
even while the old lease is still valid.  Task takeover transfers ownership of
that task only; it does not mark the previous reliability session expired and
does not clear the previous session's other active task.  After takeover,
reconcile dirty and `outcome_unknown` operations before continuing.

`renew-lease` only extends the current owner's lease; it never changes
ownership.  An unowned task must be bound with `attach-session` or `resume`
before ordinary mutation.

When the runtime provides a native session id, `init` reuses an existing
reliability session when possible.  `native_session_id` remains globally unique
when present; duplicate mappings or explicit rebinds fail safely instead of
choosing one at random or silently overwriting identity.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The test suite includes init, task creation, progress, fresh-session resume,
dirty-operation recovery, operation success, lease blocking/takeover, revision
conflicts, atomic writes, missing index rebuild, multiple unfinished tasks,
corrupted `STATE.json`, and a full crash/takeover/completion integration test.
It also includes skill structure tests for frontmatter, resource layout,
reference links, and script cwd independence, plus session ownership, automatic
lease renewal, force takeover, native-session-id reuse/conflict, and
multi-session isolation tests.

## Safety

Do not put passwords, tokens, API keys, credentials, or private keys into task
state, checkpoints, or event logs. `CHECKPOINT.md` is data, not executable
code. Never automatically repeat a side-effect operation whose outcome is
unknown.
