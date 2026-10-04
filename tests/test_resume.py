from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402


class ResumeTests(unittest.TestCase):
    def test_new_session_resumes_interrupted_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_id = "task-20260930-020000-resume"
            helpers.create_task(workspace, task_id, title="Resume Task", objective="Survive session loss.")

            helpers.cp_json(workspace, task_id, "add-step", "--title", "Analyze")
            helpers.cp_json(workspace, task_id, "add-step", "--title", "Implement")
            helpers.cp_json(workspace, task_id, "add-step", "--title", "Verify")
            helpers.cp_json(workspace, task_id, "start-step", "--step", "step-1")
            helpers.cp_json(workspace, task_id, "complete-step", "--step", "step-1", "--summary", "Analysis done")
            helpers.cp_json(workspace, task_id, "start-step", "--step", "step-2")
            helpers.cp_json(workspace, task_id, "set-next-actions", "--action", "Implement fix")
            helpers.cp_json(workspace, task_id, "checkpoint", "--important-context", "Use stdlib only")
            helpers.expire_lease(workspace, task_id)

            session_b = helpers.init_session(workspace)
            self.assertNotEqual(session_a["session_id"], session_b["session_id"])
            result = helpers.run_json(
                "resume.py",
                "--workspace", str(workspace),
                "--session-id", session_b["session_id"],
                "--task", task_id,
            )
            self.assertEqual(result["task_id"], task_id)
            self.assertEqual(result["objective"], "Survive session loss.")
            self.assertEqual(result["current_step"], "step-2")
            self.assertEqual(result["next_actions"], ["Implement fix"])
            self.assertFalse(result["dirty"])
            self.assertEqual(result["steps"][0]["status"], "completed")
            self.assertIn("Analysis done", result["checkpoint_md"])
            self.assertTrue(Path(result["checkpoint_path"]).is_file())
            self.assertTrue(Path(result["task_md_path"]).is_file())

            session_file = json.loads(Path(session_b["session_file"]).read_text(encoding="utf-8"))
            self.assertEqual(session_file["active_task"], task_id)

    def test_dirty_operation_is_marked_outcome_unknown_and_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            task_id = "task-20260930-020100-dirty"
            helpers.create_task(workspace, task_id, title="Dirty Task", objective="Recover dirty operation.")
            helpers.cp_json(workspace, task_id, "add-step", "--title", "Do operation")
            helpers.cp_json(workspace, task_id, "start-step", "--step", "step-1")
            helpers.cp_json(workspace, task_id, "start-operation", "--description", "Install package X")
            helpers.expire_lease(workspace, task_id)

            # Simulate process death: no finish-operation call is made.
            session_b = helpers.init_session(workspace)
            result = helpers.run_json(
                "resume.py",
                "--workspace", str(workspace),
                "--session-id", session_b["session_id"],
                "--task", task_id,
            )
            self.assertTrue(result["dirty"])
            self.assertEqual(result["active_operation"], "op-1")
            operations = result["operations"]
            self.assertEqual(operations[0]["state"], "outcome_unknown")
            self.assertNotEqual(operations[0]["state"], "succeeded")
            self.assertTrue(any("inspect" in warning.lower() for warning in result["warnings"]))

            state = helpers.state_of(workspace, task_id)
            self.assertTrue(state["dirty"])
            self.assertEqual(state["operations"][0]["state"], "outcome_unknown")
            event_types = [event["type"] for event in helpers.events_of(workspace, task_id)]
            self.assertIn("OPERATION_OUTCOME_UNKNOWN", event_types)

    def test_corrupted_state_returns_clear_error_and_preserves_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            task_id = "task-20260930-020200-corrupt"
            helpers.create_task(workspace, task_id, title="Corrupt Task", objective="Corruption handling.")
            state_path = helpers.task_dir(workspace, task_id) / "STATE.json"
            original = b"{ this is not valid json"
            state_path.write_bytes(original)

            result = helpers.run_script(
                "resume.py",
                "--workspace", str(workspace),
                "--task", task_id,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("state_corruption", result.stderr)
            self.assertIn("TASK.md", result.stderr)
            self.assertIn("EVENTS.jsonl", result.stderr)
            self.assertEqual(state_path.read_bytes(), original)
            self.assertFalse((helpers.task_dir(workspace, task_id) / "STATE.json.tmp").exists())
            recovery_notice = helpers.task_dir(workspace, task_id) / "RECOVERY_REQUIRED.md"
            self.assertTrue(recovery_notice.is_file())
            self.assertIn("damaged `STATE.json` has been preserved", recovery_notice.read_text(encoding="utf-8"))
            event_types = [event["type"] for event in helpers.events_of(workspace, task_id)]
            self.assertIn("STATE_CORRUPTION_DETECTED", event_types)


    def test_latest_refuses_same_second_tie_and_binds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            first = "task-20260930-021000-first"
            second = "task-20260930-021100-second"
            helpers.create_task(workspace, first, title="First Task", objective="First.")
            helpers.create_task(workspace, second, title="Second Task", objective="Second.")
            owners_before = {
                task_id: helpers.state_of(workspace, task_id)["owner_session"]
                for task_id in (first, second)
            }
            statuses_before = {
                task_id: helpers.state_of(workspace, task_id)["status"]
                for task_id in (first, second)
            }

            # updated_at has second granularity, so two tasks touched within the
            # same second cannot be ordered; force the tie deterministically.
            stamp = "2026-09-30T02:11:00+00:00"
            for task_id in (first, second):
                state_path = helpers.task_dir(workspace, task_id) / "STATE.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["updated_at"] = stamp
                state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

            result = helpers.run_script(
                "resume.py",
                "--workspace", str(workspace),
                "--latest",
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("most recent update time", result.stderr)
            self.assertIn(first, result.stderr)
            self.assertIn(second, result.stderr)

            for task_id in (first, second):
                state = helpers.state_of(workspace, task_id)
                self.assertEqual(state["owner_session"], owners_before[task_id])
                self.assertEqual(state["status"], statuses_before[task_id])
                self.assertIsNone(state["active_operation"])

    def test_latest_picks_newest_when_timestamps_differ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            older = "task-20260930-021200-older"
            newer = "task-20260930-021300-newer"
            helpers.create_task(workspace, older, title="Older", objective="Older.")
            helpers.create_task(workspace, newer, title="Newer", objective="Newer.")
            for task_id, stamp in (
                (older, "2026-09-30T02:12:00+00:00"),
                (newer, "2026-09-30T02:13:00+00:00"),
            ):
                state_path = helpers.task_dir(workspace, task_id) / "STATE.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["updated_at"] = stamp
                state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

            result = helpers.run_json(
                "resume.py",
                "--workspace", str(workspace),
                "--latest",
                "--force-takeover",
            )
            self.assertEqual(result["task_id"], newer)


if __name__ == "__main__":
    unittest.main()
