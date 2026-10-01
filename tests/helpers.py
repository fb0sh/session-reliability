from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


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
    args = [
        "--workspace", str(workspace),
        "create-task",
        "--task-id", task_id,
        "--title", title,
        "--objective", objective,
    ]
    if session_id:
        args += ["--session-id", session_id]
    if lease_seconds is not None:
        args += ["--lease-seconds", str(lease_seconds)]
    for value in requirements or []:
        args += ["--requirement", value]
    for value in constraints or []:
        args += ["--constraint", value]
    for value in success_criteria or []:
        args += ["--success-criterion", value]
    return run_json("checkpoint.py", *args)


def cp(workspace: Path, task_id: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_script("checkpoint.py", "--workspace", str(workspace), "--task", task_id, *args, check=check)


def cp_json(workspace: Path, task_id: str, *args: str, check: bool = True) -> dict[str, Any]:
    return run_json("checkpoint.py", "--workspace", str(workspace), "--task", task_id, *args, check=check)


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
