from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402


class CheckpointTests(unittest.TestCase):
    def test_create_task_writes_all_required_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            result = helpers.create_task(
                workspace,
                "task-20260930-010000-created",
                title="Created Task",
                objective="Prove create-task works.",
                requirements=["R1"],
                constraints=["C1"],
                success_criteria=["S1"],
            )
            base = helpers.task_dir(workspace, result["task_id"])
            for name in ("TASK.md", "STATE.json", "CHECKPOINT.md", "EVENTS.jsonl"):
                self.assertTrue((base / name).is_file(), name)

            state = helpers.state_of(workspace, result["task_id"])
            self.assertEqual(state["schema_version"], 1)
            self.assertEqual(state["task_id"], result["task_id"])
            self.assertEqual(state["status"], "pending")
            self.assertEqual(state["revision"], 1)
            self.assertIn("R1", (base / "TASK.md").read_text(encoding="utf-8"))
            self.assertIn("S1", (base / "TASK.md").read_text(encoding="utf-8"))
            events = helpers.events_of(workspace, result["task_id"])
            self.assertEqual(events[0]["type"], "TASK_CREATED")

    def test_progress_is_consistent_across_state_steps_events_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            task_id = "task-20260930-010100-progress"
            helpers.create_task(workspace, task_id, title="Progress Task", objective="Do progress.")

            helpers.cp_json(workspace, task_id, "add-step", "--title", "Step Alpha")
            helpers.cp_json(workspace, task_id, "add-step", "--title", "Step Beta")
            helpers.cp_json(workspace, task_id, "add-step", "--title", "Step Gamma")
            helpers.cp_json(workspace, task_id, "start-step", "--step", "step-1")
            helpers.cp_json(workspace, task_id, "complete-step", "--step", "step-1", "--summary", "Alpha complete")
            helpers.cp_json(workspace, task_id, "start-step", "--step", "step-2")
            helpers.cp_json(workspace, task_id, "set-next-actions", "--action", "Run beta", "--action", "Run gamma")
            helpers.cp_json(workspace, task_id, "checkpoint", "--important-context", "Alpha output checked")

            state = helpers.state_of(workspace, task_id)
            self.assertEqual(state["current_step"], "step-2")
            self.assertEqual([s["status"] for s in state["steps"]], ["completed", "in_progress", "pending"])
            self.assertEqual(state["steps"][0]["summary"], "Alpha complete")
            self.assertEqual(state["next_actions"], ["Run beta", "Run gamma"])
            self.assertEqual(state["revision"], 9)

            checkpoint = (helpers.task_dir(workspace, task_id) / "CHECKPOINT.md").read_text(encoding="utf-8")
            self.assertIn("Alpha complete", checkpoint)
            self.assertIn("Step `step-2`", checkpoint)
            self.assertIn("Alpha output checked", checkpoint)

            event_types = [event["type"] for event in helpers.events_of(workspace, task_id)]
            self.assertIn("STEP_STARTED", event_types)
            self.assertIn("STEP_COMPLETED", event_types)
            self.assertIn("CHECKPOINT_CREATED", event_types)

    def test_start_and_finish_operation_updates_dirty_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            task_id = "task-20260930-010200-operation"
            helpers.create_task(workspace, task_id, title="Operation Task", objective="Test operation.")

            helpers.cp_json(workspace, task_id, "start-operation", "--description", "Install package X")
            state = helpers.state_of(workspace, task_id)
            self.assertTrue(state["dirty"])
            self.assertEqual(state["active_operation"], "op-1")
            self.assertEqual(state["operations"][0]["state"], "running")

            helpers.cp_json(
                workspace,
                task_id,
                "finish-operation",
                "--operation-id", "op-1",
                "--outcome", "succeeded",
                "--result-summary", "package verified installed",
            )
            state = helpers.state_of(workspace, task_id)
            self.assertFalse(state["dirty"])
            self.assertIsNone(state["active_operation"])
            self.assertEqual(state["operations"][0]["state"], "succeeded")
            self.assertEqual(state["operations"][0]["result_summary"], "package verified installed")
            event_types = [event["type"] for event in helpers.events_of(workspace, task_id)]
            self.assertIn("OPERATION_STARTED", event_types)
            self.assertIn("OPERATION_SUCCEEDED", event_types)

    def test_update_requirements_persists_to_task_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            task_id = "task-20260930-010300-requirements"
            helpers.create_task(workspace, task_id, title="Requirements Task", objective="Original objective.")

            helpers.cp_json(
                workspace,
                task_id,
                "update-requirements",
                "--objective", "Updated objective.",
                "--requirement", "Must stay offline.",
                "--constraint", "Python stdlib only.",
                "--success-criterion", "Fresh session resumes safely.",
            )
            state = helpers.state_of(workspace, task_id)
            self.assertEqual(state["objective"], "Updated objective.")
            self.assertIn("Must stay offline.", state["requirements"])
            task_md = (helpers.task_dir(workspace, task_id) / "TASK.md").read_text(encoding="utf-8")
            self.assertIn("Updated objective.", task_md)
            self.assertIn("Must stay offline.", task_md)
            self.assertIn("Python stdlib only.", task_md)
            self.assertIn("Fresh session resumes safely.", task_md)
            event_types = [event["type"] for event in helpers.events_of(workspace, task_id)]
            self.assertIn("REQUIREMENTS_UPDATED", event_types)

    def test_state_after_mutations_is_always_complete_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            task_id = "task-20260930-010400-atomic"
            helpers.create_task(workspace, task_id, title="Atomic Task", objective="Atomic writes.")

            operations = [
                ("add-step", "--title", "One"),
                ("start-step", "--step", "step-1"),
                ("complete-step", "--step", "step-1", "--summary", "done"),
                ("set-next-actions", "--action", "next"),
                ("record-finding", "--summary", "fact"),
            ]
            for args in operations:
                helpers.cp_json(workspace, task_id, *args)
                raw = (helpers.task_dir(workspace, task_id) / "STATE.json").read_text(encoding="utf-8")
                parsed = json.loads(raw)
                self.assertEqual(parsed["task_id"], task_id)
                self.assertGreaterEqual(parsed["revision"], 1)

            leftovers = list(helpers.task_dir(workspace, task_id).glob("*.tmp.*"))
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
