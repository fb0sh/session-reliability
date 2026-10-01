#!/usr/bin/env python3
"""Durable state mutation commands for session-reliability.

Every mutating subcommand takes the per-task lock, validates the on-disk
revision, applies the mutation, atomically writes STATE.json, appends events,
re-renders CHECKPOINT.md, and refreshes index.json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_task(args: argparse.Namespace) -> str:
    task_id = lib.arg_value(args, "task_id")
    if not task_id:
        raise lib.ValidationError("--task TASK_ID is required for this subcommand")
    return str(task_id)


def _require_session(args: argparse.Namespace) -> str:
    session_id = lib.arg_value(args, "session_id")
    if not session_id:
        raise lib.ValidationError("--session-id SESSION_ID is required for this subcommand")
    return str(session_id)


def _next_pending_step(state: dict[str, Any], *, after_id: Optional[str] = None) -> Optional[str]:
    for step in state.get("steps", []):
        if step.get("id") == after_id:
            continue
        if step.get("status") == "pending":
            return str(step.get("id"))
    for step in state.get("steps", []):
        if step.get("status") == "in_progress" and step.get("id") != after_id:
            return str(step.get("id"))
    return None


def _operation_by_id(state: dict[str, Any], operation_id: str) -> dict[str, Any]:
    for op in state.get("operations", []):
        if op.get("id") == operation_id:
            return op
    raise lib.ValidationError(f"operation not found: {operation_id}")


def _resolve_active_operation(state: dict[str, Any], explicit_id: Optional[str]) -> Optional[str]:
    if explicit_id:
        return explicit_id
    active = state.get("active_operation")
    if active:
        return str(active)
    unresolved = [op.get("id") for op in state.get("operations", []) if op.get("state") in lib.UNRESOLVED_OPERATION_STATES]
    if len(unresolved) == 1:
        return str(unresolved[0])
    return None


def _emit_state(state: dict[str, Any], store: Path) -> None:
    task_id = state.get("task_id")
    base = lib.task_dir(store, task_id)
    lib.emit_json(
        {
            "task_id": task_id,
            "title": state.get("title"),
            "status": state.get("status"),
            "revision": state.get("revision"),
            "current_step": state.get("current_step"),
            "next_actions": state.get("next_actions", []),
            "dirty": bool(state.get("dirty", False)),
            "active_operation": state.get("active_operation"),
            "owner_session": state.get("owner_session"),
            "lease_expires_at": state.get("lease_expires_at"),
            "task_path": str(base),
            "task_md_path": str(base / lib.TASK_FILENAME),
            "state_path": str(base / lib.STATE_FILENAME),
            "checkpoint_path": str(base / lib.CHECKPOINT_FILENAME),
            "events_path": str(base / lib.EVENTS_FILENAME),
        }
    )


def _mutate(
    args: argparse.Namespace,
    mutation: Callable[[dict[str, Any]], Optional[list[tuple[str, dict[str, Any]]]]],
    *,
    enforce_lease: bool = True,
    renew_lease: bool = True,
    touch_session: bool = True,
    takeover_cleanup: bool = False,
    lease_seconds: Optional[int] = None,
) -> dict[str, Any]:
    workspace, store, session_id, task_id = lib.cli_context(args)
    lib.ensure_store(store)
    task_id = _require_task(args)
    expected = lib.arg_value(args, "expected_revision")
    return lib.mutate_task(
        store,
        task_id,
        mutation,
        expected_revision=expected,
        session_id=session_id,
        enforce_lease=enforce_lease,
        renew_lease=renew_lease,
        touch_session=touch_session,
        takeover_cleanup=takeover_cleanup,
        lease_seconds=lease_seconds,
    )


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_create_task(args: argparse.Namespace) -> int:
    workspace, store, session_id, inherited_task_id = lib.cli_context(args)
    task_id = getattr(args, "new_task_id", None) or inherited_task_id
    lib.ensure_store(store)
    if session_id:
        lib.create_or_touch_session(store, session_id=session_id)
    state = lib.create_task(
        store,
        title=args.title,
        objective=args.objective,
        requirements=args.requirement or [],
        constraints=args.constraint or [],
        success_criteria=args.success_criterion or [],
        task_id=task_id,
        session_id=session_id,
        lease_seconds=args.lease_seconds,
    )
    _emit_state(state, store)
    return 0


def cmd_set_status(args: argparse.Namespace) -> int:
    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        old = state.get("status")
        new = args.status
        if new == "completed":
            unresolved = [op for op in state.get("operations", []) if op.get("state") in lib.UNRESOLVED_OPERATION_STATES]
            if state.get("dirty") or unresolved:
                raise lib.ValidationError(
                    "cannot complete task while dirty or unresolved operations exist; inspect and finish them first"
                )
        state["status"] = new
        if new == "completed":
            state["current_step"] = None
            state["next_actions"] = []
        event_fields: dict[str, Any] = {"from": old, "to": new}
        if args.note:
            state.setdefault("important_context", []).append(f"Status note: {args.note}")
            event_fields["note"] = args.note
        events: list[tuple[str, dict[str, Any]]] = [("TASK_STATUS_CHANGED", event_fields)]
        if new in lib.STATUS_EVENT:
            events.append((lib.STATUS_EVENT[new], event_fields))
        return events

    _emit_state(_mutate(args, mutation), lib.resolve_store(lib.arg_value(args, "store"), workspace=lib.resolve_workspace(lib.arg_value(args, "workspace"))))
    return 0


def cmd_add_step(args: argparse.Namespace) -> int:
    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        step_id = args.step_id or lib.next_step_id(state)
        if any(step.get("id") == step_id for step in state.get("steps", [])):
            raise lib.ValidationError(f"step already exists: {step_id}")
        step = {
            "id": step_id,
            "title": args.title,
            "status": args.status,
            "summary": None,
            "created_at": lib.now_iso(),
            "started_at": None,
            "completed_at": None,
        }
        state.setdefault("steps", []).append(step)
        if state.get("current_step") is None:
            state["current_step"] = step_id
        return [("STEP_ADDED", {"step_id": step_id, "title": args.title, "status": args.status})]

    _emit_state(_mutate(args, mutation), lib.resolve_store(lib.arg_value(args, "store"), workspace=lib.resolve_workspace(lib.arg_value(args, "workspace"))))
    return 0


def _step_mutation(
    args: argparse.Namespace,
    *,
    status: str,
    event_type: str,
    set_summary: bool = False,
) -> Callable[[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    step_id = args.step_id

    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        target = None
        for step in state.get("steps", []):
            if step.get("id") == step_id:
                target = step
                break
        if target is None:
            raise lib.ValidationError(f"step not found: {step_id}")
        if status == "in_progress":
            other = next((s for s in state.get("steps", []) if s.get("status") == "in_progress" and s.get("id") != step_id), None)
            if other is not None:
                raise lib.ValidationError(
                    f"step {other.get('id')} is already in_progress; complete, fail, or block it before starting another step"
                )
        target["status"] = status
        if status == "in_progress":
            target["started_at"] = target.get("started_at") or lib.now_iso()
            state["current_step"] = step_id
        elif status in {"completed", "failed", "blocked", "skipped"}:
            target["completed_at"] = lib.now_iso()
            if set_summary and getattr(args, "summary", None):
                target["summary"] = args.summary
            if status == "blocked":
                reason = getattr(args, "reason", None) or getattr(args, "summary", None) or "Step blocked"
                state.setdefault("blockers", []).append(
                    {"summary": f"{step_id}: {reason}", "recorded_at": lib.now_iso(), "resolved": False}
                )
            if state.get("current_step") == step_id:
                state["current_step"] = _next_pending_step(state, after_id=step_id)
        return [(event_type, {"step_id": step_id, "summary": target.get("summary"), "reason": getattr(args, "reason", None)})]

    return mutation


def cmd_start_step(args: argparse.Namespace) -> int:
    _emit_state(_mutate(args, _step_mutation(args, status="in_progress", event_type="STEP_STARTED")), _store_from_args(args))
    return 0


def cmd_complete_step(args: argparse.Namespace) -> int:
    _emit_state(_mutate(args, _step_mutation(args, status="completed", event_type="STEP_COMPLETED", set_summary=True)), _store_from_args(args))
    return 0


def cmd_fail_step(args: argparse.Namespace) -> int:
    _emit_state(_mutate(args, _step_mutation(args, status="failed", event_type="STEP_FAILED", set_summary=True)), _store_from_args(args))
    return 0


def cmd_block_step(args: argparse.Namespace) -> int:
    _emit_state(_mutate(args, _step_mutation(args, status="blocked", event_type="STEP_BLOCKED", set_summary=True)), _store_from_args(args))
    return 0


def cmd_set_current_step(args: argparse.Namespace) -> int:
    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if args.clear:
            state["current_step"] = None
        else:
            if not args.step_id:
                raise lib.ValidationError("--step is required unless --clear is used")
            ids = {step.get("id") for step in state.get("steps", [])}
            if args.step_id not in ids:
                raise lib.ValidationError(f"step not found: {args.step_id}")
            state["current_step"] = args.step_id
        return [("CURRENT_STEP_CHANGED", {"current_step": state.get("current_step")})]

    _emit_state(_mutate(args, mutation), _store_from_args(args))
    return 0


def cmd_set_next_actions(args: argparse.Namespace) -> int:
    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if args.clear:
            state["next_actions"] = []
        elif args.action:
            state["next_actions"] = list(args.action)
        else:
            raise lib.ValidationError("provide at least one --action, or use --clear")
        return [("NEXT_ACTIONS_SET", {"next_actions": state.get("next_actions", [])})]

    _emit_state(_mutate(args, mutation), _store_from_args(args))
    return 0


def cmd_record_finding(args: argparse.Namespace) -> int:
    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        record = {"summary": args.summary, "recorded_at": lib.now_iso()}
        state.setdefault("facts", []).append(record)
        return [("FINDING_RECORDED", {"summary": args.summary})]

    _emit_state(_mutate(args, mutation), _store_from_args(args))
    return 0


def cmd_record_decision(args: argparse.Namespace) -> int:
    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        record = {"summary": args.summary, "recorded_at": lib.now_iso()}
        state.setdefault("decisions", []).append(record)
        return [("DECISION_RECORDED", {"summary": args.summary})]

    _emit_state(_mutate(args, mutation), _store_from_args(args))
    return 0


def cmd_update_requirements(args: argparse.Namespace) -> int:
    if not (args.objective or args.requirement or args.constraint or args.success_criterion):
        raise lib.ValidationError(
            "provide at least one of --objective, --requirement, --constraint, or --success-criterion"
        )
    workspace, store, session_id, task_id = lib.cli_context(args)
    lib.ensure_store(store)
    task_id = _require_task(args)

    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        md_path = lib.task_markdown_path(store, task_id)
        text = md_path.read_text(encoding="utf-8")
        counts: dict[str, int] = {"requirements": 0, "constraints": 0, "success_criteria": 0}
        if args.objective:
            state["objective"] = args.objective
            text = lib.replace_markdown_section(text, "Objective", args.objective)
        if args.requirement:
            state.setdefault("requirements", []).extend(args.requirement)
            text = lib.append_markdown_bullets(text, "Requirements", args.requirement)
            counts["requirements"] = len(args.requirement)
        if args.constraint:
            state.setdefault("constraints", []).extend(args.constraint)
            text = lib.append_markdown_bullets(text, "Constraints", args.constraint)
            counts["constraints"] = len(args.constraint)
        if args.success_criterion:
            state.setdefault("success_criteria", []).extend(args.success_criterion)
            text = lib.append_markdown_bullets(text, "Success Criteria", args.success_criterion)
            counts["success_criteria"] = len(args.success_criterion)
        lib.atomic_write_text(md_path, text)
        return [("REQUIREMENTS_UPDATED", {"objective_updated": bool(args.objective), **counts})]

    _emit_state(_mutate(args, mutation), store)
    return 0


def cmd_add_blocker(args: argparse.Namespace) -> int:
    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        state.setdefault("blockers", []).append(
            {"summary": args.summary, "recorded_at": lib.now_iso(), "resolved": False}
        )
        return [("BLOCKER_ADDED", {"summary": args.summary})]

    _emit_state(_mutate(args, mutation), _store_from_args(args))
    return 0


def cmd_resolve_blocker(args: argparse.Namespace) -> int:
    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        blockers = state.setdefault("blockers", [])
        resolved = 0
        for blocker in blockers:
            if args.all and not blocker.get("resolved"):
                blocker["resolved"] = True
                blocker["resolved_at"] = lib.now_iso()
                resolved += 1
            elif not blocker.get("resolved") and (blocker.get("summary") == args.summary):
                blocker["resolved"] = True
                blocker["resolved_at"] = lib.now_iso()
                resolved += 1
        if not resolved:
            raise lib.ValidationError("no matching unresolved blocker found")
        return [("BLOCKER_RESOLVED", {"count": resolved})]

    _emit_state(_mutate(args, mutation), _store_from_args(args))
    return 0


def cmd_start_operation(args: argparse.Namespace) -> int:
    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        unresolved = [op for op in state.get("operations", []) if op.get("state") in lib.UNRESOLVED_OPERATION_STATES]
        if unresolved:
            raise lib.ValidationError(
                f"unresolved operation already exists: {unresolved[0].get('id')}; finish or mark it before starting another"
            )
        op_id = args.operation_id or lib.next_operation_id(state)
        if any(op.get("id") == op_id for op in state.get("operations", [])):
            raise lib.ValidationError(f"operation already exists: {op_id}")
        operation = {
            "id": op_id,
            "description": args.description,
            "state": "running",
            "side_effect": bool(args.side_effect),
            "started_at": lib.now_iso(),
            "completed_at": None,
            "result_summary": None,
        }
        state.setdefault("operations", []).append(operation)
        state["active_operation"] = op_id
        if args.side_effect:
            state["dirty"] = True
        return [("OPERATION_STARTED", {"operation_id": op_id, "side_effect": bool(args.side_effect)})]

    _emit_state(_mutate(args, mutation), _store_from_args(args))
    return 0


def cmd_finish_operation(args: argparse.Namespace) -> int:
    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        operation_id = _resolve_active_operation(state, args.operation_id)
        if not operation_id:
            raise lib.ValidationError("no active operation; provide --operation-id")
        op = _operation_by_id(state, operation_id)
        if op.get("state") in {"succeeded", "failed"} and op.get("completed_at"):
            raise lib.ValidationError(f"operation {operation_id} is already finished as {op.get('state')}")
        op["state"] = args.outcome
        op["completed_at"] = lib.now_iso()
        if args.result_summary is not None:
            op["result_summary"] = args.result_summary
        if args.outcome == "outcome_unknown":
            op["result_summary"] = args.result_summary or (
                "Outcome unknown; external state must be inspected before retry."
            )
        unresolved = [item for item in state.get("operations", []) if item.get("state") in lib.UNRESOLVED_OPERATION_STATES]
        if unresolved:
            state["active_operation"] = unresolved[0].get("id")
            state["dirty"] = True
        else:
            state["active_operation"] = None
            state["dirty"] = False
        event_type = {
            "succeeded": "OPERATION_SUCCEEDED",
            "failed": "OPERATION_FAILED",
            "outcome_unknown": "OPERATION_OUTCOME_UNKNOWN",
        }[args.outcome]
        return [(event_type, {"operation_id": operation_id, "result_summary": op.get("result_summary")})]

    _emit_state(_mutate(args, mutation), _store_from_args(args))
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if args.important_context:
            state.setdefault("important_context", []).extend(args.important_context)
        if args.blocker:
            for item in args.blocker:
                state.setdefault("blockers", []).append(
                    {"summary": item, "recorded_at": lib.now_iso(), "resolved": False}
                )
        if args.clear_blockers:
            state["blockers"] = []
        return [("CHECKPOINT_CREATED", {"revision": state.get("revision", 1) + 1})]

    _emit_state(_mutate(args, mutation), _store_from_args(args))
    return 0


def cmd_renew_lease(args: argparse.Namespace) -> int:
    session_id = _require_session(args)
    if not isinstance(args.lease_seconds, int) or isinstance(args.lease_seconds, bool) or args.lease_seconds <= 0:
        raise lib.ValidationError("--lease-seconds must be a positive integer")

    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        # mutate_task enforces that the caller is the current owner.  This
        # command extends the lease and updates the preferred heartbeat
        # duration; it must never change owner_session.
        state["lease_duration_seconds"] = args.lease_seconds
        state["lease_expires_at"] = lib.lease_expiry_from_now(args.lease_seconds)
        return [("LEASE_RENEWED", {"session_id": session_id, "lease_seconds": args.lease_seconds})]

    _emit_state(
        _mutate(args, mutation, renew_lease=False, touch_session=True),
        _store_from_args(args),
    )
    return 0


def cmd_attach_session(args: argparse.Namespace) -> int:
    session_id = _require_session(args)
    task_id = _require_task(args)

    def mutation(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        return lib.apply_task_ownership_transfer(
            state,
            task_id=task_id,
            session_id=session_id,
            force=args.force,
            lease_duration_seconds=args.lease_seconds,
        )

    _emit_state(
        _mutate(
            args,
            mutation,
            enforce_lease=False,
            renew_lease=False,
            touch_session=True,
            takeover_cleanup=True,
        ),
        _store_from_args(args),
    )
    return 0


def cmd_detach_session(args: argparse.Namespace) -> int:
    workspace, store, session_id, task_id = lib.cli_context(args)
    lib.ensure_store(store)
    task_id = _require_task(args)
    session_id = _require_session(args)
    snapshot = lib.load_task_state(store, task_id)
    owner = snapshot.get("owner_session")
    if owner is None:
        raise lib.ValidationError("task is already unowned")
    if owner != session_id:
        raise lib.LeaseConflict(
            f"task is owned by {owner}, not {session_id}",
            details={"reason": "owner_mismatch", "owner_session": owner, "session_id": session_id},
        )

    def mutation(current: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        current_owner = current.get("owner_session")
        if current_owner is None:
            raise lib.ValidationError("task is already unowned")
        if current_owner != session_id:
            raise lib.LeaseConflict(
                f"task is owned by {current_owner}, not {session_id}",
                details={"reason": "owner_mismatch", "owner_session": current_owner, "session_id": session_id},
            )
        current["owner_session"] = None
        current["lease_expires_at"] = None
        return [("TASK_DETACHED", {"session_id": session_id})]

    state = lib.mutate_task(
        store,
        task_id,
        mutation,
        session_id=session_id,
        enforce_lease=False,
        renew_lease=False,
        touch_session=False,
    )
    lib.set_session_active_task(store, session_id, None)
    _emit_state(state, store)
    return 0


def cmd_rebuild_index(args: argparse.Namespace) -> int:
    workspace, store, session_id, task_id = lib.cli_context(args)
    lib.ensure_store(store)
    with lib.ProcessLock(lib.index_lock_path(store)):
        index = lib.rebuild_index_unlocked(store)
        lib.save_index_unlocked(store, index)
    lib.emit_json({"store": str(store), "task_count": len(index.get("tasks", {})), "session_count": len(index.get("sessions", {}))})
    return 0


def _store_from_args(args: argparse.Namespace) -> Path:
    workspace = lib.resolve_workspace(lib.arg_value(args, "workspace"))
    return lib.resolve_store(lib.arg_value(args, "store"), workspace=workspace)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = lib.make_common_parser()
    parser = argparse.ArgumentParser(
        prog="checkpoint.py",
        description="Mutate durable session-reliability task state.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("create-task", parents=[common], help="create a durable task")
    p.add_argument("--title", required=True)
    p.add_argument("--objective", required=True)
    p.add_argument("--requirement", action="append", default=[])
    p.add_argument("--constraint", action="append", default=[])
    p.add_argument("--success-criterion", action="append", default=[])
    p.add_argument("--task-id", dest="new_task_id", default=None, help="explicit task id (default: generated)")
    p.add_argument("--lease-seconds", type=int, default=lib.DEFAULT_LEASE_SECONDS,
                   help="initial lease duration when --session-id is supplied")
    p.set_defaults(func=cmd_create_task)

    p = sub.add_parser("set-status", parents=[common], help="set task status")
    p.add_argument("--status", required=True, choices=lib.TASK_STATUSES)
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_set_status)

    p = sub.add_parser("add-step", parents=[common], help="add a step")
    p.add_argument("--step-id", dest="step_id", default=None)
    p.add_argument("--title", required=True)
    p.add_argument("--status", default="pending", choices=lib.STEP_STATUSES)
    p.set_defaults(func=cmd_add_step)

    p = sub.add_parser("start-step", parents=[common], help="mark a step in_progress")
    p.add_argument("--step", dest="step_id", required=True)
    p.set_defaults(func=cmd_start_step)

    p = sub.add_parser("complete-step", parents=[common], help="mark a step completed")
    p.add_argument("--step", dest="step_id", required=True)
    p.add_argument("--summary", default=None)
    p.set_defaults(func=cmd_complete_step)

    p = sub.add_parser("fail-step", parents=[common], help="mark a step failed")
    p.add_argument("--step", dest="step_id", required=True)
    p.add_argument("--summary", default=None)
    p.set_defaults(func=cmd_fail_step)

    p = sub.add_parser("block-step", parents=[common], help="mark a step blocked")
    p.add_argument("--step", dest="step_id", required=True)
    p.add_argument("--summary", default=None)
    p.add_argument("--reason", default=None)
    p.set_defaults(func=cmd_block_step)

    p = sub.add_parser("set-current-step", parents=[common], help="set or clear the current step")
    p.add_argument("--step", dest="step_id", default=None)
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_set_current_step)

    p = sub.add_parser("set-next-actions", parents=[common], help="replace the next-action list")
    p.add_argument("--action", action="append", default=[])
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_set_next_actions)

    p = sub.add_parser("record-finding", parents=[common], help="record a confirmed fact")
    p.add_argument("--summary", required=True)
    p.set_defaults(func=cmd_record_finding)

    p = sub.add_parser("record-decision", parents=[common], help="record a decision")
    p.add_argument("--summary", required=True)
    p.set_defaults(func=cmd_record_decision)

    p = sub.add_parser("update-requirements", parents=[common], help="update TASK.md durable requirements")
    p.add_argument("--objective", default=None)
    p.add_argument("--requirement", action="append", default=[])
    p.add_argument("--constraint", action="append", default=[])
    p.add_argument("--success-criterion", action="append", default=[])
    p.set_defaults(func=cmd_update_requirements)

    p = sub.add_parser("add-blocker", parents=[common], help="record a blocker")
    p.add_argument("--summary", required=True)
    p.set_defaults(func=cmd_add_blocker)

    p = sub.add_parser("resolve-blocker", parents=[common], help="resolve blocker(s)")
    p.add_argument("--summary", default=None)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_resolve_blocker)

    p = sub.add_parser("start-operation", parents=[common], help="persist a side-effect operation before execution")
    p.add_argument("--description", required=True)
    p.add_argument("--operation-id", dest="operation_id", default=None)
    p.add_argument("--side-effect", dest="side_effect", action="store_true", default=True)
    p.add_argument("--no-side-effect", dest="side_effect", action="store_false")
    p.set_defaults(func=cmd_start_operation)

    p = sub.add_parser("finish-operation", parents=[common], help="persist an operation outcome")
    p.add_argument("--operation-id", dest="operation_id", default=None)
    p.add_argument("--outcome", required=True, choices=["succeeded", "failed", "outcome_unknown"])
    p.add_argument("--result-summary", default=None)
    p.set_defaults(func=cmd_finish_operation)

    p = sub.add_parser("unknown-operation", parents=[common], help="alias for finish-operation --outcome outcome_unknown")
    p.add_argument("--operation-id", dest="operation_id", default=None)
    p.add_argument("--result-summary", default=None)
    p.set_defaults(func=lambda a: cmd_finish_operation(_force_outcome(a, "outcome_unknown")))

    p = sub.add_parser("checkpoint", parents=[common], help="write a durable recovery checkpoint")
    p.add_argument("--important-context", action="append", default=[])
    p.add_argument("--blocker", action="append", default=[])
    p.add_argument("--clear-blockers", action="store_true")
    p.set_defaults(func=cmd_checkpoint)

    p = sub.add_parser("renew-lease", parents=[common], help="renew the task lease for a session")
    p.add_argument("--lease-seconds", type=int, default=lib.DEFAULT_LEASE_SECONDS)
    p.set_defaults(func=cmd_renew_lease)

    p = sub.add_parser("attach-session", parents=[common], help="attach a session to a task")
    p.add_argument("--lease-seconds", type=int, default=lib.DEFAULT_LEASE_SECONDS)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_attach_session)

    p = sub.add_parser("detach-session", parents=[common], help="detach the current session")
    p.set_defaults(func=cmd_detach_session)

    p = sub.add_parser("rebuild-index", parents=[common], help="rebuild index.json from authoritative task/session files")
    p.set_defaults(func=cmd_rebuild_index)

    return parser


def _force_outcome(args: argparse.Namespace, outcome: str) -> argparse.Namespace:
    args.outcome = outcome
    return args


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except lib.SRError as exc:
        lib.cli_error(exc)
        return exc.code
    except Exception as exc:  # pragma: no cover - last-resort safety
        sys.stderr.write(f"[internal_error] {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
