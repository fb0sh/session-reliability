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


def _corrupt_session(session_id: str, native_session_id: str | None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "session_id": session_id,
        "native_session_id": native_session_id,
        "status": "INVALID",
        "active_task": None,
        "started_at": "2026-10-01T00:00:00+00:00",
        "last_seen_at": "2026-10-01T00:00:00+00:00",
    }


class IdentityCorruptionTests(unittest.TestCase):
    def _write_corrupt(self, workspace: Path, session_id: str, native_id: str | None) -> tuple[Path, bytes]:
        path = helpers.store_dir(workspace) / "sessions" / f"{session_id}.json"
        original = (json.dumps(_corrupt_session(session_id, native_id), indent=2) + "\n").encode()
        path.write_bytes(original)
        return path, original

    def test_corrupt_native_claim_blocks_new_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            session_path, original = self._write_corrupt(workspace, "sr-corrupt", "runtime-corrupt")
            sessions_before = {path.name for path in (helpers.store_dir(workspace) / "sessions").glob("*.json")}

            result = helpers.run_script(
                "init.py",
                "--workspace", str(workspace),
                "--native-session-id", "runtime-corrupt",
                check=False,
            )
            self.assertEqual(result.returncode, lib.SessionIdentityCorruption.code)
            self.assertIn("session_identity_corruption", result.stderr)
            self.assertEqual(session_path.read_bytes(), original)
            sessions_after = {path.name for path in (helpers.store_dir(workspace) / "sessions").glob("*.json")}
            self.assertEqual(sessions_after, sessions_before)

    def test_explicit_other_session_cannot_steal_corrupt_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            session_path, original = self._write_corrupt(workspace, "sr-corrupt", "runtime-corrupt")

            result = helpers.run_script(
                "init.py",
                "--workspace", str(workspace),
                "--session-id", "sr-other",
                "--native-session-id", "runtime-corrupt",
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(session_path.read_bytes(), original)
            self.assertFalse((helpers.store_dir(workspace) / "sessions" / "sr-other.json").exists())

    def test_same_claimant_may_recover_corrupt_native_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            session_path, _original = self._write_corrupt(workspace, "sr-corrupt", "runtime-corrupt")

            result = helpers.run_json(
                "init.py",
                "--workspace", str(workspace),
                "--session-id", "sr-corrupt",
                "--native-session-id", "runtime-corrupt",
            )
            self.assertEqual(result["session_id"], "sr-corrupt")
            recovered = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(recovered["session_id"], "sr-corrupt")
            self.assertEqual(recovered["native_session_id"], "runtime-corrupt")
            self.assertEqual(recovered["status"], "active")
            backups = list(session_path.parent.glob("sr-corrupt.json.corrupt.*.bak"))
            self.assertEqual(len(backups), 1)
            matches = [
                session
                for session in helpers.sessions(workspace)
                if session.get("native_session_id") == "runtime-corrupt"
            ]
            self.assertEqual(len(matches), 1)

    def test_same_claimant_different_native_rebind_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            session_path, original = self._write_corrupt(workspace, "sr-corrupt", "runtime-old")

            result = helpers.run_script(
                "init.py",
                "--workspace", str(workspace),
                "--session-id", "sr-corrupt",
                "--native-session-id", "runtime-new",
                check=False,
            )
            self.assertEqual(result.returncode, lib.SessionIdentityConflict.code)
            self.assertEqual(session_path.read_bytes(), original)

    def test_unrelated_corrupt_claim_does_not_block_other_native_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            corrupt_path, original = self._write_corrupt(workspace, "sr-corrupt", "runtime-A")

            result = helpers.run_json(
                "init.py",
                "--workspace", str(workspace),
                "--native-session-id", "runtime-B",
            )
            self.assertTrue(result["session_id"].startswith("sr-"))
            self.assertEqual(corrupt_path.read_bytes(), original)
            matches = [
                session
                for session in helpers.sessions(workspace)
                if session.get("native_session_id") == "runtime-B"
            ]
            self.assertEqual(len(matches), 1)

    def test_unparseable_session_does_not_contribute_native_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            helpers.init_session(workspace)
            broken = helpers.store_dir(workspace) / "sessions" / "broken.json"
            broken.write_text("{{broken", encoding="utf-8")

            result = helpers.run_json(
                "init.py",
                "--workspace", str(workspace),
                "--native-session-id", "runtime-new",
            )
            self.assertTrue(result["session_id"].startswith("sr-"))
            self.assertEqual(broken.read_text(encoding="utf-8"), "{{broken")


if __name__ == "__main__":
    unittest.main()
