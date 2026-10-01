from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402

SCRIPTS = helpers.SCRIPTS
sys.path.insert(0, str(SCRIPTS))
import lib  # noqa: E402


class SchemaCompatibilityTests(unittest.TestCase):
    def test_future_session_schema_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            session_path = helpers.store_dir(workspace) / "sessions" / "sr-future.json"
            future = {
                "schema_version": 999,
                "session_id": "sr-future",
                "native_session_id": None,
                "status": "active",
                "active_task": None,
                "started_at": "2026-10-01T00:00:00+00:00",
                "last_seen_at": "2026-10-01T00:00:00+00:00",
            }
            original = (json.dumps(future, indent=2) + "\n").encode()
            session_path.write_bytes(original)

            result = helpers.run_script(
                "init.py",
                "--workspace", str(workspace),
                "--session-id", "sr-future",
                check=False,
            )
            self.assertEqual(result.returncode, lib.FutureSchemaError.code)
            self.assertIn("unsupported_schema_version", result.stderr)
            self.assertEqual(session_path.read_bytes(), original)
            self.assertFalse(list(session_path.parent.glob("sr-future.json.corrupt.*.bak")))

    def test_future_session_schema_blocks_native_lookup_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            sessions_before = {path.name for path in (helpers.store_dir(workspace) / "sessions").glob("*.json")}
            session_path = helpers.store_dir(workspace) / "sessions" / "sr-future-native.json"
            future = {
                "schema_version": 999,
                "session_id": "sr-future-native",
                "native_session_id": "runtime-X",
                "status": "active",
                "active_task": None,
                "started_at": "2026-10-01T00:00:00+00:00",
                "last_seen_at": "2026-10-01T00:00:00+00:00",
            }
            original = (json.dumps(future, indent=2) + "\n").encode()
            session_path.write_bytes(original)

            result = helpers.run_script(
                "init.py",
                "--workspace", str(workspace),
                "--native-session-id", "runtime-X",
                check=False,
            )
            self.assertEqual(result.returncode, lib.FutureSchemaError.code)
            self.assertEqual(session_path.read_bytes(), original)
            sessions_after = {path.name for path in (helpers.store_dir(workspace) / "sessions").glob("*.json")}
            self.assertEqual(sessions_after, sessions_before | {"sr-future-native.json"})

    def test_future_task_state_schema_is_not_ignored_by_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            task_id = "task-20261001-000030-future-state"
            helpers.create_task(workspace, task_id)
            state_path = helpers.task_dir(workspace, task_id) / "STATE.json"
            future_state = {
                "schema_version": 999,
                "task_id": task_id,
                "title": "Future",
                "status": "in_progress",
                "revision": 1,
                "objective": "future schema",
                "current_step": None,
                "steps": [],
                "next_actions": [],
                "owner_session": None,
                "lease_expires_at": None,
                "dirty": False,
                "active_operation": None,
            }
            original = (json.dumps(future_state, indent=2) + "\n").encode()
            state_path.write_bytes(original)

            result = helpers.run_script(
                "list.py",
                "--workspace", str(workspace),
                "--all",
                check=False,
            )
            self.assertEqual(result.returncode, lib.FutureSchemaError.code)
            self.assertEqual(state_path.read_bytes(), original)

    def test_current_version_corrupt_indexes_are_rebuilt(self) -> None:
        cases = {
            "missing_schema_version": {"tasks": {}, "sessions": {}},
            "bad_schema_version_type": {"schema_version": "one", "tasks": {}, "sessions": {}},
            "invalid_tasks_sessions_shape": {"schema_version": 1, "tasks": [], "sessions": "bad"},
            "missing_tasks_sessions": {"schema_version": 1},
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = helpers.init_session(workspace)
            task_id = "task-20261001-000032-index-rebuild"
            helpers.create_task(workspace, task_id, session_id=session["session_id"])
            index_path = helpers.store_dir(workspace) / "index.json"

            for name, payload in cases.items():
                with self.subTest(name=name):
                    index_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                    result = helpers.run_json("init.py", "--workspace", str(workspace))
                    self.assertTrue(result["session_id"].startswith("sr-"))
                    rebuilt = helpers.index_of(workspace)
                    self.assertEqual(rebuilt["schema_version"], 1)
                    self.assertIn(task_id, rebuilt["tasks"])
                    self.assertIn(session["session_id"], rebuilt["sessions"])

    def test_future_index_schema_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            index_path = helpers.store_dir(workspace) / "index.json"
            future_index = {"schema_version": 999, "tasks": {}, "sessions": {}}
            original = (json.dumps(future_index, indent=2) + "\n").encode()
            index_path.write_bytes(original)

            result = helpers.run_script(
                "init.py",
                "--workspace", str(workspace),
                check=False,
            )
            self.assertEqual(result.returncode, lib.FutureSchemaError.code)
            self.assertEqual(index_path.read_bytes(), original)

    def test_legacy_state_without_lease_duration_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = helpers.init_session(workspace)
            task_id = "task-20261001-000031-legacy-state"
            helpers.create_task(workspace, task_id, session_id=session["session_id"], lease_seconds=30)
            state_path = helpers.task_dir(workspace, task_id) / "STATE.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.pop("lease_duration_seconds")
            state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

            helpers.cp_json(
                workspace,
                task_id,
                "add-step", "--title", "Legacy mutation",
                session_id=session["session_id"],
            )
            updated = helpers.state_of(workspace, task_id)
            self.assertEqual(updated["lease_duration_seconds"], lib.DEFAULT_LEASE_SECONDS)
            renewed_seconds = (
                lib.parse_iso(updated["lease_expires_at"]) - lib.parse_iso(updated["updated_at"])
            ).total_seconds()
            self.assertGreaterEqual(renewed_seconds, lib.DEFAULT_LEASE_SECONDS - 5)
            self.assertLessEqual(renewed_seconds, lib.DEFAULT_LEASE_SECONDS + 5)


if __name__ == "__main__":
    unittest.main()
