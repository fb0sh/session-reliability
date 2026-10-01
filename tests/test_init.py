from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402


class InitTests(unittest.TestCase):
    def test_init_creates_store_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = helpers.init_session(workspace)

            store = workspace / ".agents" / "store" / "session-reliability"
            self.assertTrue((store / "index.json").is_file())
            self.assertTrue((store / "sessions").is_dir())
            self.assertTrue((store / "tasks").is_dir())
            self.assertTrue((store / "archive").is_dir())
            self.assertTrue(result["session_id"].startswith("sr-"))
            self.assertIsNone(result["active_task"])
            self.assertEqual(result["resumable_tasks"], [])

            session_path = Path(result["session_file"])
            self.assertTrue(session_path.is_file())
            session = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(session["schema_version"], 1)
            self.assertEqual(session["session_id"], result["session_id"])
            self.assertEqual(session["status"], "active")
            self.assertIsNone(session["native_session_id"])

            index = helpers.index_of(workspace)
            self.assertEqual(index["schema_version"], 1)
            self.assertIn(result["session_id"], index["sessions"])

    def test_multiple_unfinished_tasks_are_reported_without_auto_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            helpers.create_task(workspace, "task-20260930-000001-alpha", title="Alpha")
            helpers.create_task(workspace, "task-20260930-000002-beta", title="Beta")

            result = helpers.init_session(workspace)
            self.assertIsNone(result["active_task"])
            self.assertEqual(len(result["resumable_tasks"]), 2)
            task_ids = {item["task_id"] for item in result["resumable_tasks"]}
            self.assertEqual(task_ids, {"task-20260930-000001-alpha", "task-20260930-000002-beta"})

    def test_missing_index_is_rebuilt_from_authoritative_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            helpers.create_task(workspace, "task-20260930-000003-gamma", title="Gamma")
            store = workspace / ".agents" / "store" / "session-reliability"
            (store / "index.json").unlink()

            result = helpers.run_json("list.py", "--workspace", str(workspace), "--unfinished")
            self.assertEqual(result["count"], 1)
            self.assertTrue((store / "index.json").is_file())
            rebuilt = helpers.index_of(workspace)
            self.assertIn("task-20260930-000003-gamma", rebuilt["tasks"])


if __name__ == "__main__":
    unittest.main()
