from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402


class IntegrationRecoveryTests(unittest.TestCase):
    def test_full_crash_takeover_and_completion_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_id = "task-20260930-040000-integration"

            helpers.create_task(
                workspace,
                task_id,
                title="Integration Task",
                objective="Prove durable crash recovery.",
                session_id=session_a["session_id"],
                lease_seconds=1,
                requirements=["Do not lose the task on session loss."],
                success_criteria=["Fresh session resumes and completes."],
            )
            helpers.cp_json(workspace, task_id, "add-step", "--title", "Inspect input")
            helpers.cp_json(workspace, task_id, "add-step", "--title", "Apply recovery")
            helpers.cp_json(workspace, task_id, "start-step", "--step", "step-1")
            helpers.cp_json(workspace, task_id, "complete-step", "--step", "step-1", "--summary", "Input inspected")
            helpers.cp_json(workspace, task_id, "start-step", "--step", "step-2")
            helpers.cp_json(workspace, task_id, "record-finding", "--summary", "Input file exists")
            helpers.cp_json(workspace, task_id, "set-next-actions", "--action", "Finish recovery")
            helpers.cp_json(workspace, task_id, "checkpoint", "--important-context", "Crash after side effect")
            helpers.cp_json(workspace, task_id, "start-operation", "--description", "Write generated artifact")

            # Simulate a crash: the process ends without finish-operation.  The
            # task's lease remains but will expire after one second.
            time.sleep(1.5)
            session_b = helpers.init_session(workspace)

            resumed = helpers.run_json(
                "resume.py",
                "--workspace", str(workspace),
                "--session-id", session_b["session_id"],
                "--task", task_id,
            )
            self.assertEqual(resumed["owner_session"], session_b["session_id"])
            self.assertTrue(resumed["dirty"])
            self.assertEqual(resumed["active_operation"], "op-1")
            self.assertEqual(resumed["operations"][0]["state"], "outcome_unknown")
            self.assertTrue(any("outcome_unknown" in warning for warning in resumed["warnings"]))

            # Recovery inspection concluded that the side effect took effect.
            helpers.cp_json(
                workspace,
                task_id,
                "finish-operation",
                "--operation-id", "op-1",
                "--outcome", "succeeded",
                "--result-summary", "Artifact exists after external inspection",
            )
            helpers.cp_json(workspace, task_id, "complete-step", "--step", "step-2", "--summary", "Recovery applied")
            helpers.cp_json(workspace, task_id, "set-status", "--status", "completed")

            state = helpers.state_of(workspace, task_id)
            self.assertEqual(state["status"], "completed")
            self.assertIsNone(state["current_step"])
            self.assertFalse(state["dirty"])
            self.assertIsNone(state["active_operation"])
            self.assertEqual([step["status"] for step in state["steps"]], ["completed", "completed"])
            self.assertEqual(state["operations"][0]["state"], "succeeded")
            self.assertEqual(state["operations"][0]["result_summary"], "Artifact exists after external inspection")

            base = helpers.task_dir(workspace, task_id)
            checkpoint = (base / "CHECKPOINT.md").read_text(encoding="utf-8")
            self.assertIn("Prove durable crash recovery.", checkpoint)
            self.assertIn("Input inspected", checkpoint)
            self.assertIn("Recovery applied", checkpoint)
            self.assertIn("Artifact exists after external inspection", checkpoint)

            events = helpers.events_of(workspace, task_id)
            event_types = [event["type"] for event in events]
            for expected in (
                "TASK_CREATED",
                "STEP_COMPLETED",
                "OPERATION_STARTED",
                "OPERATION_OUTCOME_UNKNOWN",
                "TASK_TAKEOVER",
                "OPERATION_SUCCEEDED",
                "TASK_COMPLETED",
            ):
                self.assertIn(expected, event_types)

            index = helpers.index_of(workspace)
            self.assertEqual(index["tasks"][task_id]["status"], "completed")
            session_file = json.loads(Path(session_b["session_file"]).read_text(encoding="utf-8"))
            self.assertEqual(session_file["active_task"], task_id)

            unfinished = helpers.run_json("list.py", "--workspace", str(workspace), "--unfinished")
            self.assertEqual(unfinished["count"], 0)
            completed = helpers.run_json("list.py", "--workspace", str(workspace), "--completed")
            self.assertEqual(completed["count"], 1)
            self.assertEqual(completed["tasks"][0]["task_id"], task_id)


if __name__ == "__main__":
    unittest.main()
