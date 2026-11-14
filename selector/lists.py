"""selector.lists — extracted from select-models.py

Split out of the original monolithic select-models.py.  Function bodies are
moved verbatim; only module-level path state is routed through
``selector.paths`` so it stays mutable at runtime.
"""
from __future__ import annotations

from selector import paths
import json
import sys


def load_banned():
    try:
        import atomic_io
        with atomic_io.file_lock(str(paths.BANNED_PATH) + ".lock"):
            if paths.BANNED_PATH.exists():
                data = json.loads(paths.BANNED_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return set(str(x) for x in data)
                if isinstance(data, dict):
                    return set(str(x) for x in data.get("banned", []))
    except Exception:
        pass
    return set()


def save_banned(banned):
    try:
        import atomic_io
        with atomic_io.file_lock(str(paths.BANNED_PATH) + ".lock"):
            atomic_io.atomic_write_json(paths.BANNED_PATH, sorted(banned), indent=2)
    except Exception as e:
        print(f"  (banned list write failed: {e})", file=sys.stderr)


def load_boosted():
    try:
        if paths.BOOSTED_PATH.exists():
            data = json.loads(paths.BOOSTED_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(str(x) for x in data)
            if isinstance(data, dict):
                return set(str(x) for x in data.get("boosted", []))
    except Exception:
        pass
    return set()


def save_boosted(boosted):
    try:
        import atomic_io
        with atomic_io.file_lock(str(paths.BOOSTED_PATH) + ".lock"):
            atomic_io.atomic_write_json(paths.BOOSTED_PATH, sorted(boosted), indent=2)
    except Exception as e:
        print(f"  (boosted list write failed: {e})", file=sys.stderr)


def is_banned(mid, banned):
    return str(mid) in banned