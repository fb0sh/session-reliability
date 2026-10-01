"""Worker process used by tests/test_locking.py.

It acquires a ProcessLock, signals readiness, then either waits for a go file
(normal release) or sleeps until terminated (crash-release simulation).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import lib  # noqa: E402


def main() -> None:
    lock_path = Path(sys.argv[1])
    ready_path = Path(sys.argv[2])
    go_path = Path(sys.argv[3])
    mode = sys.argv[4] if len(sys.argv) > 4 else "hold"

    with lib.ProcessLock(lock_path, timeout=10):
        ready_path.write_text("locked", encoding="utf-8")
        if mode == "crash":
            while True:
                time.sleep(1)
        else:
            while not go_path.exists():
                time.sleep(0.001)


if __name__ == "__main__":
    main()
