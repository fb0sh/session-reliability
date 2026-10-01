#!/usr/bin/env python3
"""List durable tasks from authoritative task directories.

Outputs JSON on stdout.  This command never treats index.json as authoritative;
it scans tasks/*/STATE.json and can therefore recover after index loss.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    common = lib.make_common_parser()
    parser = argparse.ArgumentParser(
        prog="list.py",
        description="List session-reliability tasks.",
        parents=[common],
    )
    parser.add_argument("--active", action="store_true", help="status == in_progress")
    parser.add_argument("--paused", action="store_true", help="status == paused")
    parser.add_argument("--blocked", action="store_true", help="status == blocked")
    parser.add_argument("--unfinished", action="store_true", help="pending/in_progress/blocked/paused")
    parser.add_argument("--completed", action="store_true", help="status == completed")
    parser.add_argument("--all", action="store_true", help="all tasks (default when no filter is given)")
    parser.add_argument("--status", action="append", default=[], choices=lib.TASK_STATUSES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workspace = lib.resolve_workspace(lib.arg_value(args, "workspace"))
        store = lib.resolve_store(lib.arg_value(args, "store"), workspace=workspace)
        lib.ensure_store(store)

        statuses: set[str] = set(args.status or [])
        if args.active:
            statuses.add("in_progress")
        if args.paused:
            statuses.add("paused")
        if args.blocked:
            statuses.add("blocked")
        if args.completed:
            statuses.add("completed")
        if args.unfinished:
            statuses.update(lib.UNFINISHED_STATUSES)
        status_filter = None if (args.all or not statuses) else statuses

        tasks = lib.list_tasks(store, statuses=status_filter)
        lib.emit_json(
            {
                "workspace": str(workspace),
                "store": str(store),
                "filters": {
                    "statuses": sorted(status_filter) if status_filter is not None else sorted(lib.TASK_STATUSES),
                    "unfinished": bool(args.unfinished),
                    "all": bool(args.all),
                },
                "count": len(tasks),
                "tasks": tasks,
            }
        )
        return 0
    except lib.SRError as exc:
        lib.cli_error(exc)
        return exc.code
    except Exception as exc:  # pragma: no cover - last-resort safety
        sys.stderr.write(f"[internal_error] {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
