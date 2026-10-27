"""Atomic file-write helpers (write to temp then os.replace)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class _FileLock:
    def __init__(self, path, timeout=30.0, poll=0.1):
        self.path = Path(path)
        self.timeout = timeout
        self.poll = poll
        self._fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                if msvcrt is not None:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return self
            except (OSError, IOError):
                os.close(fd)
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not acquire lock: {self.path}")
                time.sleep(self.poll)

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                if msvcrt is not None:
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        return False


def file_lock(path, timeout=30.0):
    return _FileLock(path, timeout=timeout)


def atomic_write_text(path, content, encoding="utf-8"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def atomic_write_json(path, obj, indent=2, **kw):
    atomic_write_text(path, json.dumps(obj, indent=indent, **kw))
