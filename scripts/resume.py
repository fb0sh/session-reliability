#!/usr/bin/env python3
"""Resume a durable task from a fresh reliability session.

Outputs a JSON recovery summary on stdout.  The summary includes the object's
objective, current step, next actions, dirty/active-operation status and the
paths to TASK.md, STATE.json, CHECKPOINT.md and EVENTS.jsonl.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    common = lib.make_common_parser()
    parser = argparse.ArgumentParser(
        prog="resume.py",
        description="Resume or take over a durable task.",
        parents=[common],
    )
    parser.add_argument("--latest", action="store_true", help="resume the most recently updated unfinished task")
    parser.add_argument("--force-takeover", action="store_true", help="take over even if another session holds a valid lease")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workspace, store, session_id, task_arg = lib.cli_context(args)
        lib.ensure_store(store)

        session, _created = lib.create_or_touch_session(store, session_id=session_id)
        session_id = session["session_id"]

        selected_task_id = task_arg
        try:
            task_id = lib.choose_task(store, task_arg, latest=args.latest)
            selected_task_id = task_id
            state, warnings = lib.resume_task(
                store,
                task_id,
                session_id,
                force_takeover=args.force_takeover,
            )
        except lib.CorruptionError as exc:
            if selected_task_id:
                with contextlib.suppress(lib.SRError, OSError):
                    lib.write_recovery_notice(store, selected_task_id, exc)
                exc.details.setdefault("recovery", lib.recovery_info(store, selected_task_id))
            raise

        payload = lib.build_resume_payload(store, task_id, session_id, state, warnings)
        payload["workspace"] = str(workspace)
        payload["store"] = str(store)
        lib.emit_json(payload)
        return 0
    except lib.SRError as exc:
        lib.cli_error(exc)
        return exc.code
    except Exception as exc:  # pragma: no cover - last-resort safety
        sys.stderr.write(f"[internal_error] {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
