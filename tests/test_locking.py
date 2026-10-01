from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402

SCRIPTS = helpers.SCRIPTS
sys.path.insert(0, str(SCRIPTS))
import lib  # noqa: E402

WORKER = Path(__file__).resolve().parent / "lock_worker.py"


class LockingTests(unittest.TestCase):
    def _start_worker(self, lock_path: Path, ready_path: Path, go_path: Path, mode: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                sys.executable,
                str(WORKER),
                str(lock_path),
                str(ready_path),
                str(go_path),
                mode,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _wait_for_ready(self, ready_path: Path, process: subprocess.Popen[str], timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not ready_path.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(f"lock worker exited before readiness: rc={process.returncode}\n{stdout}\n{stderr}")
            if time.monotonic() > deadline:
                process.kill()
                self.fail("lock worker did not become ready")
            time.sleep(0.005)

    def _terminate_worker(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        # Drain and close pipes so killed workers do not leak descriptors.
        try:
            process.communicate(timeout=5)
        except Exception:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def test_lock_blocks_second_process_until_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            lock_path = base / "contention.lock"
            ready_path = base / "ready"
            go_path = base / "go"
            process = self._start_worker(lock_path, ready_path, go_path, "hold")
            try:
                self._wait_for_ready(ready_path, process)
                with self.assertRaises(lib.StoreError):
                    with lib.ProcessLock(lock_path, timeout=0.5):
                        self.fail("second process acquired a held lock")
                go_path.write_text("go", encoding="utf-8")
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr)
                with lib.ProcessLock(lock_path, timeout=5):
                    pass
                self.assertTrue(lock_path.exists())
            finally:
                if process.poll() is None:
                    self._terminate_worker(process)

    def test_lock_releases_after_process_termination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            lock_path = base / "crash.lock"
            ready_path = base / "ready"
            go_path = base / "go"
            process = self._start_worker(lock_path, ready_path, go_path, "crash")
            self._wait_for_ready(ready_path, process)
            self._terminate_worker(process)
            self.assertTrue(lock_path.exists(), "kernel lock anchor file should remain")

            with lib.ProcessLock(lock_path, timeout=5):
                pass

    def test_task_mutation_after_crashed_lock_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = helpers.init_session(workspace)
            task_id = "task-20261001-000040-crash-lock"
            helpers.create_task(workspace, task_id, session_id=session["session_id"])
            store = lib.resolve_store(None, workspace=workspace)
            lock_path = lib.task_lock_path(store, task_id)
            ready_path = Path(tmp) / "task-ready"
            go_path = Path(tmp) / "task-go"

            process = self._start_worker(lock_path, ready_path, go_path, "crash")
            self._wait_for_ready(ready_path, process)
            self._terminate_worker(process)

            result = helpers.cp_json(
                workspace,
                task_id,
                "add-step", "--title", "After crash",
                session_id=session["session_id"],
            )
            self.assertEqual(result["task_id"], task_id)
            state = helpers.state_of(workspace, task_id)
            self.assertEqual(state["steps"][0]["title"], "After crash")

    def test_identity_initialization_after_crashed_identity_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            store = lib.resolve_store(None, workspace=workspace)
            lib.ensure_store(store)
            lock_path = lib.session_identity_lock_path(store)
            ready_path = Path(tmp) / "identity-ready"
            go_path = Path(tmp) / "identity-go"

            process = self._start_worker(lock_path, ready_path, go_path, "crash")
            self._wait_for_ready(ready_path, process)
            self._terminate_worker(process)

            result = helpers.run_json(
                "init.py",
                "--workspace", str(workspace),
                "--native-session-id", "runtime-after-crash",
            )
            self.assertTrue(result["session_id"].startswith("sr-"))


if __name__ == "__main__":
    unittest.main()
