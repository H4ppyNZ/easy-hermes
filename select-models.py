#!/usr/bin/env python3
"""Central free-model selector — thin entry point.

The implementation lives in the ``selector`` package:

    selector/paths.py       runtime path state, constants, probe-cache file I/O
    selector/lists.py       banned / boosted model lists
    selector/probe_cache.py probe-result cache (TTL by failure class)
    selector/history.py     promotion history + the promotion gate
    selector/ranking.py     release dates, capabilities, ranking matrix
    selector/probing.py     provider fetch + chat probes
    selector/configio.py    config.yaml load + merge write
    selector/runner.py      per-provider processing pipeline and main()

This file keeps the original command-line interface and re-exports every
public name, so the scheduled tasks (``python select-models.py ...``) and any
existing imports keep working unchanged.

Runtime path state (``CONFIG_PATH``, ``CACHE_PATH``, ...) lives in
``selector.paths`` and is rebound there (see ``--hermes-home``).  Assigning a
mirrored constant on this module is forwarded to ``selector.paths`` so the
implementation sees it (tests do ``select_models.CONFIG_PATH = tmp``).
"""
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

REPO_DIR = Path(__file__).resolve().parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from selector import paths as _paths  # noqa: E402
from selector.lists import (  # noqa: F401,E402
    is_banned, load_banned, load_boosted, save_banned, save_boosted)
from selector.probe_cache import (  # noqa: F401,E402
    _failure_class, _ttl_days, cache_key, cached_result, put_cache)
from selector.history import (  # noqa: F401,E402
    _blank_promo_record, _model_id, _release_date, choose_default,
    load_promotion_history, promo_streak, prune_promotion_history,
    save_promotion_history, show_promotion_history, show_response_times,
    update_promotion_history)
from selector.ranking import (  # noqa: F401,E402
    _OR_CONTEXT_MAP, _OR_CTX_LOCK, _get_or_context_map, _hf_context,
    _normalize_key, _to_ts, approximate_context, build_ranking_from_mr,
    extract_capabilities, extract_release_date, load_ranking_config,
    rank_models)
from selector.probing import (  # noqa: F401,E402
    _extract_elapsed_ms, _gemini_probe, _ollama_local_probe, _openai_probe,
    fetch_provider, get_reasoning_efforts, probe, probe_reasoning_levels,
    reasoning_score_from_note)
from selector.configio import load_config, write_config  # noqa: F401,E402
from selector.runner import _process_provider, main  # noqa: F401,E402

# --- constants (read-only mirrors of selector.paths) --------------------------
LOCALAPPDATA = _paths.LOCALAPPDATA
HERMES_HOME = _paths.HERMES_HOME
CONFIG_PATH = _paths.CONFIG_PATH
CACHE_PATH = _paths.CACHE_PATH
LAST_PASSING_PATH = _paths.LAST_PASSING_PATH
BANNED_PATH = _paths.BANNED_PATH
BOOSTED_PATH = _paths.BOOSTED_PATH
PROMOTION_HISTORY_PATH = _paths.PROMOTION_HISTORY_PATH
LIST_ONLY = _paths.LIST_ONLY
CACHE_TTL_DAYS = _paths.CACHE_TTL_DAYS
REASONING_PRIORITY = _paths.REASONING_PRIORITY
PROBE_MAX_TOKENS = _paths.PROBE_MAX_TOKENS
PROBE_RETRY_MAX_TOKENS = _paths.PROBE_RETRY_MAX_TOKENS
DEFAULT_RANKING_WEIGHTS = _paths.DEFAULT_RANKING_WEIGHTS
DEFAULT_PROVIDER_PRIORITY = _paths.DEFAULT_PROVIDER_PRIORITY
PROMOTION_MARGIN = _paths.PROMOTION_MARGIN
POLL_COOLDOWN_SECONDS = _paths.POLL_COOLDOWN_SECONDS
HYSTERESIS_DAYS = _paths.HYSTERESIS_DAYS
HISTORY_PRUNE_DAYS = _paths.HISTORY_PRUNE_DAYS

# --- module __setattr__ mirror ------------------------------------------------
# ``select_models.CONFIG_PATH = tmp`` (tests) or ``select_models.LIST_ONLY = True``
# must reach the implementation, which reads the constants from selector.paths.
_MIRRORED = ("HERMES_HOME", "CONFIG_PATH", "CACHE_PATH", "LAST_PASSING_PATH",
             "BANNED_PATH", "BOOSTED_PATH", "PROMOTION_HISTORY_PATH", "LIST_ONLY")


class _FacadeModule(type(sys)):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in _MIRRORED:
            setattr(_paths, name, value)


_self = sys.modules.get(__name__)
if _self is not None and getattr(_self, "__file__", None) == str(REPO_DIR / "select-models.py"):
    _self.__class__ = _FacadeModule


if __name__ == "__main__":
    main()
