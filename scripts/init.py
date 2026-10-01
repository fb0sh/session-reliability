#!/usr/bin/env python3
"""Initialize the session-reliability runtime for a workspace.

Outputs machine-readable JSON on stdout.  Human-readable errors go to stderr
and the process exits non-zero.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="init.py",
        description="Initialize session-reliability store and a reliability session.",
    )
    parser.add_argument("--workspace", metavar="PATH", default=None,
                        help="workspace root (default: cwd or SESSION_RELIABILITY_WORKSPACE)")
    parser.add_argument("--store", metavar="PATH", default=None,
                        help="store path (default: workspace/.agents/store/session-reliability)")
    parser.add_argument("--session-id", metavar="SESSION_ID", default=None,
                        help="reuse/refresh a specific reliability session id")
    parser.add_argument("--native-session-id", metavar="NATIVE_ID", default=None,
                        help="optional runtime-provided session/conversation id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workspace = lib.resolve_workspace(args.workspace)
        store = lib.resolve_store(args.store, workspace=workspace)
        lib.ensure_store(store)
        session, created = lib.create_or_touch_session(
            store,
            session_id=args.session_id,
            native_session_id=args.native_session_id,
        )
        resumable = lib.find_resumable_tasks(store)
        payload = {
            "session_id": session["session_id"],
            "native_session_id": session.get("native_session_id"),
            "session_file": str(lib.session_path(store, session["session_id"])),
            "session_created": created,
            "status": session.get("status"),
            "active_task": session.get("active_task"),
            "workspace": str(workspace),
            "store": str(store),
            "resumable_tasks": resumable,
        }
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
