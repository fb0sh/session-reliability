from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402

SCRIPTS = helpers.SCRIPTS
sys.path.insert(0, str(SCRIPTS))
import lib  # noqa: E402


class ConcurrencyTests(unittest.TestCase):
    def test_revision_conflict_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            store = lib.resolve_store(None, workspace=workspace)
            lib.ensure_store(store)
            session_id = "sr-20260930-030500-conflict"
            state = lib.create_task(store, title="Revision Task", objective="Detect conflict.", session_id=session_id)
            task_id = state["task_id"]
            revision = state["revision"]

            lib.mutate_task(
                store,
                task_id,
                lambda current: current.__setitem__("next_actions", ["A"]),
                session_id=session_id,
            )
            with self.assertRaises(lib.RevisionConflict) as caught:
                lib.mutate_task(
                    store,
                    task_id,
                    lambda current: current.__setitem__("next_actions", ["B"]),
                    expected_revision=revision,
                    session_id=session_id,
                )
            self.assertIn("revision conflict", caught.exception.message.lower())
            self.assertEqual(lib.load_task_state(store, task_id)["next_actions"], ["A"])

    def test_valid_lease_blocks_takeover_and_expired_lease_allows_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_id = "task-20260930-030000-lease"
            helpers.create_task(
                workspace,
                task_id,
                title="Lease Task",
                objective="Test lease semantics.",
                session_id=session_a["session_id"],
                lease_seconds=900,
            )
            session_b = helpers.init_session(workspace)

            blocked = helpers.run_script(
                "resume.py",
                "--workspace", str(workspace),
                "--session-id", session_b["session_id"],
                "--task", task_id,
                check=False,
            )
            self.assertEqual(blocked.returncode, lib.LeaseConflict.code)
            self.assertIn("lease_conflict", blocked.stderr)
            self.assertEqual(helpers.state_of(workspace, task_id)["owner_session"], session_a["session_id"])

            # Simulate the old session's lease expiring while it is no longer active.
            helpers.expire_lease(workspace, task_id)

            taken = helpers.run_json(
                "resume.py",
                "--workspace", str(workspace),
                "--session-id", session_b["session_id"],
                "--task", task_id,
            )
            self.assertEqual(taken["owner_session"], session_b["session_id"])
            event_types = [event["type"] for event in helpers.events_of(workspace, task_id)]
            self.assertIn("TASK_TAKEOVER", event_types)

    def test_process_lock_serializes_concurrent_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = helpers.init_session(workspace)
            task_id = "task-20260930-030100-lock"
            helpers.create_task(workspace, task_id, title="Lock Task", objective="Concurrent mutation.")

            commands = [
                [
                    sys.executable,
                    str(SCRIPTS / "checkpoint.py"),
                    "--workspace", str(workspace),
                    "--session-id", session["session_id"],
                    "--task", task_id,
                    "add-step",
                    "--title", f"Concurrent {index}",
                ]
                for index in range(4)
            ]
            processes = [subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for cmd in commands]
            results = [process.communicate(timeout=20) for process in processes]
            for process, (stdout, stderr) in zip(processes, results):
                if process.returncode != 0:
                    self.fail(f"concurrent mutation failed: {stderr}\n{stdout}")
            state = helpers.state_of(workspace, task_id)
            self.assertEqual(len(state["steps"]), 4)
            self.assertEqual(state["revision"], 5)


if __name__ == "__main__":
    unittest.main()
