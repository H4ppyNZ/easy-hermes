"""Atomic file-write helpers (write to temp then os.replace)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


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
