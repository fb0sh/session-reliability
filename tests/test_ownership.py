from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

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
            helpers.create_task(workspace, task_id, session_id=session["session_id"], lease_seconds=30)
            before = helpers.state_of(workspace, task_id)
            self.assertEqual(before["owner_session"], session["session_id"])
            self.assertEqual(before["lease_duration_seconds"], 30)

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
            self.assertEqual(after["lease_duration_seconds"], 30)
            renewed_seconds = (
                lib.parse_iso(after["lease_expires_at"]) - lib.parse_iso(after["updated_at"])
            ).total_seconds()
            self.assertGreaterEqual(renewed_seconds, 20)
            self.assertLessEqual(renewed_seconds, 35)
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

    def test_takeover_preserves_unrelated_old_session_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_x = "task-20261001-000020-isolation-x"
            task_y = "task-20261001-000021-isolation-y"
            helpers.create_task(workspace, task_x, session_id=session_a["session_id"])
            helpers.create_task(workspace, task_y, session_id=session_a["session_id"])
            self.assertEqual(
                json.loads(Path(session_a["session_file"]).read_text(encoding="utf-8"))["active_task"],
                task_y,
            )
            helpers.expire_lease(workspace, task_x, touch_session=False)
            session_b = helpers.init_session(workspace)

            helpers.run_json(
                "resume.py",
                "--workspace", str(workspace),
                "--session-id", session_b["session_id"],
                "--task", task_x,
            )

            self.assertEqual(helpers.state_of(workspace, task_x)["owner_session"], session_b["session_id"])
            session_a_after = json.loads(Path(session_a["session_file"]).read_text(encoding="utf-8"))
            session_b_after = json.loads(Path(session_b["session_file"]).read_text(encoding="utf-8"))
            self.assertEqual(session_a_after["status"], "active")
            self.assertEqual(session_a_after["active_task"], task_y)
            self.assertEqual(session_b_after["active_task"], task_x)

    def test_takeover_unbinds_matching_old_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_a = helpers.init_session(workspace)
            task_id = "task-20261001-000022-unbind"
            helpers.create_task(workspace, task_id, session_id=session_a["session_id"])
            self.assertEqual(
                json.loads(Path(session_a["session_file"]).read_text(encoding="utf-8"))["active_task"],
                task_id,
            )
            helpers.expire_lease(workspace, task_id, touch_session=False)
            session_b = helpers.init_session(workspace)

            helpers.run_json(
                "resume.py",
                "--workspace", str(workspace),
                "--session-id", session_b["session_id"],
                "--task", task_id,
            )

            session_a_after = json.loads(Path(session_a["session_file"]).read_text(encoding="utf-8"))
            session_b_after = json.loads(Path(session_b["session_file"]).read_text(encoding="utf-8"))
            self.assertIsNone(session_a_after["active_task"])
            self.assertEqual(session_a_after["status"], "active")
            self.assertEqual(session_b_after["active_task"], task_id)

    def test_resume_and_attach_takeover_are_consistent(self) -> None:
        def scenario(use_attach: bool) -> dict[str, Any]:
            tmp = tempfile.TemporaryDirectory()
            workspace = Path(tmp.name)
            session_a = helpers.init_session(workspace)
            task_id = "task-20261001-000023-consistency"
            helpers.create_task(workspace, task_id, session_id=session_a["session_id"], lease_seconds=900)
            helpers.expire_lease(workspace, task_id, touch_session=False)
            session_b = helpers.init_session(workspace)

            if use_attach:
                result = run_checkpoint(workspace, task_id, session_b["session_id"], "attach-session")
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                helpers.run_json(
                    "resume.py",
                    "--workspace", str(workspace),
                    "--session-id", session_b["session_id"],
                    "--task", task_id,
                )

            state = helpers.state_of(workspace, task_id)
            session_a_after = json.loads(Path(session_a["session_file"]).read_text(encoding="utf-8"))
            session_b_after = json.loads(Path(session_b["session_file"]).read_text(encoding="utf-8"))
            takeover = [event for event in helpers.events_of(workspace, task_id) if event["type"] == "TASK_TAKEOVER"][-1]
            result = {
                "owner_is_new": state["owner_session"] == session_b["session_id"],
                "lease_duration_seconds": state["lease_duration_seconds"],
                "old_active_task": session_a_after["active_task"],
                "old_status": session_a_after["status"],
                "new_active_task": session_b_after["active_task"],
                "from_is_old": takeover["from_session"] == session_a["session_id"],
                "to_is_new": takeover["to_session"] == session_b["session_id"],
                "lease_expired": takeover["lease_expired"],
                "forced": takeover["forced"],
            }
            tmp.cleanup()
            return result

        resume_result = scenario(use_attach=False)
        attach_result = scenario(use_attach=True)
        self.assertEqual(resume_result, attach_result)

    def test_native_rebind_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = helpers.init_session(
                workspace,
                session_id="sr-20261001-000024-rebind",
                native_session_id="runtime-X",
            )
            result = helpers.run_script(
                "init.py",
                "--workspace", str(workspace),
                "--session-id", first["session_id"],
                "--native-session-id", "runtime-Y",
                check=False,
            )
            self.assertEqual(result.returncode, lib.SessionIdentityConflict.code)
            session = json.loads(Path(first["session_file"]).read_text(encoding="utf-8"))
            self.assertEqual(session["native_session_id"], "runtime-X")

    def test_null_native_binding_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = helpers.init_session(workspace, session_id="sr-20261001-000025-null")
            second = helpers.init_session(
                workspace,
                session_id=first["session_id"],
                native_session_id="runtime-X",
            )
            self.assertEqual(second["session_id"], first["session_id"])
            session = json.loads(Path(second["session_file"]).read_text(encoding="utf-8"))
            self.assertEqual(session["native_session_id"], "runtime-X")

    def test_renew_lease_changes_heartbeat_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = helpers.init_session(workspace)
            task_id = "task-20261001-000026-renew-duration"
            helpers.create_task(workspace, task_id, session_id=session["session_id"], lease_seconds=900)

            helpers.cp_json(
                workspace,
                task_id,
                "renew-lease", "--lease-seconds", "45",
                session_id=session["session_id"],
            )
            helpers.cp_json(
                workspace,
                task_id,
                "add-step", "--title", "After renew",
                session_id=session["session_id"],
            )
            state = helpers.state_of(workspace, task_id)
            self.assertEqual(state["lease_duration_seconds"], 45)
            renewed_seconds = (
                lib.parse_iso(state["lease_expires_at"]) - lib.parse_iso(state["updated_at"])
            ).total_seconds()
            self.assertGreaterEqual(renewed_seconds, 40)
            self.assertLessEqual(renewed_seconds, 50)

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
            first = helpers.init_session(
                workspace,
                session_id="sr-20261001-000010-aaaa",
                native_session_id="runtime-X",
            )
            # Simulate a pre-existing metadata conflict without using init,
            # because init now correctly refuses to create the duplicate.
            second = {
                "schema_version": 1,
                "session_id": "sr-20261001-000011-bbbb",
                "native_session_id": "runtime-X",
                "status": "active",
                "active_task": None,
                "started_at": "2026-10-01T00:00:00+00:00",
                "last_seen_at": "2026-10-01T00:00:00+00:00",
            }
            session_path = helpers.store_dir(workspace) / "sessions" / "sr-20261001-000011-bbbb.json"
            session_path.write_text(json.dumps(second, indent=2) + "\n", encoding="utf-8")

            result = helpers.run_script(
                "init.py",
                "--workspace", str(workspace),
                "--native-session-id", "runtime-X",
                check=False,
            )
            self.assertEqual(result.returncode, lib.SessionIdentityConflict.code)
            self.assertIn("session_identity_conflict", result.stderr)
            self.assertEqual(len(helpers.sessions(workspace)), 2)
            unchanged = json.loads(Path(first["session_file"]).read_text(encoding="utf-8"))
            self.assertEqual(unchanged["native_session_id"], "runtime-X")

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
