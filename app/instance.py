from __future__ import annotations

import os
import sys
from pathlib import Path


class InstanceLock:
    """OS-owned advisory lock, automatically released after a crash."""

    def __init__(self, root: Path):
        self.root = root
        self.file = None

    def acquire(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = (self.root / ".instance.lock").open("a+b")
        if self.file.seek(0, 2) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close()
            self.file = None
            (self.root / ".show-window").write_text(str(os.getpid()))
            return False

    def release(self):
        if self.file:
            self.file.close()
            self.file = None
