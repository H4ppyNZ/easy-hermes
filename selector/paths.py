"""selector.paths — runtime path state, constants, and probe-cache file I/O.

Split out of the original monolithic select-models.py.  This module is the
single owner of mutable runtime state (paths that ``--hermes-home`` can
rebind, and the LIST_ONLY flag); every other module reads it through
``paths.<NAME>`` so rebinds are visible everywhere.  Function bodies are
moved verbatim from select-models.py.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (LOCALAPPDATA / "hermes"))
CONFIG_PATH = HERMES_HOME / "config.yaml"
CACHE_PATH = HERMES_HOME / "cache" / "free_model_probe_cache.json"
LAST_PASSING_PATH = REPO_DIR / "out" / "last_selection.json"
BANNED_PATH = HERMES_HOME / "cache" / "banned_models.json"
BOOSTED_PATH = HERMES_HOME / "cache" / "boosted_models.json"
PROMOTION_HISTORY_PATH = HERMES_HOME / "cache" / "promotion_history.json"

LIST_ONLY = False

CACHE_TTL_DAYS = 7
REASONING_PRIORITY = ["max", "xhigh", "high", "medium", "low", "minimal", "none"]
PROBE_MAX_TOKENS = 2000
PROBE_RETRY_MAX_TOKENS = 8000

DEFAULT_RANKING_WEIGHTS = {
    "release_date": 0.40,
    "context_window": 0.30,
    "provider_priority": 0.30,
    "reasoning": 0.0,
    "response_time": 0.0,
    "boost": 0.0,
}
DEFAULT_PROVIDER_PRIORITY = {
    "openrouter": 1.0,
    "gemini": 1.0,
    "nvidia": 1.0,
    "novita": 1.0,
    "ollama-cloud": 1.0,
    "ollama-local": 1.0,
    "nous": 1.0,
}
PROMOTION_MARGIN = 2
POLL_COOLDOWN_SECONDS = float(os.environ.get("EASY_HERMES_POLL_COOLDOWN", "1"))
HYSTERESIS_DAYS = 7
HISTORY_PRUNE_DAYS = 30


def rebind_hermes_home(new_home):
    """Rebind every home-derived path (used by --hermes-home)."""
    global HERMES_HOME, CONFIG_PATH, CACHE_PATH, BANNED_PATH, BOOSTED_PATH, PROMOTION_HISTORY_PATH
    HERMES_HOME = Path(new_home)
    CONFIG_PATH = HERMES_HOME / "config.yaml"
    CACHE_PATH = HERMES_HOME / "cache" / "free_model_probe_cache.json"
    BANNED_PATH = HERMES_HOME / "cache" / "banned_models.json"
    BOOSTED_PATH = HERMES_HOME / "cache" / "boosted_models.json"
    PROMOTION_HISTORY_PATH = HERMES_HOME / "cache" / "promotion_history.json"


def load_cache():
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"entries": {}}


def save_cache(cache):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        import atomic_io
        atomic_io.atomic_write_json(CACHE_PATH, cache, indent=2)
    except Exception as e:
        print(f"  (cache write failed: {e})", file=sys.stderr)


def save_last_selection(models):
    try:
        LAST_PASSING_PATH.parent.mkdir(parents=True, exist_ok=True)
        import atomic_io
        atomic_io.atomic_write_json(LAST_PASSING_PATH, models, indent=2, default=str)
    except Exception as e:
        print(f"  (last selection write failed: {e})", file=sys.stderr)


def load_last_selection():
    try:
        if LAST_PASSING_PATH.exists():
            return json.loads(LAST_PASSING_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None
