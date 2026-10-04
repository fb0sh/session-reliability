"""Shared implementation for the session-reliability skill.

Only the Python standard library is used.  The module intentionally keeps the
on-disk format simple: filesystem + Markdown + JSON + JSONL.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

SCHEMA_VERSION = 1
DEFAULT_LEASE_SECONDS = 15 * 60

STORE_DIRNAME = Path(".agents") / "store" / "session-reliability"
INDEX_FILENAME = "index.json"
EVENTS_FILENAME = "EVENTS.jsonl"
STATE_FILENAME = "STATE.json"
TASK_FILENAME = "TASK.md"
CHECKPOINT_FILENAME = "CHECKPOINT.md"
RECOVERY_REQUIRED_FILENAME = "RECOVERY_REQUIRED.md"

TASK_STATUSES = [
    "pending",
    "in_progress",
    "blocked",
    "paused",
    "completed",
    "failed",
    "abandoned",
]

UNFINISHED_STATUSES = {"pending", "in_progress", "blocked", "paused"}
FINAL_STATUSES = {"completed", "failed", "abandoned"}

STEP_STATUSES = ["pending", "in_progress", "completed", "failed", "blocked", "skipped"]

OPERATION_STATES = ["planned", "running", "succeeded", "failed", "outcome_unknown"]
UNRESOLVED_OPERATION_STATES = {"planned", "running", "outcome_unknown"}

SESSION_STATUSES = ["active", "idle", "closed", "expired"]

EVENT_TYPES = {
    "SESSION_CREATED",
    "SESSION_ATTACHED",
    "TASK_CREATED",
    "TASK_ATTACHED",
    "TASK_DETACHED",
    "STEP_STARTED",
    "STEP_COMPLETED",
    "STEP_FAILED",
    "FINDING_RECORDED",
    "DECISION_RECORDED",
    "OPERATION_STARTED",
    "OPERATION_SUCCEEDED",
    "OPERATION_FAILED",
    "OPERATION_OUTCOME_UNKNOWN",
    "CHECKPOINT_CREATED",
    "TASK_PAUSED",
    "TASK_BLOCKED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "TASK_ABANDONED",
    "TASK_TAKEOVER",
    "TASK_STATUS_CHANGED",
    "REQUIREMENTS_UPDATED",
    "LEASE_RENEWED",
    "STATE_CORRUPTION_DETECTED",
    "STEP_ADDED",
    "STEP_BLOCKED",
    "BLOCKER_ADDED",
    "BLOCKER_RESOLVED",
    "NEXT_ACTIONS_SET",
    "CURRENT_STEP_CHANGED",
}

STATUS_EVENT = {
    "paused": "TASK_PAUSED",
    "blocked": "TASK_BLOCKED",
    "completed": "TASK_COMPLETED",
    "failed": "TASK_FAILED",
    "abandoned": "TASK_ABANDONED",
}

_ID_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SRError(Exception):
    """Base class for all controlled session-reliability errors."""

    code = 1
    kind = "error"

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        data = {"error": self.kind, "message": self.message}
        if self.details:
            data["details"] = self.details
        return data


class ValidationError(SRError):
    code = 2
    kind = "validation_error"


class StoreError(SRError):
    code = 1
    kind = "store_error"


class TaskNotFound(SRError):
    code = 5
    kind = "task_not_found"


class RevisionConflict(SRError):
    code = 3
    kind = "revision_conflict"


class LeaseConflict(SRError):
    code = 4
    kind = "lease_conflict"


class CorruptionError(SRError):
    code = 6
    kind = "state_corruption"


class FutureSchemaError(SRError):
    code = 7
    kind = "unsupported_schema_version"


class SchemaError(CorruptionError):
    kind = "invalid_state_schema"


class SessionIdentityConflict(SRError):
    code = 8
    kind = "session_identity_conflict"


class SessionIdentityCorruption(SRError):
    code = 9
    kind = "session_identity_corruption"


# ---------------------------------------------------------------------------
# Time / IDs
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """Return an offset-aware UTC timestamp in ISO 8601 format."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: Any) -> Optional[_dt.datetime]:
    if not value:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"timestamp must be a string or null, got {type(value).__name__}")
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"invalid ISO 8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def lease_is_valid(lease_expires_at: Any, *, now: Optional[_dt.datetime] = None) -> bool:
    expiry = parse_iso(lease_expires_at)
    if expiry is None:
        return False
    current = now or _dt.datetime.now(_dt.timezone.utc)
    return expiry > current


def lease_expiry_from_now(seconds: int = DEFAULT_LEASE_SECONDS) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=max(0, seconds))).isoformat(
        timespec="seconds"
    )


def get_lease_duration_seconds(state: dict[str, Any]) -> int:
    """Return the task's preferred lease duration, with V1 fallback.

    ``lease_duration_seconds`` is optional for older V1 state files.  When it
    is absent, the default 900-second duration is used.
    """
    value = state.get("lease_duration_seconds", DEFAULT_LEASE_SECONDS)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SchemaError("lease_duration_seconds must be a positive integer")
    return value


def slugify(text: str, max_length: int = 40) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text:
        return secrets.token_hex(2)
    return text[:max_length].strip("-") or secrets.token_hex(2)


def _timestamp_compact() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def generate_session_id() -> str:
    return f"sr-{_timestamp_compact()}-{secrets.token_hex(2)}"


def generate_task_id(title: str) -> str:
    return f"task-{_timestamp_compact()}-{slugify(title)}"


def validate_id_component(value: str, *, field: str = "id") -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty string")
    if ".." in value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValidationError(f"{field} contains an unsafe path component: {value!r}")
    if not _ID_COMPONENT_RE.match(value):
        raise ValidationError(f"{field} has invalid format: {value!r}")
    return value


def next_step_id(state: dict[str, Any]) -> str:
    existing = {str(step.get("id")) for step in state.get("steps", [])}
    index = 1
    while True:
        candidate = f"step-{index}"
        if candidate not in existing:
            return candidate
        index += 1


def next_operation_id(state: dict[str, Any]) -> str:
    existing = {str(op.get("id")) for op in state.get("operations", [])}
    index = 1
    while True:
        candidate = f"op-{index}"
        if candidate not in existing:
            return candidate
        index += 1


# ---------------------------------------------------------------------------
# Workspace / store resolution
# ---------------------------------------------------------------------------


def resolve_workspace(cli_workspace: Optional[str] = None) -> Path:
    """Resolve workspace root.

    Priority: explicit CLI value, SESSION_RELIABILITY_WORKSPACE, cwd.
    The current working directory is always a valid workspace.
    """
    if cli_workspace:
        path = Path(os.path.expanduser(cli_workspace))
    elif os.environ.get("SESSION_RELIABILITY_WORKSPACE"):
        path = Path(os.path.expanduser(os.environ["SESSION_RELIABILITY_WORKSPACE"]))
    else:
        path = Path.cwd()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def resolve_store(cli_store: Optional[str] = None, *, workspace: Optional[Path] = None) -> Path:
    """Resolve runtime store path.

    Priority: explicit CLI value, SESSION_RELIABILITY_STORE, then
    <workspace-root>/.agents/store/session-reliability/.
    """
    workspace = workspace or resolve_workspace()
    if cli_store:
        path = Path(os.path.expanduser(cli_store))
        if not path.is_absolute():
            path = workspace / path
    elif os.environ.get("SESSION_RELIABILITY_STORE"):
        path = Path(os.path.expanduser(os.environ["SESSION_RELIABILITY_STORE"]))
        if not path.is_absolute():
            path = workspace / path
    else:
        path = workspace / STORE_DIRNAME
    return path.resolve()


# ---------------------------------------------------------------------------
# Atomic file operations / locking
# ---------------------------------------------------------------------------


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace *path* with *text* using write/fsync/os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(3)}")
    try:
        with open(tmp, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            if tmp.exists():
                tmp.unlink()


def atomic_write_json(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    atomic_write_text(path, text)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        with contextlib.suppress(OSError):
            os.fsync(handle.fileno())


class ProcessLock:
    """A small cross-process exclusive lock with crash-safe semantics.

    POSIX uses ``fcntl.flock``.  Windows uses ``msvcrt.locking`` byte-range
    locking.  Both are kernel-backed, so an abnormal process exit releases the
    lock when the OS closes its file descriptor/handle.  Lock-file existence
    never means the lock is currently held.  Lock files are intentionally kept
    as stable inode/handle anchors.

    Unknown platforms without either kernel backend keep a best-effort
    ``O_CREAT|O_EXCL`` fallback for compatibility.  That fallback is not used
    on Linux, macOS, or Windows and can leave stale lock files after a crash.
    """

    def __init__(self, path: Path, *, timeout: float = 10.0):
        self.path = Path(path)
        self.timeout = timeout
        self._fd: Optional[int] = None
        self._backend: Optional[str] = None
        self._fallback_created = False

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.release()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        if _HAS_FCNTL:
            self._acquire_flock(deadline)
            return
        if _IS_WINDOWS and _HAS_MSVCRT:
            self._acquire_msvcrt(deadline)
            return
        self._acquire_exclusive_file_fallback(deadline)

    def release(self) -> None:
        backend = self._backend
        self._backend = None
        if backend == "flock":
            self._release_flock()
        elif backend == "msvcrt":
            self._release_msvcrt()
        elif backend == "fallback":
            self._release_exclusive_file_fallback()

    # -- POSIX flock -----------------------------------------------------

    def _acquire_flock(self, deadline: float) -> None:
        import fcntl  # type: ignore

        self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._backend = "flock"
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    self._release_flock()
                    raise StoreError(f"failed to acquire lock {self.path}: {exc}") from exc
                if time.monotonic() >= deadline:
                    self._release_flock()
                    raise StoreError(f"timed out waiting for lock: {self.path}")
                time.sleep(0.025)

    def _release_flock(self) -> None:
        if self._fd is None:
            return
        import fcntl  # type: ignore

        with contextlib.suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None

    # -- Windows msvcrt --------------------------------------------------

    def _acquire_msvcrt(self, deadline: float) -> None:
        import msvcrt  # type: ignore

        self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if os.fstat(self._fd).st_size == 0:
                os.write(self._fd, b"\0")
                with contextlib.suppress(OSError):
                    os.fsync(self._fd)
            while True:
                os.lseek(self._fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                    self._backend = "msvcrt"
                    return
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        self._release_msvcrt()
                        raise StoreError(f"failed to acquire lock {self.path}: {exc}") from exc
                    if time.monotonic() >= deadline:
                        self._release_msvcrt()
                        raise StoreError(f"timed out waiting for lock: {self.path}")
                    time.sleep(0.025)
        except Exception:
            self._release_msvcrt()
            raise

    def _release_msvcrt(self) -> None:
        if self._fd is None:
            return
        import msvcrt  # type: ignore

        with contextlib.suppress(OSError):
            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None

    # -- unknown-platform fallback --------------------------------------

    def _acquire_exclusive_file_fallback(self, deadline: float) -> None:
        # This path is not used on Linux, macOS, or Windows.  It cannot be
        # crash-safe because it has no kernel lock primitive; a crash can leave
        # a stale lock file that must be cleaned up manually.
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
                os.close(fd)
                self._fallback_created = True
                self._backend = "fallback"
                return
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise StoreError(
                        f"timed out waiting for non-crash-safe fallback lock: {self.path}"
                    )
                time.sleep(0.05)

    def _release_exclusive_file_fallback(self) -> None:
        if self._fallback_created:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            self._fallback_created = False


try:
    import fcntl as _fcntl_probe  # noqa: F401

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - exercised only on Windows
    _HAS_FCNTL = False

_IS_WINDOWS = os.name == "nt"

try:
    import msvcrt as _msvcrt_probe  # noqa: F401

    _HAS_MSVCRT = True
except ImportError:  # pragma: no cover - exercised on non-Windows platforms
    _HAS_MSVCRT = False


def task_lock_path(store: Path, task_id: str) -> Path:
    return store / "tasks" / task_id / ".lock"


def index_lock_path(store: Path) -> Path:
    return store / "index.lock"


def session_lock_path(store: Path, session_id: str) -> Path:
    return store / "sessions" / f"{session_id}.lock"


def session_identity_lock_path(store: Path) -> Path:
    return store / "sessions.identity.lock"


# ---------------------------------------------------------------------------
# Schema / model helpers
# ---------------------------------------------------------------------------


def require_dict(value: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{where} must be a JSON object")
    return value


def check_schema_version(data: dict[str, Any], *, where: str) -> int:
    if "schema_version" not in data:
        raise SchemaError(f"{where} is missing required field schema_version")
    version = data["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise SchemaError(f"{where}.schema_version must be an integer")
    if version > SCHEMA_VERSION:
        raise FutureSchemaError(
            f"{where} uses schema_version {version}, but this skill only supports up to {SCHEMA_VERSION}"
        )
    if version < 1:
        raise SchemaError(f"{where}.schema_version must be >= 1")
    return version


def validate_operation(operation: dict[str, Any], *, where: str = "operation") -> None:
    require_dict(operation, where=where)
    for key in ("id", "description", "state", "side_effect"):
        if key not in operation:
            raise SchemaError(f"{where} missing required field {key!r}")
    if operation["state"] not in OPERATION_STATES:
        raise SchemaError(f"{where}.state must be one of {OPERATION_STATES}")
    if not isinstance(operation["side_effect"], bool):
        raise SchemaError(f"{where}.side_effect must be a boolean")


def validate_step(step: dict[str, Any], *, where: str = "step") -> None:
    require_dict(step, where=where)
    for key in ("id", "title", "status"):
        if key not in step:
            raise SchemaError(f"{where} missing required field {key!r}")
    if step["status"] not in STEP_STATUSES:
        raise SchemaError(f"{where}.status must be one of {STEP_STATUSES}")


def validate_state(state: dict[str, Any], *, where: str = "STATE.json") -> None:
    require_dict(state, where=where)
    check_schema_version(state, where=where)
    for key in ("task_id", "title", "status", "revision", "objective", "steps", "next_actions", "dirty"):
        if key not in state:
            raise SchemaError(f"{where} missing required field {key!r}")
    if state["status"] not in TASK_STATUSES:
        raise SchemaError(f"{where}.status must be one of {TASK_STATUSES}")
    if not isinstance(state["revision"], int) or isinstance(state["revision"], bool) or state["revision"] < 1:
        raise SchemaError(f"{where}.revision must be a positive integer")
    if not isinstance(state["steps"], list):
        raise SchemaError(f"{where}.steps must be an array")
    for i, step in enumerate(state["steps"]):
        validate_step(step, where=f"{where}.steps[{i}]")
    if state.get("operations") is not None:
        if not isinstance(state["operations"], list):
            raise SchemaError(f"{where}.operations must be an array")
        for i, operation in enumerate(state["operations"]):
            validate_operation(operation, where=f"{where}.operations[{i}]")
    if not isinstance(state["next_actions"], list):
        raise SchemaError(f"{where}.next_actions must be an array")
    if not isinstance(state["dirty"], bool):
        raise SchemaError(f"{where}.dirty must be a boolean")
    active = state.get("active_operation")
    if active is not None and not isinstance(active, str):
        raise SchemaError(f"{where}.active_operation must be a string or null")
    if "lease_duration_seconds" in state:
        duration = state["lease_duration_seconds"]
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            raise SchemaError(f"{where}.lease_duration_seconds must be a positive integer")


def validate_session(session: dict[str, Any], *, where: str = "session.json") -> None:
    require_dict(session, where=where)
    check_schema_version(session, where=where)
    for key in ("session_id", "status", "started_at", "last_seen_at"):
        if key not in session:
            raise SchemaError(f"{where} missing required field {key!r}")
    if session["status"] not in SESSION_STATUSES:
        raise SchemaError(f"{where}.status must be one of {SESSION_STATUSES}")


def validate_task_ownership(
    state: dict[str, Any],
    *,
    task_id: str,
    session_id: Optional[str],
) -> None:
    """Reject ordinary mutations that are not made by the current owner."""
    owner = state.get("owner_session")
    if owner is None:
        raise LeaseConflict(
            f"task {task_id} is unowned; attach-session or resume is required before mutation",
            details={"reason": "unowned", "owner_session": None, "session_id": session_id},
        )
    if not session_id:
        raise LeaseConflict(
            f"task {task_id} is owned by session {owner}; --session-id is required for mutation",
            details={"reason": "missing_session", "owner_session": owner, "session_id": None},
        )
    if session_id != owner:
        lease_valid = lease_is_valid(state.get("lease_expires_at"))
        raise LeaseConflict(
            f"task {task_id} is owned by session {owner}; session {session_id} cannot mutate it "
            f"({'lease still valid' if lease_valid else 'lease expired; use resume or attach-session to take over'})",
            details={
                "reason": "lease_valid" if lease_valid else "lease_expired",
                "owner_session": owner,
                "lease_expires_at": state.get("lease_expires_at"),
                "session_id": session_id,
            },
        )


def apply_task_ownership_transfer(
    state: dict[str, Any],
    *,
    task_id: str,
    session_id: str,
    force: bool,
    lease_duration_seconds: Optional[int] = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Apply attach/renew/takeover ownership semantics in memory.

    Must be called while holding the task lock.  When
    ``lease_duration_seconds`` is None, the task's persisted duration is
    preserved (with the legacy V1 fallback of 900 seconds).  The caller is
    responsible for persisting the mutated task state and for best-effort
    session metadata cleanup after the task write.
    """
    duration = (
        lease_duration_seconds
        if lease_duration_seconds is not None
        else get_lease_duration_seconds(state)
    )
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        raise ValidationError("lease_duration_seconds must be a positive integer")

    owner = state.get("owner_session")
    lease_valid = lease_is_valid(state.get("lease_expires_at"))
    events: list[tuple[str, dict[str, Any]]] = []

    if owner is None:
        events.append(("TASK_ATTACHED", {"session_id": session_id}))
    elif owner == session_id:
        events.append(
            ("LEASE_RENEWED", {"session_id": session_id, "lease_seconds": duration})
        )
    else:
        if lease_valid and not force:
            raise LeaseConflict(
                f"task {task_id} is leased to session {owner} until {state.get('lease_expires_at')}",
                details={
                    "reason": "lease_valid",
                    "owner_session": owner,
                    "lease_expires_at": state.get("lease_expires_at"),
                    "session_id": session_id,
                },
            )
        events.append(
            (
                "TASK_TAKEOVER",
                {
                    "from_session": owner,
                    "to_session": session_id,
                    "lease_expired": not lease_valid,
                    "forced": bool(force and lease_valid),
                },
            )
        )

    state["owner_session"] = session_id
    state["lease_duration_seconds"] = duration
    state["lease_expires_at"] = lease_expiry_from_now(duration)
    events.append(("SESSION_ATTACHED", {"session_id": session_id}))
    return events


def default_state(
    *,
    task_id: str,
    title: str,
    objective: str,
    status: str = "pending",
    owner_session: Optional[str] = None,
    lease_expires_at: Optional[str] = None,
    lease_duration_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any]:
    ts = now_iso()
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "title": title,
        "status": status,
        "revision": 1,
        "objective": objective,
        "current_step": None,
        "steps": [],
        "next_actions": [],
        "owner_session": owner_session,
        "lease_expires_at": lease_expires_at,
        "lease_duration_seconds": lease_duration_seconds,
        "dirty": False,
        "active_operation": None,
        "operations": [],
        "requirements": [],
        "constraints": [],
        "success_criteria": [],
        "facts": [],
        "decisions": [],
        "blockers": [],
        "important_context": [],
        "created_at": ts,
        "updated_at": ts,
    }
    return state


def default_session(*, session_id: str, native_session_id: Optional[str] = None) -> dict[str, Any]:
    ts = now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "native_session_id": native_session_id,
        "status": "active",
        "active_task": None,
        "started_at": ts,
        "last_seen_at": ts,
    }


# ---------------------------------------------------------------------------
# Task paths and base load/save
# ---------------------------------------------------------------------------


def task_dir(store: Path, task_id: str) -> Path:
    validate_id_component(task_id, field="task_id")
    return store / "tasks" / task_id


def session_path(store: Path, session_id: str) -> Path:
    validate_id_component(session_id, field="session_id")
    return store / "sessions" / f"{session_id}.json"


def state_path(store: Path, task_id: str) -> Path:
    return task_dir(store, task_id) / STATE_FILENAME


def checkpoint_path(store: Path, task_id: str) -> Path:
    return task_dir(store, task_id) / CHECKPOINT_FILENAME


def events_path(store: Path, task_id: str) -> Path:
    return task_dir(store, task_id) / EVENTS_FILENAME


def task_markdown_path(store: Path, task_id: str) -> Path:
    return task_dir(store, task_id) / TASK_FILENAME


def load_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise StoreError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CorruptionError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CorruptionError(f"expected a JSON object in {path}")
    return data


def load_task_state(store: Path, task_id: str, *, validate: bool = True) -> dict[str, Any]:
    path = state_path(store, task_id)
    state = load_json(path)
    if validate:
        validate_state(state, where=str(path))
    if state.get("task_id") != task_id:
        raise SchemaError(f"{path}.task_id does not match directory task id {task_id!r}")
    return state


def load_json_lines(path: Path, *, tail: Optional[int] = None) -> list[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    if tail is not None:
        lines = lines[-tail:]
    result: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            result.append(record)
    return result


def append_event(task_dir_path: Path, event_type: str, **fields: Any) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise ValidationError(f"unsupported event type: {event_type}")
    event = {"ts": now_iso(), "type": event_type}
    event.update(fields)
    append_jsonl(task_dir_path / EVENTS_FILENAME, event)
    return event


# ---------------------------------------------------------------------------
# Bundled assets and rendering
# ---------------------------------------------------------------------------


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def asset_path(name: str) -> Path:
    return skill_root() / "assets" / name


def read_asset(name: str) -> str:
    path = asset_path(name)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise StoreError(f"skill asset not found: {path}") from exc


def _render(template: str, values: dict[str, str]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("{{" + key + "}}", value)
    return result


def _format_bullets(items: Iterable[str], *, none_text: str = "- (none specified)") -> str:
    items = [str(item).strip() for item in items if str(item).strip()]
    if not items:
        return none_text
    return "\n".join(f"- {item}" for item in items)


def render_task_md(
    *,
    task_id: str,
    title: str,
    objective: str,
    requirements: Iterable[str] = (),
    constraints: Iterable[str] = (),
    success_criteria: Iterable[str] = (),
    created_at: Optional[str] = None,
) -> str:
    values = {
        "task_id": task_id,
        "title": title,
        "objective": objective.strip() or "(not specified)",
        "requirements": _format_bullets(requirements),
        "constraints": _format_bullets(constraints),
        "success_criteria": _format_bullets(success_criteria),
        "created_at": created_at or now_iso(),
    }
    return _render(read_asset("TASK.md"), values)


def parse_markdown_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line.rstrip())
    for key in list(sections):
        cleaned: list[str] = []
        for line in sections[key]:
            stripped = line.strip()
            if stripped.startswith("- "):
                cleaned.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("#"):
                cleaned.append(stripped)
        sections[key] = cleaned
    return sections


def append_markdown_bullets(text: str, heading: str, bullets: Iterable[str]) -> str:
    bullets = [str(b).strip() for b in bullets if str(b).strip()]
    if not bullets:
        return text
    lines = text.splitlines()
    heading_line = f"## {heading}"
    try:
        start = lines.index(heading_line)
    except ValueError:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(heading_line)
        lines.append("")
        lines.extend(f"- {b}" for b in bullets)
        return "\n".join(lines).rstrip() + "\n"
    # Find the next level-2 heading after the target section.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    existing = []
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if stripped in {"- (none specified)", "(none specified)"}:
            continue
        if stripped:
            existing.append(line)
    insertion = list(existing)
    if insertion:
        insertion.append("")
    insertion.extend(f"- {b}" for b in bullets)
    # ensure exactly one blank line before next heading
    while insertion and insertion[-1] == "":
        insertion.pop()
    new_lines = lines[: start + 1] + [""] + insertion + [""] + lines[end:]
    return "\n".join(new_lines).rstrip() + "\n"


def replace_markdown_section(text: str, heading: str, content: str) -> str:
    """Replace the body of a level-2 Markdown section."""
    lines = text.splitlines()
    heading_line = f"## {heading}"
    try:
        start = lines.index(heading_line)
    except ValueError:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([heading_line, "", content.rstrip(), ""])
        return "\n".join(lines).rstrip() + "\n"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    new_lines = lines[: start + 1] + ["", content.rstrip(), ""] + lines[end:]
    return "\n".join(new_lines).rstrip() + "\n"


def _format_operation_line(op: dict[str, Any]) -> str:
    desc = str(op.get("description", "")).strip() or "(no description)"
    state = op.get("state", "unknown")
    summary = op.get("result_summary")
    text = f"`{op.get('id')}` — {desc} — **{state}**"
    if summary:
        text += f": {summary}"
    if state in UNRESOLVED_OPERATION_STATES:
        text += " *(inspect external state before retry; never blindly retry)*"
    return text


def render_checkpoint_md(state: dict[str, Any], *, updated_at: Optional[str] = None) -> str:
    steps = state.get("steps", [])
    completed = [s for s in steps if s.get("status") == "completed"]
    current_id = state.get("current_step")
    current = next((s for s in steps if s.get("id") == current_id), None)
    actionable = [s for s in steps if s.get("status") not in {"skipped"}]
    if current:
        position = f"Step `{current.get('id')}` — {current.get('title')} ({current.get('status')})"
    elif state.get("status") == "completed":
        position = "Task completed"
    elif steps:
        position = f"{len(completed)}/{len(actionable)} actionable steps completed; no current step set"
    else:
        position = "No steps have been created yet"

    if completed:
        completed_lines = []
        for step in completed:
            summary = step.get("summary")
            line = f"- `{step.get('id')}` — {step.get('title')}"
            if summary:
                line += f": {summary}"
            completed_lines.append(line)
        completed_text = "\n".join(completed_lines)
    else:
        completed_text = "- (nothing completed yet)"

    facts = state.get("facts") or []
    facts_text = _format_bullets([f"{f.get('summary')} ({f.get('recorded_at')})" if f.get("recorded_at") else str(f.get("summary")) for f in facts]) if facts else "- (no confirmed facts recorded yet)"
    decisions = state.get("decisions") or []
    decisions_text = _format_bullets([f"{d.get('summary')} ({d.get('recorded_at')})" if d.get("recorded_at") else str(d.get("summary")) for d in decisions]) if decisions else "- (no decisions recorded yet)"
    blockers = [b.get("summary") for b in (state.get("blockers") or []) if not b.get("resolved", False)]
    blockers_text = _format_bullets(blockers, none_text="- (no blockers)") if blockers else "- (no blockers)"

    next_actions = state.get("next_actions") or []
    next_text = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(next_actions)) if next_actions else "1. (no next actions recorded; inspect state and continue)"

    side_effects = [op for op in (state.get("operations") or []) if op.get("side_effect") and op.get("state") in {"succeeded", "failed"}]
    unresolved = [op for op in (state.get("operations") or []) if op.get("state") in UNRESOLVED_OPERATION_STATES]
    side_effects_text = _format_bullets([_format_operation_line(op) for op in side_effects]) if side_effects else "- (no completed side-effect operations)"
    unresolved_text = _format_bullets([_format_operation_line(op) for op in unresolved]) if unresolved else "- (none)"

    context = state.get("important_context") or []
    context_text = _format_bullets(context) if context else "- (none)"

    values = {
        "title": state.get("title", ""),
        "task_id": state.get("task_id", ""),
        "status": state.get("status", ""),
        "revision": str(state.get("revision", "")),
        "updated_at": updated_at or state.get("updated_at") or now_iso(),
        "objective": state.get("objective") or "(not specified)",
        "current_position": position,
        "completed": completed_text,
        "confirmed_facts": facts_text,
        "decisions": decisions_text,
        "current_step": (f"`{current.get('id')}` — {current.get('title')} ({current.get('status')})" if current else "(none)"),
        "next_actions": next_text,
        "blockers": blockers_text,
        "side_effects": side_effects_text,
        "unresolved_operations": unresolved_text,
        "important_context": context_text,
    }
    return _render(read_asset("CHECKPOINT.md"), values)


def write_checkpoint(store: Path, task_id: str, state: dict[str, Any]) -> None:
    text = render_checkpoint_md(state)
    atomic_write_text(checkpoint_path(store, task_id), text)


def write_recovery_notice(store: Path, task_id: str, error: SRError) -> Path:
    """Preserve a machine/human recovery marker without touching STATE.json."""
    base = task_dir(store, task_id)
    if not base.is_dir():
        raise TaskNotFound(f"task not found: {task_id}")
    checkpoint = base / CHECKPOINT_FILENAME
    events = base / EVENTS_FILENAME
    text = f"""# Recovery Required

- Task: `{task_id}`
- Detected at: {now_iso()}
- Problem: **state corruption**
- Error kind: `{error.kind}`
- Error: {error.message}

## Safety Rule

The damaged `STATE.json` has been preserved exactly as-is. Do not delete or
overwrite it. Use `TASK.md`, `CHECKPOINT.md`, and the tail of
`EVENTS.jsonl` to recover as much intent as possible. If safe recovery cannot
be established, mark the task blocked in a new recovered state or a separate
recovery task and report the corruption to the user.

## Files

- `TASK.md`: `{base / TASK_FILENAME}`
- `CHECKPOINT.md`: `{checkpoint}`
- `EVENTS.jsonl`: `{events}`
- damaged `STATE.json`: `{base / STATE_FILENAME}`
"""
    path = base / RECOVERY_REQUIRED_FILENAME
    atomic_write_text(path, text)
    try:
        append_event(base, "STATE_CORRUPTION_DETECTED", task_id=task_id, error=error.message)
    except (SRError, OSError):
        pass
    return path


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def empty_index() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "tasks": {}, "sessions": {}}


def _task_index_entry(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": state.get("title", ""),
        "status": state.get("status", "unknown"),
        "current_step": state.get("current_step"),
        "owner_session": state.get("owner_session"),
        "dirty": bool(state.get("dirty", False)),
        "updated_at": state.get("updated_at"),
    }


def _session_index_entry(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": session.get("status", "unknown"),
        "active_task": session.get("active_task"),
        "last_seen_at": session.get("last_seen_at"),
    }


def _read_index_unlocked(store: Path) -> dict[str, Any]:
    path = store / INDEX_FILENAME
    try:
        index = load_json(path)
        check_schema_version(index, where=str(path))
        if not isinstance(index.get("tasks"), dict) or not isinstance(index.get("sessions"), dict):
            raise SchemaError(f"{path} has invalid tasks/sessions shape")
    except FutureSchemaError:
        # Future versions must never be overwritten or downgraded.
        raise
    except (StoreError, CorruptionError):
        # Missing, invalid JSON, invalid current schema, or invalid shape is a
        # disposable cache corruption and may be rebuilt.
        return empty_index()
    return index


def rebuild_index_unlocked(store: Path) -> dict[str, Any]:
    index = empty_index()
    tasks_dir = store / "tasks"
    if tasks_dir.is_dir():
        for child in sorted(tasks_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                state = load_task_state(store, child.name, validate=True)
                index["tasks"][child.name] = _task_index_entry(state)
            except FutureSchemaError:
                raise
            except SRError as exc:
                index["tasks"][child.name] = {
                    "title": child.name,
                    "status": "corrupt",
                    "error": exc.message,
                    "updated_at": None,
                }
    sessions_dir = store / "sessions"
    if sessions_dir.is_dir():
        for child in sorted(sessions_dir.glob("*.json")):
            try:
                session = load_json(child)
                validate_session(session, where=str(child))
                if session.get("session_id") != child.stem:
                    raise SchemaError(f"session_id mismatch in {child}")
                index["sessions"][child.stem] = _session_index_entry(session)
            except FutureSchemaError:
                raise
            except SRError as exc:
                index["sessions"][child.stem] = {
                    "status": "corrupt",
                    "active_task": None,
                    "last_seen_at": None,
                    "error": exc.message,
                }
    return index


def save_index_unlocked(store: Path, index: dict[str, Any]) -> None:
    index["schema_version"] = SCHEMA_VERSION
    atomic_write_json(store / INDEX_FILENAME, index)


def ensure_store(store: Path) -> dict[str, Any]:
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    for child in ("sessions", "tasks", "archive"):
        (store / child).mkdir(parents=True, exist_ok=True)
    # Do not hold the index lock while rebuilding because rebuild reads files only.
    with ProcessLock(index_lock_path(store)):
        index_path = store / INDEX_FILENAME
        if not index_path.exists():
            index = rebuild_index_unlocked(store)
            save_index_unlocked(store, index)
            return index
        index = _read_index_unlocked(store)
        # Empty index after a missing/corrupt current-version cache is
        # rebuilt from authoritative task/session files.  A valid empty index
        # with an actually empty store is harmless to rewrite.
        if not index["tasks"] and not index["sessions"]:
            index = rebuild_index_unlocked(store)
            save_index_unlocked(store, index)
        return index


def load_index(store: Path, *, rebuild_if_missing: bool = True) -> dict[str, Any]:
    with ProcessLock(index_lock_path(store)):
        path = store / INDEX_FILENAME
        if not path.exists():
            if not rebuild_if_missing:
                raise StoreError(f"missing index: {path}")
            index = rebuild_index_unlocked(store)
            save_index_unlocked(store, index)
            return index
        try:
            index = _read_index_unlocked(store)
        except FutureSchemaError:
            raise
        # A corrupt/empty index is a discovery cache; rebuild from authoritative files.
        if not index["tasks"] and not index["sessions"]:
            rebuilt = rebuild_index_unlocked(store)
            if rebuilt["tasks"] or rebuilt["sessions"]:
                save_index_unlocked(store, rebuilt)
                return rebuilt
        return index


def update_index_task(store: Path, task_id: str, state: dict[str, Any]) -> None:
    with ProcessLock(index_lock_path(store)):
        index = _read_index_unlocked(store)
        index.setdefault("tasks", {})[task_id] = _task_index_entry(state)
        save_index_unlocked(store, index)


def update_index_session(store: Path, session_id: str, session: dict[str, Any]) -> None:
    with ProcessLock(index_lock_path(store)):
        index = _read_index_unlocked(store)
        index.setdefault("sessions", {})[session_id] = _session_index_entry(session)
        save_index_unlocked(store, index)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def find_session_by_native_id(
    store: Path,
    native_session_id: str,
    *,
    tolerate_corrupt_session_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return valid reliability sessions matching a runtime-native session id.

    Scans session files directly; index.json is not authoritative.  A
    parseable session file that claims the requested native id is never
    silently ignored just because other fields are corrupt.  Completely
    unreadable JSON cannot contribute a reliable native-id claim and is skipped.

    ``tolerate_corrupt_session_id`` is used only for explicit same-session
    recovery: if a corrupt claimant's session_id equals that value, lookup
    ignores it so the caller can back up and recreate the same session id.
    """
    matches: list[dict[str, Any]] = []
    sessions_dir = store / "sessions"
    if not sessions_dir.is_dir():
        return matches
    for child in sorted(sessions_dir.glob("*.json")):
        try:
            session = load_json(child)
        except StoreError:
            continue
        except CorruptionError:
            # Completely unreadable JSON cannot contribute a reliable native-id
            # claim.  Do not regex-scan it.
            continue

        version = session.get("schema_version")
        if isinstance(version, int) and not isinstance(version, bool) and version > SCHEMA_VERSION:
            raise FutureSchemaError(
                f"{child} uses schema_version {version}, but this skill only supports up to {SCHEMA_VERSION}"
            )

        if session.get("native_session_id") != native_session_id:
            continue

        try:
            validate_session(session, where=str(child))
            if session.get("session_id") != child.stem:
                raise SchemaError(f"session_id mismatch in {child}")
        except FutureSchemaError:
            raise
        except SRError as exc:
            claimant_session_id = session.get("session_id")
            if tolerate_corrupt_session_id is not None and claimant_session_id == tolerate_corrupt_session_id:
                continue
            raise SessionIdentityCorruption(
                f"session {child} claims native_session_id {native_session_id!r} but its metadata is corrupt",
                details={
                    "session_file": str(child),
                    "session_id": claimant_session_id,
                    "native_session_id": native_session_id,
                    "error": exc.message,
                },
            ) from exc
        matches.append(session)
    return matches


def assert_native_session_id_available(
    store: Path,
    native_session_id: str,
    *,
    current_session_id: Optional[str] = None,
) -> None:
    """Enforce one native_session_id -> one reliability session.

    A null binding may be set to X, an existing X may be refreshed by the same
    session, but X may not be silently rebound to Y and one native id may not
    map to multiple reliability sessions.
    """
    matches = find_session_by_native_id(
        store,
        native_session_id,
        tolerate_corrupt_session_id=current_session_id,
    )
    if not matches:
        return
    if len(matches) == 1 and matches[0].get("session_id") == current_session_id:
        return
    raise SessionIdentityConflict(
        f"native_session_id {native_session_id!r} is already bound to another reliability session",
        details={
            "native_session_id": native_session_id,
            "current_session_id": current_session_id,
            "existing_session_ids": [match.get("session_id") for match in matches],
        },
    )


def create_or_touch_session(
    store: Path,
    *,
    session_id: Optional[str] = None,
    native_session_id: Optional[str] = None,
) -> tuple[dict[str, Any], bool]:
    """Create or touch a reliability session with atomic native-id binding.

    Native-session-id uniqueness and binding mutations are serialized by a
    store-level identity lock.  Lock order is identity -> session -> index.
    """
    store = Path(store)
    with ProcessLock(session_identity_lock_path(store)):
        return _create_or_touch_session_locked(
            store,
            session_id=session_id,
            native_session_id=native_session_id,
        )


def _create_or_touch_session_locked(
    store: Path,
    *,
    session_id: Optional[str] = None,
    native_session_id: Optional[str] = None,
) -> tuple[dict[str, Any], bool]:
    if session_id is None and native_session_id is not None:
        matches = find_session_by_native_id(store, native_session_id)
        if len(matches) > 1:
            raise SessionIdentityConflict(
                f"multiple reliability sessions claim native_session_id {native_session_id!r}",
                details={
                    "native_session_id": native_session_id,
                    "session_ids": [match.get("session_id") for match in matches],
                },
            )
        if len(matches) == 1:
            session_id = matches[0].get("session_id")

    sid = session_id or generate_session_id()
    validate_id_component(sid, field="session_id")

    if native_session_id is not None:
        assert_native_session_id_available(
            store,
            native_session_id,
            current_session_id=sid,
        )

    path = session_path(store, sid)
    created = False
    with ProcessLock(session_lock_path(store, sid)):
        if path.exists():
            try:
                session = load_json(path)
                validate_session(session, where=str(path))
                if session.get("session_id") != sid:
                    raise SchemaError(f"session_id mismatch in {path}")
                if native_session_id is not None:
                    existing_native = session.get("native_session_id")
                    if existing_native not in (None, native_session_id):
                        raise SessionIdentityConflict(
                            f"session {sid} already has native_session_id {existing_native!r}; "
                            f"refusing to rebind it to {native_session_id!r}",
                            details={
                                "session_id": sid,
                                "existing_native_session_id": existing_native,
                                "requested_native_session_id": native_session_id,
                            },
                        )
                    session["native_session_id"] = native_session_id
                session["last_seen_at"] = now_iso()
                if session.get("status") in {"expired", "closed"}:
                    session["status"] = "active"
            except FutureSchemaError:
                raise
            except SessionIdentityConflict:
                raise
            except SRError:
                # If the corrupt file is still parseable enough to declare a
                # native identity or a different session id, do not silently
                # steal or rebind it during recovery.
                existing_native: Optional[str] = None
                existing_session_id: Optional[str] = None
                try:
                    raw = load_json(path)
                    if isinstance(raw, dict):
                        existing_native = raw.get("native_session_id")
                        existing_session_id = raw.get("session_id")
                except SRError:
                    existing_native = None
                    existing_session_id = None

                if existing_session_id is not None and existing_session_id != sid:
                    raise SessionIdentityCorruption(
                        f"session file {path} declares session_id {existing_session_id!r}, not {sid!r}",
                        details={
                            "session_file": str(path),
                            "declared_session_id": existing_session_id,
                            "requested_session_id": sid,
                        },
                    )
                if native_session_id is not None and existing_native not in (None, native_session_id):
                    raise SessionIdentityConflict(
                        f"session {sid} already declares native_session_id {existing_native!r}; "
                        f"refusing to rebind it to {native_session_id!r}",
                        details={
                            "session_id": sid,
                            "existing_native_session_id": existing_native,
                            "requested_native_session_id": native_session_id,
                        },
                    )

                backup = path.with_name(f"{path.name}.corrupt.{_timestamp_compact()}.bak")
                with contextlib.suppress(OSError):
                    shutil.copy2(path, backup)
                session = default_session(session_id=sid, native_session_id=native_session_id)
                created = True
        else:
            session = default_session(session_id=sid, native_session_id=native_session_id)
            created = True
        atomic_write_json(path, session)

    # Identity lock remains held through index update, but the individual
    # session lock has already been released.
    update_index_session(store, sid, session)
    return session, created


def load_session(store: Path, session_id: str) -> dict[str, Any]:
    path = session_path(store, session_id)
    session = load_json(path)
    validate_session(session, where=str(path))
    if session.get("session_id") != session_id:
        raise SchemaError(f"session_id mismatch in {path}")
    return session


def set_session_active_task(store: Path, session_id: str, task_id: Optional[str]) -> dict[str, Any]:
    path = session_path(store, session_id)
    with ProcessLock(session_lock_path(store, session_id)):
        if path.exists():
            session = load_json(path)
            validate_session(session, where=str(path))
            if session.get("session_id") != session_id:
                raise SchemaError(f"session_id mismatch in {path}")
        else:
            session = default_session(session_id=session_id)
        session["active_task"] = task_id
        session["last_seen_at"] = now_iso()
        if session.get("status") == "expired":
            session["status"] = "active"
        atomic_write_json(path, session)
    update_index_session(store, session_id, session)
    return session


def clear_session_task_binding_if_matches(
    store: Path,
    session_id: str,
    task_id: str,
) -> Optional[dict[str, Any]]:
    """Clear session.active_task only when it currently points at task_id.

    This is task-binding cleanup, not session invalidation.  Session status and
    last_seen_at are intentionally left unchanged.  Future-schema session files
    are never modified; FutureSchemaError propagates to the caller.
    """
    validate_id_component(session_id, field="session_id")
    validate_id_component(task_id, field="task_id")
    path = session_path(store, session_id)
    if not path.exists():
        return None
    with ProcessLock(session_lock_path(store, session_id)):
        session = load_json(path)
        validate_session(session, where=str(path))
        if session.get("session_id") != session_id:
            raise SchemaError(f"session_id mismatch in {path}")
        if session.get("active_task") != task_id:
            return session
        session["active_task"] = None
        atomic_write_json(path, session)
    update_index_session(store, session_id, session)
    return session


def retire_session(store: Path, session_id: str) -> None:
    """Explicitly invalidate an entire reliability session.

    This is not called by ordinary task takeover.  Task ownership transfer only
    unbinds the specific task and never marks the previous session expired.
    """
    validate_id_component(session_id, field="session_id")
    path = session_path(store, session_id)
    if not path.exists():
        return
    with ProcessLock(session_lock_path(store, session_id)):
        session = load_json(path)
        validate_session(session, where=str(path))
        if session.get("session_id") != session_id:
            raise SchemaError(f"session_id mismatch in {path}")
        session["status"] = "expired"
        session["active_task"] = None
        session["last_seen_at"] = now_iso()
        atomic_write_json(path, session)
    update_index_session(store, session_id, session)


# ---------------------------------------------------------------------------
# Task mutation
# ---------------------------------------------------------------------------


def create_task(
    store: Path,
    *,
    title: str,
    objective: str,
    requirements: Iterable[str] = (),
    constraints: Iterable[str] = (),
    success_criteria: Iterable[str] = (),
    task_id: Optional[str] = None,
    session_id: Optional[str] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any]:
    store = Path(store)
    ensure_store(store)
    title = (title or "").strip()
    objective = (objective or "").strip()
    if not title:
        raise ValidationError("title is required")
    if not objective:
        raise ValidationError("objective is required")
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
        raise ValidationError("lease_seconds must be a positive integer")

    base_id = task_id or generate_task_id(title)
    validate_id_component(base_id, field="task_id")
    chosen = base_id
    if task_id is None:
        counter = 2
        while task_dir(store, chosen).exists():
            chosen = f"{base_id}-{counter}"
            counter += 1
    elif task_dir(store, chosen).exists():
        raise ValidationError(f"task already exists: {chosen}")

    path = task_dir(store, chosen)
    path.mkdir(parents=True, exist_ok=False)
    try:
        with ProcessLock(task_lock_path(store, chosen)):
            owner = session_id
            lease = lease_expiry_from_now(lease_seconds) if owner else None
            state = default_state(
                task_id=chosen,
                title=title,
                objective=objective,
                status="pending" if not session_id else "in_progress",
                owner_session=owner,
                lease_expires_at=lease,
                lease_duration_seconds=lease_seconds,
            )
            state["requirements"] = [str(x).strip() for x in requirements if str(x).strip()]
            state["constraints"] = [str(x).strip() for x in constraints if str(x).strip()]
            state["success_criteria"] = [str(x).strip() for x in success_criteria if str(x).strip()]
            task_md = render_task_md(
                task_id=chosen,
                title=title,
                objective=objective,
                requirements=state["requirements"],
                constraints=state["constraints"],
                success_criteria=state["success_criteria"],
                created_at=state["created_at"],
            )
            checkpoint_md = render_checkpoint_md(state)
            atomic_write_text(path / TASK_FILENAME, task_md)
            atomic_write_text(path / CHECKPOINT_FILENAME, checkpoint_md)
            (path / EVENTS_FILENAME).touch(exist_ok=True)
            atomic_write_json(path / STATE_FILENAME, state)
            append_event(path, "TASK_CREATED", task_id=chosen, title=title, owner_session=owner)
            if session_id:
                set_session_active_task(store, session_id, chosen)
                append_event(path, "TASK_ATTACHED", session_id=session_id)
                append_event(path, "SESSION_ATTACHED", session_id=session_id)
            update_index_task(store, chosen, state)
            return state
    except Exception:
        # A failed creation should not leave a half-created durable task.
        with contextlib.suppress(Exception):
            shutil.rmtree(path, ignore_errors=True)
        raise


def mutate_task(
    store: Path,
    task_id: str,
    mutation: Callable[[dict[str, Any]], Optional[list[tuple[str, dict[str, Any]]]]],
    *,
    expected_revision: Optional[int] = None,
    events: Optional[list[tuple[str, dict[str, Any]]]] = None,
    session_id: Optional[str] = None,
    enforce_lease: bool = True,
    renew_lease: bool = True,
    touch_session: bool = True,
    takeover_cleanup: bool = False,
    lease_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """Apply an in-place state mutation under the per-task lock.

    Ordinary task mutations enforce the current owner/lease guard.  Owner
    changes must use ``enforce_lease=False`` internally via explicit
    attach/resume/takeover flows, never through ordinary mutation callbacks.

    ``mutation`` receives the currently loaded state and may mutate it.  It may
    return a list of event tuples to append after the state is written.  The
    revision is incremented by exactly one per successful mutation.
    """
    store = Path(store)
    path = task_dir(store, task_id)
    if not path.is_dir():
        raise TaskNotFound(f"task not found: {task_id}")
    with ProcessLock(task_lock_path(store, task_id)):
        state = load_task_state(store, task_id)
        if expected_revision is not None and state.get("revision") != expected_revision:
            raise RevisionConflict(
                f"revision conflict for {task_id}: expected {expected_revision}, found {state.get('revision')}",
                details={"expected": expected_revision, "found": state.get("revision")},
            )

        owner_before = state.get("owner_session")
        if enforce_lease:
            validate_task_ownership(state, task_id=task_id, session_id=session_id)

        before = state.get("revision")
        returned_events = mutation(state)
        if enforce_lease and state.get("owner_session") != owner_before:
            raise ValidationError("ordinary task mutation cannot change owner_session")

        all_events = list(events or [])
        if returned_events:
            all_events.extend(returned_events)

        if renew_lease and enforce_lease and session_id and state.get("owner_session") == session_id:
            duration = lease_seconds if lease_seconds is not None else get_lease_duration_seconds(state)
            state["lease_duration_seconds"] = duration
            state["lease_expires_at"] = lease_expiry_from_now(duration)

        validate_state(state, where=str(path / STATE_FILENAME))
        state["revision"] = before + 1
        state["updated_at"] = now_iso()
        atomic_write_json(path / STATE_FILENAME, state)
        for event_type, fields in all_events:
            append_event(path, event_type, task_id=task_id, **fields)
        write_checkpoint(store, task_id, state)
        update_index_task(store, task_id, state)
        # Session metadata is convenience data.  Task STATE.json is already
        # authoritative, so metadata updates below are best-effort and never
        # roll back the task write.
        if touch_session and session_id:
            with contextlib.suppress(SRError, OSError):
                set_session_active_task(store, session_id, task_id)
        if takeover_cleanup and owner_before and owner_before != session_id:
            with contextlib.suppress(SRError, OSError):
                clear_session_task_binding_if_matches(store, owner_before, task_id)
        return state


# ---------------------------------------------------------------------------
# Index / discovery / listing
# ---------------------------------------------------------------------------


def _state_sort_key(task: dict[str, Any]) -> tuple[int, str]:
    status_rank = {
        "in_progress": 0,
        "blocked": 1,
        "paused": 2,
        "pending": 3,
        "completed": 4,
        "failed": 5,
        "abandoned": 6,
    }
    return (status_rank.get(str(task.get("status")), 9), str(task.get("updated_at") or ""))


def list_tasks(
    store: Path,
    *,
    statuses: Optional[Iterable[str]] = None,
    unfinished_only: bool = False,
) -> list[dict[str, Any]]:
    """Return task summaries, scanning task directories (index is not authority)."""
    store = Path(store)
    ensure_store(store)
    wanted = set(statuses or [])
    tasks: list[dict[str, Any]] = []
    tasks_dir = store / "tasks"
    if tasks_dir.is_dir():
        for child in sorted(tasks_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                state = load_task_state(store, child.name, validate=True)
            except FutureSchemaError:
                raise
            except SRError as exc:
                if not wanted or unfinished_only or "corrupt" in wanted:
                    tasks.append(
                        {
                            "task_id": child.name,
                            "title": child.name,
                            "objective": "",
                            "status": "corrupt",
                            "current_step": None,
                            "owner_session": None,
                            "dirty": False,
                            "updated_at": None,
                            "error": exc.message,
                        }
                    )
                continue
            status = state.get("status", "unknown")
            if unfinished_only and status not in UNFINISHED_STATUSES:
                continue
            if wanted and status not in wanted:
                continue
            tasks.append(
                {
                    "task_id": state.get("task_id"),
                    "title": state.get("title", ""),
                    "objective": state.get("objective", ""),
                    "status": status,
                    "current_step": state.get("current_step"),
                    "owner_session": state.get("owner_session"),
                    "dirty": bool(state.get("dirty", False)),
                    "updated_at": state.get("updated_at"),
                }
            )
    tasks.sort(key=_state_sort_key)
    return tasks


def find_resumable_tasks(store: Path) -> list[dict[str, Any]]:
    return list_tasks(store, unfinished_only=True)


def choose_task(store: Path, task_id: Optional[str] = None, *, latest: bool = False) -> str:
    if task_id:
        validate_id_component(task_id, field="task_id")
        if not task_dir(store, task_id).is_dir():
            raise TaskNotFound(f"task not found: {task_id}")
        return task_id
    candidates = find_resumable_tasks(store)
    if latest:
        if not candidates:
            raise TaskNotFound("no unfinished task available")
        # _state_sort_key uses an ascending string, so sort explicitly here.
        # The task id is the secondary key so the ranking is deterministic.
        candidates.sort(
            key=lambda item: (
                str(item.get("updated_at") or ""),
                str(item.get("task_id") or ""),
            ),
            reverse=True,
        )
        # Timestamps have second granularity, so two tasks touched within the
        # same second are indistinguishable.  Never break such a tie silently:
        # that can resume a different task than the one just worked on.  Present
        # the candidates instead, matching the "do not randomly bind" rule.
        newest = str(candidates[0].get("updated_at") or "")
        tied = [
            str(item.get("task_id"))
            for item in candidates
            if str(item.get("updated_at") or "") == newest
        ]
        if len(tied) > 1:
            raise ValidationError(
                "multiple unfinished tasks share the most recent update time; "
                "specify --task",
                details={"candidates": tied, "updated_at": newest},
            )
        return str(candidates[0]["task_id"])
    if len(candidates) == 1:
        return str(candidates[0]["task_id"])
    if not candidates:
        raise TaskNotFound("no unfinished task available")
    raise ValidationError(
        "multiple unfinished tasks; specify --task or --latest",
        details={"candidates": [c["task_id"] for c in candidates]},
    )


def recovery_info(store: Path, task_id: str) -> dict[str, Any]:
    """Collect human/machine recovery hints without mutating task data."""
    base = task_dir(store, task_id)
    info: dict[str, Any] = {
        "task_id": task_id,
        "task_dir": str(base),
        "state_path": str(base / STATE_FILENAME),
        "task_path": str(base / TASK_FILENAME),
        "checkpoint_path": str(base / CHECKPOINT_FILENAME),
        "events_path": str(base / EVENTS_FILENAME),
        "state_readable": False,
        "task_md_present": (base / TASK_FILENAME).exists(),
        "checkpoint_present": (base / CHECKPOINT_FILENAME).exists(),
        "recovery_required_path": str(base / RECOVERY_REQUIRED_FILENAME),
        "recovery_required_present": (base / RECOVERY_REQUIRED_FILENAME).exists(),
        "events_count": 0,
        "last_events": [],
    }
    try:
        load_json(base / STATE_FILENAME)
        info["state_readable"] = True
    except SRError as exc:
        info["state_error"] = exc.message
    events = load_json_lines(base / EVENTS_FILENAME)
    info["events_count"] = len(events)
    info["last_events"] = events[-10:]
    return info


# ---------------------------------------------------------------------------
# Resume / lease / takeover
# ---------------------------------------------------------------------------


def _reconcile_dirty_and_operations(
    state: dict[str, Any],
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """Normalize dirty state after recovery.

    A running/planned operation found during resume is conservatively changed to
    outcome_unknown.  It is never marked successful automatically.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    warnings: list[str] = []
    ops = state.setdefault("operations", [])
    unresolved: list[dict[str, Any]] = []
    for op in ops:
        if op.get("state") in {"planned", "running"}:
            old_state = op.get("state")
            op["state"] = "outcome_unknown"
            op["completed_at"] = op.get("completed_at") or now_iso()
            op["result_summary"] = op.get("result_summary") or (
                "Unknown: session ended while operation was running; external state must be inspected."
            )
            events.append(
                (
                    "OPERATION_OUTCOME_UNKNOWN",
                    {
                        "operation_id": op.get("id"),
                        "previous_state": old_state,
                        "reason": "resume found an unresolved operation",
                    },
                )
            )
            warnings.append(
                f"Operation {op.get('id')} had state {old_state!r}; marked outcome_unknown. "
                "Inspect external state before deciding whether to retry."
            )
        if op.get("state") in UNRESOLVED_OPERATION_STATES:
            unresolved.append(op)
    if unresolved:
        state["dirty"] = True
        state["active_operation"] = unresolved[0].get("id")
        warnings.append(
            "Task is dirty with unresolved operation(s). Inspect external state, then call finish-operation."
        )
    else:
        state["active_operation"] = None
        state["dirty"] = False
    return events, warnings


def resume_task(
    store: Path,
    task_id: str,
    session_id: str,
    *,
    force_takeover: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Resume or take over a task under its lock.

    Attach, expired-lease takeover, and force takeover all use the same
    ownership-transfer helper.  A task takeover only transfers this task; it
    never marks the previous reliability session expired.
    """
    store = Path(store)
    ensure_store(store)
    path = task_dir(store, task_id)
    if not path.is_dir():
        raise TaskNotFound(f"task not found: {task_id}")

    warnings: list[str] = []

    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        lease_duration = get_lease_duration_seconds(state)
        events = apply_task_ownership_transfer(
            state,
            task_id=task_id,
            session_id=session_id,
            force=force_takeover,
            lease_duration_seconds=lease_duration,
        )
        dirty_events, dirty_warnings = _reconcile_dirty_and_operations(state)
        warnings.extend(dirty_warnings)
        return events + dirty_events

    state = mutate_task(
        store,
        task_id,
        mutation,
        session_id=session_id,
        enforce_lease=False,
        renew_lease=False,
        touch_session=True,
        takeover_cleanup=True,
    )
    return state, warnings


# ---------------------------------------------------------------------------
# Resume payload
# ---------------------------------------------------------------------------


def build_resume_payload(
    store: Path,
    task_id: str,
    session_id: str,
    state: dict[str, Any],
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    base = task_dir(store, task_id)
    task_md = ""
    checkpoint_md = ""
    with contextlib.suppress(FileNotFoundError):
        task_md = (base / TASK_FILENAME).read_text(encoding="utf-8")
    with contextlib.suppress(FileNotFoundError):
        checkpoint_md = (base / CHECKPOINT_FILENAME).read_text(encoding="utf-8")
    return {
        "session_id": session_id,
        "task_id": task_id,
        "title": state.get("title", ""),
        "objective": state.get("objective", ""),
        "status": state.get("status"),
        "revision": state.get("revision"),
        "current_step": state.get("current_step"),
        "steps": state.get("steps", []),
        "next_actions": state.get("next_actions", []),
        "dirty": bool(state.get("dirty", False)),
        "active_operation": state.get("active_operation"),
        "operations": state.get("operations", []),
        "owner_session": state.get("owner_session"),
        "lease_expires_at": state.get("lease_expires_at"),
        "checkpoint_path": str(base / CHECKPOINT_FILENAME),
        "task_path": str(base),
        "task_md_path": str(base / TASK_FILENAME),
        "state_path": str(base / STATE_FILENAME),
        "events_path": str(base / EVENTS_FILENAME),
        "warnings": list(warnings),
        "recovery_instructions": [
            "Read TASK.md for durable objective/requirements/constraints/success criteria.",
            "Read CHECKPOINT.md for the compact recovery summary.",
            "Read STATE.json for authoritative machine-readable current state.",
            "If dirty or active_operation is unresolved, inspect external state before any retry.",
            "Never blindly retry an operation whose outcome is unknown.",
        ],
        "task_md": task_md,
        "checkpoint_md": checkpoint_md,
    }


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def make_common_parser() -> "argparse.ArgumentParser":
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--workspace", metavar="PATH", default=argparse.SUPPRESS,
                        help="workspace root (default: cwd or SESSION_RELIABILITY_WORKSPACE)")
    parser.add_argument("--store", metavar="PATH", default=argparse.SUPPRESS,
                        help="store path (default: workspace/.agents/store/session-reliability)")
    parser.add_argument("--task", dest="task_id", metavar="TASK_ID", default=argparse.SUPPRESS,
                        help="task id")
    parser.add_argument("--session-id", dest="session_id", metavar="SESSION_ID", default=argparse.SUPPRESS,
                        help="reliability session id")
    parser.add_argument("--expected-revision", type=int, default=argparse.SUPPRESS,
                        help="optional optimistic-concurrency revision guard")
    return parser


def arg_value(args: Any, name: str, default: Any = None) -> Any:
    return getattr(args, name, default)


def cli_context(args: Any) -> tuple[Path, Path, Optional[str], Optional[str]]:
    workspace = resolve_workspace(arg_value(args, "workspace"))
    store = resolve_store(arg_value(args, "store"), workspace=workspace)
    session_id = arg_value(args, "session_id")
    task_id = arg_value(args, "task_id")
    return workspace, store, session_id, task_id


def emit_json(data: Any) -> None:
    import sys

    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


def cli_error(exc: SRError) -> None:
    import sys

    sys.stderr.write(f"[{exc.kind}] {exc.message}\n")
    if exc.details:
        sys.stderr.write(json.dumps(exc.details, ensure_ascii=False, indent=2) + "\n")
    sys.stderr.flush()


