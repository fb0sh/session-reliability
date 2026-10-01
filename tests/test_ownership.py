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


def run_checkpoint(workspace: Path, task_id: str, session_id: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(SCRIPTS / "checkpoint.py"),
        "--workspace", str(workspace),
        "--task", task_id,
    ]
    if session_id:
        cmd += ["--session-id", session_id]
    cmd += list(args)
    return subprocess.run(cmd, text=True, capture_output=True)


class OwnershipTests(unittest.TestCase):
    def test_valid_lease_blocks_foreign_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_id = "task-20261001-000001-owner"
            helpers.create_task(workspace, task_id, session_id=session_a["session_id"], lease_seconds=900)
            helpers.cp_json(
                workspace,
                task_id,
                "add-step",
                "--title", "Owner step",
                session_id=session_a["session_id"],
            )
            before = helpers.state_of(workspace, task_id)
            session_b = helpers.init_session(workspace)

            result = run_checkpoint(
                workspace,
                task_id,
                session_b["session_id"],
                "complete-step", "--step", "step-1",
            )
            self.assertEqual(result.returncode, lib.LeaseConflict.code)
            self.assertIn("lease_conflict", result.stderr)
            after = helpers.state_of(workspace, task_id)
            self.assertEqual(after["revision"], before["revision"])
            self.assertEqual(after["steps"][0]["status"], "pending")
            self.assertEqual(after["owner_session"], session_a["session_id"])

    def test_missing_session_id_is_rejected_for_owned_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_id = "task-20261001-000002-missing-session"
            helpers.create_task(workspace, task_id, session_id=session_a["session_id"])
            helpers.cp_json(workspace, task_id, "add-step", "--title", "Step", session_id=session_a["session_id"])
            before = helpers.state_of(workspace, task_id)

            result = helpers.run_script(
                "checkpoint.py",
                "--workspace", str(workspace),
                "--task", task_id,
                "complete-step", "--step", "step-1",
                check=False,
            )
            self.assertEqual(result.returncode, lib.LeaseConflict.code)
            self.assertIn("--session-id", result.stderr)
            after = helpers.state_of(workspace, task_id)
            self.assertEqual(after["revision"], before["revision"])
            self.assertEqual(after["steps"][0]["status"], "pending")

    def test_owner_mutation_renews_lease_and_session_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = helpers.init_session(workspace)
            task_id = "task-20261001-000003-heartbeat"
            helpers.create_task(workspace, task_id, session_id=session["session_id"], lease_seconds=1)
            before = helpers.state_of(workspace, task_id)
            self.assertEqual(before["owner_session"], session["session_id"])

            # Force an observable old heartbeat timestamp without adding test sleep.
            session_path = Path(session["session_file"])
            session_data = json.loads(session_path.read_text(encoding="utf-8"))
            old_seen = "2000-01-01T00:00:00+00:00"
            session_data["last_seen_at"] = old_seen
            session_path.write_text(json.dumps(session_data, indent=2) + "\n", encoding="utf-8")

            helpers.cp_json(
                workspace,
                task_id,
                "add-step", "--title", "Heartbeat step",
                session_id=session["session_id"],
            )
            after = helpers.state_of(workspace, task_id)
            self.assertGreater(after["revision"], before["revision"])
            self.assertGreater(
                lib.parse_iso(after["lease_expires_at"]),
                lib.parse_iso(before["lease_expires_at"]),
                "owner mutation should renew the lease",
            )
            refreshed_session = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertGreater(
                lib.parse_iso(refreshed_session["last_seen_at"]),
                lib.parse_iso(old_seen),
                "owner mutation should refresh session.last_seen_at",
            )
            self.assertEqual(refreshed_session["active_task"], task_id)

    def test_renew_lease_cannot_steal_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_id = "task-20261001-000004-renew"
            helpers.create_task(workspace, task_id, session_id=session_a["session_id"], lease_seconds=900)
            session_b = helpers.init_session(workspace)

            result = run_checkpoint(
                workspace,
                task_id,
                session_b["session_id"],
                "renew-lease", "--lease-seconds", "900",
            )
            self.assertEqual(result.returncode, lib.LeaseConflict.code)
            state = helpers.state_of(workspace, task_id)
            self.assertEqual(state["owner_session"], session_a["session_id"])

    def test_expired_lease_allows_resume_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_id = "task-20261001-000005-expired"
            helpers.create_task(workspace, task_id, session_id=session_a["session_id"], lease_seconds=900)
            session_b = helpers.init_session(workspace)
            helpers.expire_lease(workspace, task_id)

            result = helpers.run_json(
                "resume.py",
                "--workspace", str(workspace),
                "--session-id", session_b["session_id"],
                "--task", task_id,
            )
            self.assertEqual(result["owner_session"], session_b["session_id"])
            self.assertTrue(result["lease_expires_at"])
            events = helpers.events_of(workspace, task_id)
            takeover = [event for event in events if event["type"] == "TASK_TAKEOVER"]
            self.assertEqual(len(takeover), 1)
            self.assertTrue(takeover[0]["lease_expired"])
            self.assertFalse(takeover[0]["forced"])
            self.assertEqual(takeover[0]["from_session"], session_a["session_id"])
            self.assertEqual(takeover[0]["to_session"], session_b["session_id"])

    def test_valid_lease_normal_resume_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_id = "task-20261001-000006-blocked-resume"
            helpers.create_task(workspace, task_id, session_id=session_a["session_id"], lease_seconds=900)
            session_b = helpers.init_session(workspace)

            result = helpers.run_script(
                "resume.py",
                "--workspace", str(workspace),
                "--session-id", session_b["session_id"],
                "--task", task_id,
                check=False,
            )
            self.assertEqual(result.returncode, lib.LeaseConflict.code)
            self.assertIn("lease_conflict", result.stderr)
            self.assertEqual(helpers.state_of(workspace, task_id)["owner_session"], session_a["session_id"])

    def test_explicit_force_takeover_recovers_crash_with_running_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_id = "task-20261001-000007-force"
            helpers.create_task(workspace, task_id, session_id=session_a["session_id"], lease_seconds=900)
            helpers.cp_json(workspace, task_id, "add-step", "--title", "Side effect", session_id=session_a["session_id"])
            helpers.cp_json(workspace, task_id, "start-step", "--step", "step-1", session_id=session_a["session_id"])
            helpers.cp_json(
                workspace,
                task_id,
                "start-operation", "--description", "Write external artifact",
                session_id=session_a["session_id"],
            )
            # Session A disappears while lease is still valid.
            session_b = helpers.init_session(workspace)

            blocked = helpers.run_script(
                "resume.py",
                "--workspace", str(workspace),
                "--session-id", session_b["session_id"],
                "--task", task_id,
                check=False,
            )
            self.assertEqual(blocked.returncode, lib.LeaseConflict.code)

            taken = helpers.run_json(
                "resume.py",
                "--workspace", str(workspace),
                "--session-id", session_b["session_id"],
                "--task", task_id,
                "--force-takeover",
            )
            self.assertEqual(taken["owner_session"], session_b["session_id"])
            self.assertTrue(taken["dirty"])
            self.assertEqual(taken["active_operation"], "op-1")
            self.assertEqual(taken["operations"][0]["state"], "outcome_unknown")
            events = helpers.events_of(workspace, task_id)
            takeover = [event for event in events if event["type"] == "TASK_TAKEOVER"][-1]
            self.assertFalse(takeover["lease_expired"])
            self.assertTrue(takeover["forced"])

            # Owner B can inspect and finish the unresolved operation.
            helpers.cp_json(
                workspace,
                task_id,
                "finish-operation",
                "--operation-id", "op-1",
                "--outcome", "succeeded",
                "--result-summary", "Artifact exists after external inspection",
                session_id=session_b["session_id"],
            )
            helpers.cp_json(
                workspace,
                task_id,
                "complete-step", "--step", "step-1", "--summary", "Recovered",
                session_id=session_b["session_id"],
            )
            state = helpers.state_of(workspace, task_id)
            self.assertFalse(state["dirty"])
            self.assertEqual(state["operations"][0]["state"], "succeeded")
            self.assertEqual(state["steps"][0]["status"], "completed")

    def test_unowned_task_requires_attach_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = helpers.init_session(workspace)
            task_id = "task-20261001-000008-unowned"
            created = helpers.run_json(
                "checkpoint.py",
                "--workspace", str(workspace),
                "create-task",
                "--task-id", task_id,
                "--title", "Unowned",
                "--objective", "Require attachment.",
            )
            self.assertEqual(created["owner_session"], None)

            rejected = run_checkpoint(workspace, task_id, session["session_id"], "add-step", "--title", "Blocked")
            self.assertEqual(rejected.returncode, lib.LeaseConflict.code)
            self.assertIn("unowned", rejected.stderr)

            attached = run_checkpoint(workspace, task_id, session["session_id"], "attach-session", "--session-id", session["session_id"])
            self.assertEqual(attached.returncode, 0, attached.stderr)
            allowed = run_checkpoint(workspace, task_id, session["session_id"], "add-step", "--title", "Allowed")
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual(helpers.state_of(workspace, task_id)["owner_session"], session["session_id"])

    def test_attach_force_takeover_and_detach_owner_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_id = "task-20261001-000009-attach-detach"
            helpers.create_task(workspace, task_id, session_id=session_a["session_id"], lease_seconds=900)
            session_b = helpers.init_session(workspace)

            blocked = run_checkpoint(workspace, task_id, session_b["session_id"], "attach-session")
            self.assertEqual(blocked.returncode, lib.LeaseConflict.code)

            forced = run_checkpoint(workspace, task_id, session_b["session_id"], "attach-session", "--force")
            self.assertEqual(forced.returncode, 0, forced.stderr)
            self.assertEqual(helpers.state_of(workspace, task_id)["owner_session"], session_b["session_id"])
            takeover = [event for event in helpers.events_of(workspace, task_id) if event["type"] == "TASK_TAKEOVER"][-1]
            self.assertFalse(takeover["lease_expired"])
            self.assertTrue(takeover["forced"])

            wrong_detach = run_checkpoint(workspace, task_id, session_a["session_id"], "detach-session")
            self.assertEqual(wrong_detach.returncode, lib.LeaseConflict.code)

            detached = run_checkpoint(workspace, task_id, session_b["session_id"], "detach-session")
            self.assertEqual(detached.returncode, 0, detached.stderr)
            state = helpers.state_of(workspace, task_id)
            self.assertIsNone(state["owner_session"])
            self.assertIsNone(state["lease_expires_at"])

            unowned = run_checkpoint(workspace, task_id, session_b["session_id"], "set-next-actions", "--action", "blocked")
            self.assertEqual(unowned.returncode, lib.LeaseConflict.code)

    def test_native_session_id_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = helpers.init_session(workspace, native_session_id="runtime-123")
            second = helpers.init_session(workspace, native_session_id="runtime-123")
            self.assertEqual(first["session_id"], second["session_id"])
            self.assertTrue(first["session_created"])
            self.assertFalse(second["session_created"])
            self.assertEqual(len(helpers.sessions(workspace)), 1)

    def test_duplicate_native_session_id_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace, session_id="sr-20261001-000010-aaaa", native_session_id="runtime-X")
            helpers.init_session(workspace, session_id="sr-20261001-000011-bbbb", native_session_id="runtime-X")
            result = helpers.run_script(
                "init.py",
                "--workspace", str(workspace),
                "--native-session-id", "runtime-X",
                check=False,
            )
            self.assertEqual(result.returncode, lib.SessionIdentityConflict.code)
            self.assertIn("session_identity_conflict", result.stderr)
            self.assertEqual(len(helpers.sessions(workspace)), 2)

    def test_multiple_sessions_independent_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            session_b = helpers.init_session(workspace)
            task_x = "task-20261001-000012-x"
            task_y = "task-20261001-000013-y"
            helpers.create_task(workspace, task_x, session_id=session_a["session_id"])
            helpers.create_task(workspace, task_y, session_id=session_b["session_id"])

            own = helpers.cp_json(workspace, task_x, "set-next-actions", "--action", "A only", session_id=session_a["session_id"])
            self.assertEqual(own["task_id"], task_x)
            foreign = run_checkpoint(workspace, task_y, session_a["session_id"], "set-next-actions", "--action", "A cannot")
            self.assertEqual(foreign.returncode, lib.LeaseConflict.code)
            other = helpers.cp_json(workspace, task_y, "set-next-actions", "--action", "B only", session_id=session_b["session_id"])
            self.assertEqual(other["task_id"], task_y)


if __name__ == "__main__":
    unittest.main()
