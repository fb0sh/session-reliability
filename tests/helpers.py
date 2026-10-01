from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import lib  # noqa: E402


def store_dir(workspace: Path) -> Path:
    return workspace / ".agents" / "store" / "session-reliability"


def sessions(workspace: Path) -> list[dict[str, Any]]:
    directory = store_dir(workspace) / "sessions"
    if not directory.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            result.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return result


def default_session_id(workspace: Path) -> Optional[str]:
    found = sessions(workspace)
    if not found:
        return None
    active = [session for session in found if session.get("status") == "active"]
    pool = active or found
    pool.sort(key=lambda session: str(session.get("last_seen_at") or ""), reverse=True)
    return str(pool[0].get("session_id")) if pool else None


def run_script(name: str, *args: str, check: bool = True, env: Optional[dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(SCRIPTS / name), *map(str, args)]
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    result = subprocess.run(cmd, text=True, capture_output=True, env=full_env)
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def run_json(name: str, *args: str, check: bool = True, env: Optional[dict[str, str]] = None) -> dict[str, Any]:
    result = run_script(name, *args, check=check, env=env)
    if not result.stdout.strip():
        raise AssertionError(f"no JSON stdout from {name}: stderr={result.stderr}")
    return json.loads(result.stdout)


def init_session(workspace: Path, *, session_id: Optional[str] = None, native_session_id: Optional[str] = None) -> dict[str, Any]:
    args = ["--workspace", str(workspace)]
    if session_id:
        args += ["--session-id", session_id]
    if native_session_id:
        args += ["--native-session-id", native_session_id]
    return run_json("init.py", *args)


def create_task(workspace: Path, task_id: str, *, title: str = "Demo Task", objective: str = "Demo objective", session_id: Optional[str] = None, lease_seconds: Optional[int] = None, requirements: Optional[list[str]] = None, constraints: Optional[list[str]] = None, success_criteria: Optional[list[str]] = None) -> dict[str, Any]:
    effective_session_id = session_id if session_id is not None else default_session_id(workspace)
    args = [
        "--workspace", str(workspace),
        "create-task",
        "--task-id", task_id,
        "--title", title,
        "--objective", objective,
    ]
    if effective_session_id:
        args += ["--session-id", effective_session_id]
    if lease_seconds is not None:
        args += ["--lease-seconds", str(lease_seconds)]
    for value in requirements or []:
        args += ["--requirement", value]
    for value in constraints or []:
        args += ["--constraint", value]
    for value in success_criteria or []:
        args += ["--success-criterion", value]
    return run_json("checkpoint.py", *args)


def _owner_session(workspace: Path, task_id: str, explicit: Optional[str]) -> Optional[str]:
    if explicit is not None:
        return explicit
    try:
        return state_of(workspace, task_id).get("owner_session")
    except (OSError, json.JSONDecodeError):
        return default_session_id(workspace)


def cp(workspace: Path, task_id: str, *args: str, session_id: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    effective_session_id = _owner_session(workspace, task_id, session_id)
    prefix = ["--workspace", str(workspace), "--task", task_id]
    if effective_session_id:
        prefix += ["--session-id", effective_session_id]
    return run_script("checkpoint.py", *prefix, *args, check=check)


def cp_json(workspace: Path, task_id: str, *args: str, session_id: Optional[str] = None, check: bool = True) -> dict[str, Any]:
    effective_session_id = _owner_session(workspace, task_id, session_id)
    prefix = ["--workspace", str(workspace), "--task", task_id]
    if effective_session_id:
        prefix += ["--session-id", effective_session_id]
    return run_json("checkpoint.py", *prefix, *args, check=check)


def expire_lease(workspace: Path, task_id: str, *, expired_at: str = "2000-01-01T00:00:00+00:00") -> dict[str, Any]:
    state = state_of(workspace, task_id)
    owner = state.get("owner_session")
    store = lib.resolve_store(None, workspace=workspace)

    def mutation(current: dict[str, Any]) -> None:
        current["lease_expires_at"] = expired_at

    return lib.mutate_task(
        store,
        task_id,
        mutation,
        session_id=owner,
        renew_lease=False,
    )


def state_of(workspace: Path, task_id: str) -> dict[str, Any]:
    path = workspace / ".agents" / "store" / "session-reliability" / "tasks" / task_id / "STATE.json"
    return json.loads(path.read_text(encoding="utf-8"))


def task_dir(workspace: Path, task_id: str) -> Path:
    return workspace / ".agents" / "store" / "session-reliability" / "tasks" / task_id


def events_of(workspace: Path, task_id: str) -> list[dict[str, Any]]:
    path = task_dir(workspace, task_id) / "EVENTS.jsonl"
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def index_of(workspace: Path) -> dict[str, Any]:
    path = workspace / ".agents" / "store" / "session-reliability" / "index.json"
    return json.loads(path.read_text(encoding="utf-8"))
