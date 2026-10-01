"""Subprocess gate helper for native identity race tests.

The parent starts several workers, waits until all have created their ready
file, then creates the shared go file.  Each worker then execs init.py.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> None:
    ready_path = Path(sys.argv[1])
    go_path = Path(sys.argv[2])
    script_path = sys.argv[3]
    script_args = sys.argv[4:]

    ready_path.write_text("ready", encoding="utf-8")
    while not go_path.exists():
        time.sleep(0.001)

    os.execv(sys.executable, [sys.executable, script_path, *script_args])


if __name__ == "__main__":
    main()
