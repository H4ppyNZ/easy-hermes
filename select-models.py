#!/usr/bin/env python3
"""Central free-model selector (Phase 2)."""
import argparse
import json
import os
import sys
import re
import time
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (LOCALAPPDATA / "hermes"))
CONFIG_PATH = HERMES_HOME / "config.yaml"
CACHE_PATH = HERMES_HOME / "cache" / "free_model_probe_cache.json"
REPO_DIR = Path(__file__).resolve().parent
LAST_PASSING_PATH = REPO_DIR / "out" / "last_selection.json"
sys.path.insert(0, str(REPO_DIR))

import providers
try:
    import chain_delta
except Exception:
    chain_delta = None

CACHE_TTL_DAYS = 7
REASONING_PRIORITY = ["max", "xhigh", "high", "medium", "low", "minimal", "none"]
PROBE_MAX_TOKENS = 2000
PROBE_RETRY_MAX_TOKENS = 8000


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


def build_ranking_from_mr(mr):
    if not isinstance(mr, dict):
        mr = {}
    weights = mr.get("weights")
    if not isinstance(weights, dict):
        weights = dict(DEFAULT_RANKING_WEIGHTS)
    else:
        merged_w = dict(DEFAULT_RANKING_WEIGHTS)
        merged_w.update(weights)
        weights = merged_w
    try:
        total = sum(float(v) for v in weights.values())
    except (TypeError, ValueError):
        total = 0.0
    if total > 0:
        weights = {k: float(v) / total for k, v in weights.items()}
    else:
        weights = dict(DEFAULT_RANKING_WEIGHTS)
    pp = mr.get("provider_priority")
    if not isinstance(pp, dict):
        pp = dict(DEFAULT_PROVIDER_PRIORITY)
    else:
        merged = dict(DEFAULT_PROVIDER_PRIORITY)
        merged.update(pp)
        pp = merged
    try:
        missing = float(mr.get("missing_release_date", 0.0))
    except (TypeError, ValueError):
        missing = 0.0
    return {"weights": weights, "provider_priority": pp, "missing_release_date": missing}


def _model_id(m):
    if isinstance(m, dict):
        return str(m.get("id") or m.get("model") or m.get("name"))
    return str(m)


def _release_date(m):
    if isinstance(m, dict):
        return m.get("_release_date") or m.get("release_date")
    return None


def load_promotion_history():
    try:
        if PROMOTION_HISTORY_PATH.exists():
            data = json.loads(PROMOTION_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_promotion_history(history):
    try:
        import atomic_io
        atomic_io.atomic_write_json(PROMOTION_HISTORY_PATH, history, indent=2)
    except Exception as e:
        print(f"  (promotion history write failed: {e})", file=sys.stderr)


def _blank_promo_record(today):
    return {
        "first_seen": today,
        "last_seen": today,
        "last_pass": None,
        "last_clean": None,
        "last_fail": None,
        "last_fail_note": None,
        "consecutive_clean_passes": 0,
        "total_runs": 0,
        "clean_runs": 0,
        "unclean_runs": 0,
        "failed_runs": 0,
        "missed_runs": 0,
        "promotion_count": 0,
        "promoted_at": None,
    }


def update_promotion_history(history, probed, today):
    seen = set()
    for m in probed:
        mid = m["model"]
        seen.add(mid)
        rec = history.setdefault(mid, _blank_promo_record(today))
        for k, v in _blank_promo_record(today).items():
            rec.setdefault(k, v)
        rec["last_seen"] = today
        rec["total_runs"] += 1
        if m.get("test_passed"):
            rec["last_pass"] = today
            if m.get("clean_pass"):
                rec["clean_runs"] += 1
                if rec.get("last_clean") != today:
                    rec["consecutive_clean_passes"] += 1
                    rec["last_clean"] = today
            else:
                rec["unclean_runs"] += 1
                rec["consecutive_clean_passes"] = 0
        else:
            rec["failed_runs"] += 1
            rec["consecutive_clean_passes"] = 0
            rec["last_fail"] = today
            rec["last_fail_note"] = (m.get("fail_note") or "")[:120]
    for mid, rec in history.items():
        if mid not in seen:
            if rec.get("consecutive_clean_passes"):
                rec["consecutive_clean_passes"] = 0
            rec["missed_runs"] = rec.get("missed_runs", 0) + 1


def prune_promotion_history(history, today):
    cutoff = date.fromisoformat(today).toordinal() - HISTORY_PRUNE_DAYS
    removed = []
    for mid in list(history):
        last = (history[mid].get("last_seen") or "")[:10]
        try:
            if date.fromisoformat(last).toordinal() < cutoff:
                removed.append(mid)
                del history[mid]
        except ValueError:
            removed.append(mid)
            del history[mid]
    return removed


def show_promotion_history(history):
    if not history:
        print("Promotion history is empty (no runs recorded yet).")
        return
    print(f"\n{'=' * 104}")
    print("PROMOTION HISTORY  (consecutive_clean_passes is what the gate reads)")
    print(f"{'=' * 104}")
    print(f"  {'model':<42} {'streak':>6} {'runs':>5} {'clean':>6} {'uncln':>6} "
          f"{'fail':>5} {'miss':>5} {'prom':>5}  last_clean")
    print(f"  {'-' * 100}")
    for mid in sorted(history, key=lambda k: (-history[k].get("consecutive_clean_passes", 0), k)):
        r = history[mid]
        print(f"  {mid:<42} {r.get('consecutive_clean_passes', 0):>6} "
              f"{r.get('total_runs', 0):>5} {r.get('clean_runs', 0):>6} "
              f"{r.get('unclean_runs', 0):>6} {r.get('failed_runs', 0):>5} "
              f"{r.get('missed_runs', 0):>5} {r.get('promotion_count', 0):>5}  "
              f"{r.get('last_clean') or '-'}")
    print(f"{'=' * 104}\n")


def promo_streak(history, model_id):
    return int(history.get(model_id, {}).get("consecutive_clean_passes", 0))


def choose_default(passed, probed_all, history, current_default, margin, hysteresis_days, emergency=True):
    if not passed:
        return None, "no models passed probing -- leaving model.default unchanged"

    by_date = sorted(passed, key=lambda m: (_release_date(m) or "1970-01-01",
                                          promo_streak(history, _model_id(m)), _model_id(m)),
                      reverse=True)

    probed_ids = {_model_id(m) for m in probed_all}
    passed_ids = {_model_id(m) for m in passed}
    incumbent_in_scope = current_default in probed_ids
    incumbent_passed = current_default in passed_ids
    incumbent_dead = incumbent_in_scope and not incumbent_passed
    incumbent_streak = promo_streak(history, current_default)
    incumbent_healthy = incumbent_in_scope and incumbent_passed and incumbent_streak >= margin

    eligible = [m for m in by_date if promo_streak(history, _model_id(m)) >= margin]

    if incumbent_in_scope:
        inc_state = "healthy" if incumbent_healthy else ("DEAD (failed probing)" if incumbent_dead
                                                         else f"in scope, streak {incumbent_streak}")
    else:
        inc_state = "out of scope (not a free candidate)"
    print(f"\n{'─' * 80}")
    print(f"PROMOTION GATE  (margin={margin} clean pass{'es' if margin != 1 else ''}, "
          f"hysteresis={hysteresis_days}d)")
    print(f"{'─' * 80}")
    print(f"  incumbent: {current_default or '(none)'}  [{inc_state}]")
    print(f"  {'model':<42} {'streak':>6}  eligible")
    for m in by_date:
        s = promo_streak(history, _model_id(m))
        mark = "yes" if s >= margin else f"no (needs {margin - s} more)"
        star = " <- incumbent" if _model_id(m) == current_default else ""
        print(f"  {_model_id(m):<42} {s:>6}  {mark}{star}")
    print(f"{'─' * 80}")

    # first-run bootstrap: no incumbent at all
    if not current_default:
        if eligible:
            w = eligible[0]
            return w, (f"first run: promoting best eligible '{_model_id(w)}' "
                       f"(streak {promo_streak(history, _model_id(w))})")
        if passed:
            w = by_date[0]
            return w, (f"first run: promoting best available '{_model_id(w)}' (bootstrap)")

    # Case 1: incumbent genuinely dead
    if incumbent_dead:
        if eligible:
            w = eligible[0]
            return w, (f"incumbent '{current_default}' FAILED probing this run; "
                       f"promoting best eligible '{_model_id(w)}' "
                       f"(streak {promo_streak(history, _model_id(w))})")
        if emergency:
            w = by_date[0]
            return w, (f"EMERGENCY: incumbent '{current_default}' failed probing and no model "
                       f"has {margin} clean passes; promoting best available "
                       f"'{_model_id(w)}' (streak {promo_streak(history, _model_id(w))})")
        return None, (f"incumbent '{current_default}' failed and nothing reached {margin} clean "
                      "passes -- leaving model.default unchanged")

    # Case 2: nothing earned the slot
    if not eligible:
        return None, (f"no model has {margin} clean passes yet -- leaving model.default "
                      f"'{current_default}' unchanged")

    # Case 3: qualified challenger
    challenger = next((m for m in eligible if _model_id(m) != current_default), None)
    if challenger is None:
        return None, (f"'{current_default}' is the newest qualified model -- unchanged")

    incumbent_entry = next((m for m in passed if _model_id(m) == current_default), None)
    if incumbent_entry:
        inc_date = _release_date(incumbent_entry) or "1970-01-01"
        ch_date = _release_date(challenger) or "1970-01-01"
        try:
            days_newer = (date.fromisoformat(ch_date) - date.fromisoformat(inc_date)).days
        except ValueError:
            days_newer = 0
        threshold = max(1, hysteresis_days)
        if days_newer < threshold:
            why = "not newer" if days_newer <= 0 else f"only {days_newer}d newer (needs {threshold}d)"
            return None, (f"keeping '{current_default}': challenger '{_model_id(challenger)}' is {why}")

    return challenger, (f"promoting '{_model_id(challenger)}' over '{current_default}': "
                        f"streak {promo_streak(history, _model_id(challenger))}"
                        + ("" if incumbent_in_scope else " (incumbent out of scope)"))


def cache_key(provider, model_id):
    return f"{provider}::{model_id}"


def _failure_class(e):
    if e.get("failure_class"):
        return e["failure_class"]
    if e.get("passed"):
        return "standard"
    note = (e.get("note") or "").lower()
    if " 404" in note or "http 404" in note or "410" in note or "not found" in note or "gone" in note:
        return "permanent"
    if ("429" in note or "500" in note or "502" in note or "503" in note
            or "timeout" in note or "empty content" in note or "timed out" in note):
        return "transient"
    return "standard"


def _ttl_days(fc):
    return {"permanent": 30, "transient": 1, "standard": CACHE_TTL_DAYS}.get(fc, CACHE_TTL_DAYS)


def cached_result(cache, provider, model_id, refresh):
    if refresh:
        return None
    e = cache.get("entries", {}).get(cache_key(provider, model_id))
    if not e:
        return None
    try:
        probed = datetime.fromisoformat(e["probed_at"])
        fc = _failure_class(e)
        ttl = timedelta(days=_ttl_days(fc))
        if (datetime.now(timezone.utc) - probed) >= ttl:
            return None
    except Exception:
        return None
    return bool(e.get("passed")), e.get("note", ""), bool(e.get("retried", False))


def put_cache(cache, provider, model_id, passed, note, retried):
    fc = "standard" if passed else None
    low = str(note).lower()
    if not passed:
        if "404" in low or "410" in low or "not found" in low or "gone" in low:
            fc = "permanent"
        elif ("429" in low or "500" in low or "502" in low or "503" in low
              or "timeout" in low or "empty content" in low or "timed out" in low):
            fc = "transient"
        else:
            fc = "standard"
    cache.setdefault("entries", {})[cache_key(provider, model_id)] = {
        "passed": bool(passed),
        "note": str(note),
        "retried": bool(retried),
        "failure_class": fc,
        "probed_at": datetime.now(timezone.utc).isoformat(),
    }


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
BANNED_PATH = HERMES_HOME / "cache" / "banned_models.json"
BOOSTED_PATH = HERMES_HOME / "cache" / "boosted_models.json"
PROMOTION_HISTORY_PATH = HERMES_HOME / "cache" / "promotion_history.json"
PROMOTION_MARGIN = 2
PROVIDER_COOLDOWN_SECONDS = float(os.environ.get("EASY_HERMES_PROVIDER_COOLDOWN", "5"))
HYSTERESIS_DAYS = 7
HISTORY_PRUNE_DAYS = 30


def load_banned():
    try:
        import atomic_io
        with atomic_io.file_lock(str(BANNED_PATH) + ".lock"):
            if BANNED_PATH.exists():
                data = json.loads(BANNED_PATH.read_text(encoding="utf-8"))
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
        with atomic_io.file_lock(str(BANNED_PATH) + ".lock"):
            atomic_io.atomic_write_json(BANNED_PATH, sorted(banned), indent=2)
    except Exception as e:
        print(f"  (banned list write failed: {e})", file=sys.stderr)


def load_boosted():
    try:
        if BOOSTED_PATH.exists():
            data = json.loads(BOOSTED_PATH.read_text(encoding="utf-8"))
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
        with atomic_io.file_lock(str(BOOSTED_PATH) + ".lock"):
            atomic_io.atomic_write_json(BOOSTED_PATH, sorted(boosted), indent=2)
    except Exception as e:
        print(f"  (boosted list write failed: {e})", file=sys.stderr)


def is_banned(mid, banned):
    return str(mid) in banned


def extract_release_date(m):
    if not isinstance(m, dict):
        return None
    for key in ("_release_date", "release_date", "created", "modified_at", "updated_at", "updated"):
        v = m.get(key)
        if v is None:
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            ts = float(v)
            if ts > 1e12:
                ts = ts / 1000.0
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            except (ValueError, OSError, OverflowError):
                continue
        s = str(v).strip()
        if not s:
            continue
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.date().isoformat()
        except ValueError:
            pass
        try:
            ts = float(s)
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        except ValueError:
            continue
    # Approximate via release_dates (OpenRouter/HF/hardcoded) if available.
    mid = m.get("id") or m.get("model") or m.get("name")
    if mid:
        try:
            import release_dates as rd
            res = rd.get_release_date(str(mid))
            if isinstance(res, tuple):
                return res[0]
            if isinstance(res, str):
                return res
        except Exception:
            pass
    # Last-resort approximation from model id / provider.
    mid_l = str(mid or "").lower()
    approx = {
        "gemini-3.6": "2026-07-01",
        "gemini-3.5": "2026-06-01",
        "gemini-3.1": "2026-03-01",
        "gemini-3": "2026-02-01",
        "gemini-2.5": "2025-06-01",
        "gemini-2.0": "2024-12-01",
        "gemini-1.5": "2024-05-01",
        "gemma-4": "2026-03-01",
        "gemma-3": "2025-03-01",
        "gemma-2": "2024-06-01",
    }
    for key, val in approx.items():
        if key in mid_l:
            return val
    # If a 4-digit year is embedded in the id, use Jan 1 of that year.
    import re
    ym = re.search(r"(20\d{2})", mid_l)
    if ym:
        return f"{ym.group(1)}-01-01"
    return None


def extract_capabilities(m):
    """Best-effort capability set from a model record. No network."""
    if not isinstance(m, dict):
        return set()
    caps = set()
    # Direct fields
    for key in ("capabilities", "tags", "supported_parameters", "parameters"):
        v = m.get(key)
        if isinstance(v, (list, tuple, set)):
            for item in v:
                caps.add(str(item).lower().strip())
        elif isinstance(v, str):
            for part in v.replace(",", " ").split():
                caps.add(part.lower().strip())
        elif isinstance(v, dict):
            for k in v:
                caps.add(str(k).lower().strip())
    # Architecture
    arch = m.get("architecture")
    if isinstance(arch, dict):
        mod = arch.get("modality")
        if isinstance(mod, str):
            for part in mod.replace(",", " ").replace("+", " ").replace("/", " ").split():
                caps.add(part.lower().strip())
        for k, v in arch.items():
            if isinstance(v, bool) and v:
                caps.add(str(k).lower().strip())
    # OpenRouter supported_parameters is a list of strings already handled above.
    # Normalize common aliases
    alias = {
        "tool_calling": "tools", "function_calling": "tools", "functions": "tools",
        "vision": "vision", "image": "vision", "multimodal": "vision",
        "code": "coding", "coder": "coding",
        "reasoning": "reasoning", "thinking": "reasoning",
        "json": "json", "structured_outputs": "json",
    }
    out = set()
    for c in caps:
        out.add(alias.get(c, c))
    # Drop empties
    out.discard("")
    return out


_OR_CONTEXT_MAP = None
_OR_CTX_LOCK = threading.Lock()


def _get_or_context_map():
    global _OR_CONTEXT_MAP
    with _OR_CTX_LOCK:
        if _OR_CONTEXT_MAP is not None:
            return _OR_CONTEXT_MAP
        _OR_CONTEXT_MAP = {}
        try:
            import urllib.request, json, os
            key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")
            url = "https://openrouter.ai/api/v1/models"
            headers = {"Accept": "application/json", "User-Agent": "easy-hermes/select-models"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            for m in (data.get("data") or []):
                mid = m.get("id")
                ctx = m.get("context_length")
                if mid and ctx is not None:
                    _OR_CONTEXT_MAP[str(mid)] = ctx
                    name = str(mid).split("/")[-1].lower()
                    _OR_CONTEXT_MAP.setdefault(name, ctx)
        except Exception:
            pass
        return _OR_CONTEXT_MAP


def _normalize_key(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _hf_context(mid):
    import os
    if os.environ.get("EASY_HERMES_HF_CONTEXT", "").lower() not in ("1", "true", "yes"):
        return None
    if not mid:
        return None
    cands = {str(mid)}
    # title-case each path segment (HF repos are usually CamelCase model names)
    cands.add("/".join(p.capitalize() if i > 0 else p for i, p in enumerate(str(mid).split("/"))))
    # also try lowercased whole
    cands.add(str(mid).lower())
    for repo in cands:
        for branch in ("main", "master"):
            url = f"https://huggingface.co/{repo}/raw/{branch}/config.json"
            try:
                with urllib.request.urlopen(url, timeout=8) as r:
                    d = json.loads(r.read())
                for key in ("max_position_embeddings", "max_seq_len", "n_ctx",
                            "context_length", "max_sequence_length", "seq_length"):
                    v = d.get(key)
                    if isinstance(v, (int, float)) and v > 0:
                        return int(v)
            except Exception:
                continue
    return None


def approximate_context(m):
    if not isinstance(m, dict):
        return None
    ctx = m.get("context_window")
    if isinstance(ctx, (int, float)):
        return ctx
    mid = m.get("id") or m.get("model") or m.get("name")
    if not mid:
        return None
    # Prefer HuggingFace (authoritative model config) when the repo is public.
    hf = _hf_context(mid)
    if hf is not None:
        return hf
    # Fall back to OpenRouter (broader coverage, fuzzy match).
    ormap = _get_or_context_map()
    if not isinstance(ormap, dict) or not ormap:
        return None
    target = _normalize_key(mid)
    for k, v in ormap.items():
        if _normalize_key(k) == target:
            return v
    best = None
    best_score = 0.0
    import difflib
    for k, v in ormap.items():
        nk = _normalize_key(k)
        if target in nk or nk in target:
            score = min(len(target), len(nk)) / max(len(target), len(nk), 1)
            if score > best_score:
                best_score = score
                best = v
            continue
        r = difflib.SequenceMatcher(None, target, nk).ratio()
        if r > best_score:
            best_score = r
            best = v
    if best is not None and best_score >= 0.82:
        return best
    return None


def _extract_elapsed_ms(note):
    if not note:
        return None
    matches = re.findall(r"(\d+)\s*ms", str(note))
    if not matches:
        return None
    try:
        return int(matches[-1])
    except (TypeError, ValueError):
        return None


def reasoning_score_from_note(note):
    if not note:
        return 0.0
    m = re.search(r"reasoning=(\d+)ch", str(note))
    if m:
        return 1.0 if int(m.group(1)) > 0 else 0.0
    return 0.0


def get_reasoning_efforts(model_id, base_url):
    import os
    cache_path = HERMES_HOME / "cache" / "reasoning_caps.json"
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    # map base url to cache key
    keys = []
    if base_url:
        keys.append(base_url.rstrip("/") + "/models")
        keys.append(base_url.rstrip("/"))
    keys.append("https://openrouter.ai/api/v1/models")
    keys.append("https://inference-api.nousresearch.com/v1/models")
    for k in keys:
        bucket = data.get(k, {})
        caps = bucket.get("caps", {}) if isinstance(bucket, dict) else {}
        info = caps.get(model_id)
        if isinstance(info, dict):
            return info.get("supported_efforts")
    return None


def probe_reasoning_levels(model_id, cfg, api_key, timeout=None):
    base = cfg.get("probe_base_url") or ""
    t = timeout if (timeout is not None and timeout > 0) else (cfg.get("probe_timeout") or 15)
    if not t or t <= 0:
        t = 15
    extra = cfg.get("probe_headers")
    # Phase 1: initial polling WITHOUT reasoning (decides pass/fail).
    print("    base probe (no reasoning)...", end=" ", flush=True)
    passed0, note0, retried0 = _openai_probe(
        model_id, base, api_key, timeout=t, extra_headers=extra,
        reasoning=None, max_tokens=PROBE_MAX_TOKENS)
    retried_overall = bool(retried0)
    print(f"{'PASS' if passed0 else 'fail'} ({note0})")
    if not passed0:
        return False, note0, retried_overall

    # Phase 2: poll each advertised reasoning level (does not un-pass the model).
    supported = get_reasoning_efforts(model_id, base)
    if supported:
        efforts = [e for e in REASONING_PRIORITY if e in supported and e != "none"]
    else:
        efforts = [e for e in REASONING_PRIORITY if e != "none"]
    if not efforts:
        efforts = ["high"]

    chosen_note = note0 + " (no reasoning effort worked)"
    for effort in efforts:
        print(f"    probing reasoning={effort}...", end=" ", flush=True)
        passed, note, retried = _openai_probe(
            model_id, base, api_key, timeout=t, extra_headers=extra,
            reasoning=effort, max_tokens=PROBE_MAX_TOKENS)
        if retried:
            retried_overall = True
        print(f"{'PASS' if passed else 'fail'} ({note})")
        if passed:
            chosen_note = f"{note} (effort={effort})"
            # store chosen effort by appending to note only; pass already secured
            break
        if passed is False and note.startswith("budget exhausted"):
            print(f"    retrying with max_tokens={PROBE_RETRY_MAX_TOKENS}...", end=" ", flush=True)
            passed2, note2, retried2 = _openai_probe(
                model_id, base, api_key, timeout=t, extra_headers=extra,
                reasoning=effort, max_tokens=PROBE_RETRY_MAX_TOKENS)
            if retried2:
                retried_overall = True
            print(f"{'PASS' if passed2 else 'fail'} ({note2})")
            if passed2:
                chosen_note = f"{note2} (effort={effort}, budget retry)"
                break
    return True, chosen_note, retried_overall


def load_ranking_config():
    cfg, _ = load_config()
    mr = cfg.get("model_ranking") if isinstance(cfg, dict) else None
    if not isinstance(mr, dict):
        mr = {}
    weights = mr.get("weights")
    if not isinstance(weights, dict):
        weights = dict(DEFAULT_RANKING_WEIGHTS)
    else:
        # fill in any newly added weight keys with defaults
        merged_w = dict(DEFAULT_RANKING_WEIGHTS)
        merged_w.update(weights)
        weights = merged_w
    # normalize to sum 1
    try:
        total = sum(float(v) for v in weights.values())
    except (TypeError, ValueError):
        total = 0.0
    if total > 0:
        weights = {k: float(v) / total for k, v in weights.items()}
    else:
        weights = dict(DEFAULT_RANKING_WEIGHTS)
    pp = mr.get("provider_priority")
    if not isinstance(pp, dict):
        pp = dict(DEFAULT_PROVIDER_PRIORITY)
    else:
        # fill defaults for missing providers
        merged = dict(DEFAULT_PROVIDER_PRIORITY)
        merged.update(pp)
        pp = merged
    try:
        missing = float(mr.get("missing_release_date", 0.0))
    except (TypeError, ValueError):
        missing = 0.0
    return {"weights": weights, "provider_priority": pp, "missing_release_date": missing}


def _to_ts(rd):
    if rd is None:
        return None
    if isinstance(rd, bool):
        return None
    if isinstance(rd, (int, float)):
        ts = float(rd)
        if ts > 1e12:
            ts = ts / 1000.0
        return ts
    s = str(rd).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        pass
    try:
        ts = float(s)
        if ts > 1e12:
            ts = ts / 1000.0
        return ts
    except ValueError:
        return None


def rank_models(models, ranking=None):
    if ranking is None:
        ranking = load_ranking_config()
    if not models:
        return models
    weights = ranking["weights"]
    pp_map = ranking["provider_priority"]
    missing = ranking["missing_release_date"]
    try:
        boosted = load_boosted()
    except Exception:
        boosted = set()

    # context min/max
    cws = [m.get("context_window") for m in models if isinstance(m.get("context_window"), (int, float))]
    cw_min = min(cws) if cws else 0
    cw_max = max(cws) if cws else 0
    # release ts min/max
    ts_list = [_to_ts(m.get("_release_date")) for m in models]
    ts_valid = [t for t in ts_list if t is not None]
    ts_min = min(ts_valid) if ts_valid else 0
    ts_max = max(ts_valid) if ts_valid else 0
    # response time min/max (lower is better)
    rt_list = [m.get("_response_time_ms") for m in models]
    rt_valid = [r for r in rt_list if isinstance(r, (int, float))]
    rt_min = min(rt_valid) if rt_valid else 0
    rt_max = max(rt_valid) if rt_valid else 0

    for idx, m in enumerate(models):
        # context norm
        cw = m.get("context_window")
        if isinstance(cw, (int, float)) and cw_max > cw_min:
            cw_norm = (cw - cw_min) / (cw_max - cw_min)
        elif isinstance(cw, (int, float)) and cws:
            cw_norm = 1.0
        else:
            cw_norm = 0.0
        # release norm
        ts = ts_list[idx]
        if ts is not None:
            if ts_max > ts_min:
                rel_norm = (ts - ts_min) / (ts_max - ts_min)
            else:
                rel_norm = 1.0
        else:
            rel_norm = missing
        # provider priority norm
        prov = m.get("_provider")
        raw_pp = pp_map.get(prov, 1.0)
        try:
            pp_norm = float(raw_pp)
        except (TypeError, ValueError):
            pp_norm = 1.0
        pp_norm = max(0.0, min(1.0, pp_norm))

        is_boosted = str(m.get("id")) in boosted
        boost_norm = 1.0 if is_boosted else 0.0
        # response time norm (lower is better)
        rt = rt_list[idx]
        if isinstance(rt, (int, float)) and rt_max > rt_min:
            rt_norm = (rt_max - rt) / (rt_max - rt_min)
        elif isinstance(rt, (int, float)) and rt_valid:
            rt_norm = 1.0
        else:
            rt_norm = 0.0
        reasoning_norm = float(m.get("_reasoning", 0.0) or 0.0)
        score = (weights.get("release_date", 0.0) * rel_norm
                 + weights.get("context_window", 0.0) * cw_norm
                 + weights.get("provider_priority", 0.0) * pp_norm
                 + weights.get("reasoning", 0.0) * reasoning_norm
                 + weights.get("response_time", 0.0) * rt_norm
                 + weights.get("boost", 1.0) * boost_norm)
        if is_boosted:
            score += 1.0
        m["_score"] = round(score, 4)
        m["_score_parts"] = {
            "release": round(rel_norm, 3),
            "context": round(cw_norm, 3),
            "provider": round(pp_norm, 3),
            "reasoning": round(reasoning_norm, 3),
            "response_time": round(rt_norm, 3),
        }

    return sorted(models, key=lambda m: (-(m.get("_score") or 0), -(m.get("context_window") or 0), str(m.get("id"))))


def fetch_provider(slug, cfg):
    modname = cfg["module"]
    try:
        import importlib
        mod = importlib.import_module(modname)
    except Exception as e:
        return [], f"import failed: {e}"
    fn = None
    preferred = cfg.get("fetch_fn")
    names = ([preferred] if preferred else []) + ["poll", "fetch_models", "fetch", "fetch_free_models", "get_models"]
    for name in names:
        if not name:
            continue
        fn = getattr(mod, name, None)
        if callable(fn):
            break
    if fn is None:
        return [], "no fetch function"
    key, _ = providers.get_api_key(cfg)
    try:
        try:
            raws = fn(api_key=key)
        except TypeError:
            raws = fn()
    except Exception as e:
        return [], str(e)
    if not isinstance(raws, list):
        return [], "fetch did not return a list"
    out = []
    for r in raws:
        if not isinstance(r, dict):
            continue
        mid = r.get("id") if "id" in r else (r.get("id") or r.get("name") or r.get("model") or r.get("slug"))
        if mid is None:
            continue
        if "id" not in r:
            mid = r.get("id") or r.get("name") or r.get("model") or r.get("slug")
            out.append({
                "id": str(mid) if mid is not None else None,
                "display_name": r.get("display_name") or r.get("name") or mid,
                "context_window": r.get("context_window") or r.get("context_length"),
                "rate_limits": r.get("rate_limits"),
                "notes": r.get("notes", ""),
            })
        else:
            out.append(r)
    return out, None


import urllib.request
import urllib.error


def _openai_probe(model_id, base_url, api_key, timeout=15, extra_headers=None, reasoning=None, max_tokens=256):
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url = url + "/v1"
    url = url + "/chat/completions"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "If I have 3 apples and eat 1, how many are left? Answer with just the number."}],
        "max_tokens": max_tokens,
    }
    if reasoning is not None:
        payload["reasoning_effort"] = reasoning
    headers = {"Content-Type": "application/json", "User-Agent": "easy-hermes/select-models"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    retried = False
    last = "no attempt"
    for delay in (0, 2, 5):
        if delay:
            retried = True
            time.sleep(delay)
        req = urllib.request.Request(url, data=__import__("json").dumps(payload).encode(), headers=headers)
        try:
            start = time.perf_counter()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            content = (msg.get("content") or "").strip()
            reasoning_text = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
            if content:
                return True, f"ok: {content[:40]} (reasoning={len(reasoning_text)}ch, {elapsed_ms}ms)", retried
            fr = choice.get("finish_reason") if isinstance(choice, dict) else None
            if reasoning_text and fr == "length":
                return False, f"budget exhausted thinking ({len(reasoning_text)}ch, {elapsed_ms}ms)", retried
            note = f"empty content (finish_reason={fr}, {elapsed_ms}ms)" if fr else f"empty content ({elapsed_ms}ms)"
            return False, note, retried
        except urllib.error.HTTPError as e:
            code = e.code
            body = ""
            try:
                body = e.read().decode(errors="replace")
            except Exception:
                pass
            low = body.lower()
            if code == 429 and ("per day" in low or "daily" in low or "limit: 0" in low.replace(" ", "")):
                return False, f"HTTP 429: daily quota exceeded (not retryable)", retried
            if code == 429 or code >= 500:
                last = f"HTTP {code} (retrying)"
                continue
            return False, f"HTTP {code}: {body[:80]}", retried
        except Exception as e:
            return False, f"{type(e).__name__}: {e}", retried
    return False, last, retried


def _gemini_probe(model_id, api_key, base_url, timeout=15):
    import re as _re
    if not isinstance(model_id, str) or not _re.fullmatch(r"[A-Za-z0-9._\-/]+", model_id):
        return False, "invalid model id", False
    mid = model_id
    if not mid.startswith("models/"):
        mid = "models/" + mid
    from urllib.parse import quote
    mid_enc = "/".join(quote(seg, safe="") for seg in mid.split("/"))
    url = f"{base_url.rstrip('/')}/{mid_enc}:generateContent"
    payload = {"contents": [{"parts": [{"text": "If I have 3 apples and eat 1, how many are left? Answer with just the number."}]}],
               "generationConfig": {"maxOutputTokens": 256}}
    retried = False
    last = "no attempt"
    for delay in (0, 5, 15):
        if delay:
            retried = True
            time.sleep(delay)
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json",
                                             "x-goog-api-key": str(api_key),
                                             "User-Agent": "easy-hermes/select-models"})
        try:
            start = time.perf_counter()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            cands = data.get("candidates") or []
            if cands:
                parts = (cands[0].get("content", {}) or {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
                if text:
                    return True, f"ok: {text[:40]} ({elapsed_ms}ms)", retried
                fr = cands[0].get("finishReason")
                if fr:
                    last = f"empty content (finishReason={fr}, {elapsed_ms}ms)"
                else:
                    last = f"empty content ({elapsed_ms}ms)"
                return False, last, retried
            pf = data.get("promptFeedback") or {}
            br = pf.get("blockReason")
            if br:
                return False, f"blocked: {br}", retried
            return False, "empty content", retried
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace")
            except Exception:
                pass
            code = e.code
            low = body.lower()
            if code == 429 and ("per day" in low or "daily" in low or "limit: 0" in low.replace(" ", "")):
                return False, "HTTP 429: daily quota exceeded (not retryable)", retried
            if code == 429 or code >= 500:
                last = f"HTTP {code}: {body[:80]}"
                continue
            return False, f"HTTP {code}: {body[:80]}", retried
        except Exception as e:
            return False, f"{type(e).__name__}: {e}", retried
    return False, last, retried


def _ollama_local_probe(model_id, base_url, timeout=60):
    try:
        import poll_ollama_local as _pol
        ok, note, retried = _pol.probe_model(model_id, _pol.openai_base(base_url), fast=False)
        return bool(ok), str(note), bool(retried)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", False


def probe(slug, cfg, model_id, api_key, timeout=None, reasoning=None):
    kind = cfg.get("probe_kind")
    if kind == "openai":
        base = cfg.get("probe_base_url") or ""
        t = timeout if (timeout is not None and timeout > 0) else (cfg.get("probe_timeout") or 15)
        if not t or t <= 0:
            t = 15
        if reasoning:
            return probe_reasoning_levels(model_id, cfg, api_key, timeout=t)
        return _openai_probe(model_id, base, api_key, timeout=t, extra_headers=cfg.get("probe_headers"))
    if kind == "gemini":
        base = cfg.get("probe_base_url") or "https://generativelanguage.googleapis.com/v1beta"
        t = timeout if (timeout is not None and timeout > 0) else (cfg.get("probe_timeout") or 15)
        if not t or t <= 0:
            t = 15
        return _gemini_probe(model_id, api_key, base, timeout=t)
    if kind == "ollama-local":
        try:
            import poll_ollama_local as _pol
            bu = _pol.resolve_base_url()
        except Exception:
            bu = "http://localhost:11434"
        return _ollama_local_probe(model_id, bu)
    return False, "unknown probe kind", False


def load_config():
    import yaml
    if not CONFIG_PATH.exists():
        return {}, yaml
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}), yaml
    except Exception:
        return {}, yaml


def write_config(passing, yaml_mod, chosen=None, current_default=None, promote=True):
    if not passing and not chosen and not current_default:
        return
    import atomic_io
    lock_path = str(CONFIG_PATH) + ".lock"
    with atomic_io.file_lock(lock_path):
        try:
            if CONFIG_PATH.exists():
                bak = CONFIG_PATH.with_suffix(".yaml.bak")
                atomic_io.atomic_write_text(bak, CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        cfg, _ = load_config()
        if not isinstance(cfg, dict):
            cfg = {}
        if "model_ranking" not in cfg:
            cfg["model_ranking"] = {
                "weights": dict(DEFAULT_RANKING_WEIGHTS),
                "provider_priority": dict(DEFAULT_PROVIDER_PRIORITY),
                "missing_release_date": 0.0,
            }
        # effective default: promoted chosen, or kept incumbent, or first passing
        effective_default_id = None
        default_model = None
        if promote and chosen is not None:
            default_model = chosen
            effective_default_id = _model_id(chosen)
        elif promote and current_default:
            effective_default_id = current_default
        elif promote and passing:
            default_model = passing[0]
            effective_default_id = _model_id(passing[0])

        if default_model is not None:
            best = default_model
            best_provider = best.get("_provider", "custom")
            hermes_provider = best_provider if best_provider in ("nous", "openrouter", "custom", "ollama-cloud", "nvidia", "gemini", "novita") else "custom"
            mcfg = cfg.setdefault("model", {})
            if not isinstance(mcfg, dict):
                mcfg = {"default": mcfg} if isinstance(mcfg, str) else {}
                cfg["model"] = mcfg
            mcfg["default"] = best["id"]
            mcfg["provider"] = hermes_provider
            if hermes_provider == "custom" and best.get("_base_url"):
                mcfg["base_url"] = best["_base_url"]
            else:
                mcfg.pop("base_url", None)

        # fallback chain: all passing except the effective default
        fb = []
        for m in (passing or []):
            if effective_default_id and _model_id(m) == effective_default_id:
                continue
            prov = m.get("_provider", "custom")
            hp = prov if prov in ("nous", "openrouter", "custom", "ollama-cloud", "nvidia", "gemini", "novita") else "custom"
            entry = {"provider": hp, "model": m["id"]}
            if hp == "custom" and m.get("_base_url"):
                entry["base_url"] = m["_base_url"]
            fb.append(entry)

        existing = cfg.get("fallback_providers")
        if not isinstance(existing, list):
            existing = []
        existing_keys = {(str(e.get("provider")), str(e.get("model"))) for e in existing if isinstance(e, dict)}
        new_fb = [e for e in fb if (str(e.get("provider")), str(e.get("model"))) not in existing_keys]
        cfg["fallback_providers"] = list(existing) + new_fb

        try:
            import atomic_io
            import io
            buf = io.StringIO()
            yaml_mod.safe_dump(cfg, buf, default_flow_style=False, sort_keys=False, allow_unicode=True)
            atomic_io.atomic_write_text(CONFIG_PATH, buf.getvalue(), encoding="utf-8")
            print(f"Updated {CONFIG_PATH}")
            if effective_default_id:
                print(f"  model.default -> {effective_default_id}")
        except Exception as e:
            print(f"ERROR writing config: {e}", file=sys.stderr)


def _process_provider(slug, cfg, *, ranking, reasoning_mode, top_n, args, banned, cache, cache_lock, print_lock):
    if slug == "ollama-local":
        try:
            import poll_ollama_local as _pol
            cfg = dict(cfg)
            cfg["probe_base_url"] = _pol.openai_base(_pol.resolve_base_url())
        except Exception:
            pass
    # Cap OpenRouter probing to avoid burning free-tier daily quota unless overridden.
    if slug == "openrouter":
        cap = getattr(args, "openrouter_top", None)
        if cap is None:
            cap = 3
        if cap and cap > 0 and (top_n is None or top_n > cap):
            top_n = cap
    passing_list = []
    probed_list = []
    _buf = []

    def _print(*a, **k):
        sep = k.get("sep", " ")
        end = k.get("end", "\n")
        _buf.append(sep.join(str(x) for x in a) + end)

    def _flush():
        if not _buf:
            return
        with print_lock:
            sys.stdout.write("".join(_buf))
            sys.stdout.flush()
        _buf.clear()

    _print(f"\n=== {cfg['display']} ({slug}) ===")
    ok, vnote = providers.validate_key(cfg)
    if not ok:
        _print(f"  key validation failed: {vnote}")
        _flush()
        return passing_list, probed_list, {"provider": slug, "fetched": 0, "passed": 0, "error": vnote}
    if vnote and vnote != "no key required":
        _print(f"  {vnote}")
    models, err = fetch_provider(slug, cfg)
    if err:
        _print(f"  fetch error: {err}")
        _flush()
        return passing_list, probed_list, {"provider": slug, "fetched": 0, "passed": 0, "error": err}

    models = [m for m in (models or []) if not is_banned(m.get("id") if isinstance(m, dict) else m, banned)]
    for m in models:
        if isinstance(m, dict) and "_release_date" not in m:
            m["_release_date"] = extract_release_date(m)
        if isinstance(m, dict):
            m["_capabilities"] = extract_capabilities(m)
        if isinstance(m, dict) and m.get("context_window") is None:
            approx_ctx = approximate_context(m)
            if approx_ctx is not None:
                m["context_window"] = approx_ctx

    key, _ = providers.get_api_key(cfg)

    if not cfg.get("probe", True):
        cands = rank_models(models, ranking)
        if top_n is not None:
            cands = cands[:top_n]
        _print(f"  fetched {len(models)} candidate(s); listing-only, including top {len(cands)} (unverified)")
        passed_here = 0
        for m in cands:
            m["_provider"] = slug
            m["_base_url"] = cfg.get("probe_base_url")
            m["_probe_ok"] = None
            m["_probe_note"] = "unverified (listing only, no probe)"
            m["_reasoning"] = 0.0
            m["_response_time_ms"] = None
            passing_list.append(m)
            passed_here += 1
            probed_list.append({"model": m["id"], "test_passed": True, "clean_pass": False,
                               "fail_note": "listing-only (no probe)", "release_date": m.get("_release_date")})
        _flush()
        return passing_list, probed_list, {"provider": slug, "fetched": len(models), "probed": 0,
                                           "passed": passed_here}

    _print(f"  fetched {len(models)} candidate(s)")
    cf = cfg.get("chat_filter")
    if cf:
        try:
            mod_name, fn_name = cf.split(":", 1)
            import importlib
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name)
            cands = [m for m in models if fn(m.get("id") or "")]
            if top_n is not None:
                cands = cands[:top_n]
            _print(f"  {slug}: probing {len(cands)} chat-like models (account-gated; agreements matter)")
        except Exception:
            cands = rank_models(models, ranking)
            if top_n is not None:
                cands = cands[:top_n]
    else:
        cands = rank_models(models, ranking)
        if top_n is not None:
            cands = cands[:top_n]
    _print(f"  probing top {len(cands)}")

    can_probe = not (cfg.get("probe_kind") == "openai" and not key)
    passed_here = 0
    for m in cands:
        mid = m.get("id")
        if not can_probe:
            if passed_here >= 1:
                _print(f"    skip {mid}  (no API key to probe)")
                continue
            m["_provider"] = slug
            m["_base_url"] = cfg.get("probe_base_url")
            m["_probe_ok"] = None
            m["_probe_note"] = "unverified (no API key to probe)"
            m["_reasoning"] = 0.0
            m["_response_time_ms"] = None
            passing_list.append(m)
            passed_here += 1
            _print(f"    UNVERIFIED {mid}  (no API key to probe)")
            probed_list.append({"model": mid, "test_passed": True, "clean_pass": False,
                               "fail_note": "no API key to probe", "release_date": m.get("_release_date")})
            continue
        with cache_lock:
            cr = cached_result(cache, slug, mid, args.refresh)
        if cr is not None:
            cached_passed, cached_note, cached_retried = cr
            ok2, note, retried = cached_passed, cached_note, cached_retried
            note = f"[cache] {note}"
        else:
            ok2, note, retried = probe(slug, cfg, mid, key,
                                       timeout=cfg.get("probe_timeout"), reasoning=reasoning_mode)
            with cache_lock:
                put_cache(cache, slug, mid, ok2, note, retried)
        clean = bool(ok2) and not bool(retried)
        m["_provider"] = slug
        m["_base_url"] = cfg.get("probe_base_url")
        m["_probe_ok"] = clean
        m["_probe_note"] = note
        m["_reasoning"] = reasoning_score_from_note(note)
        m["_response_time_ms"] = _extract_elapsed_ms(note)
        if clean:
            passing_list.append(m)
            passed_here += 1
        _print(f"    {'PASS' if clean else 'fail'} {mid}  ({note})")
        probed_list.append({"model": mid, "test_passed": bool(ok2), "clean_pass": clean,
                           "fail_note": None if ok2 else note, "release_date": m.get("_release_date")})

    _flush()
    return passing_list, probed_list, {"provider": slug, "fetched": len(models), "probed": len(cands),
                                       "passed": passed_here}


def main():
    ap = argparse.ArgumentParser(description="Central free-model selector (Phase 2)")
    ap.add_argument("--dry-run", action="store_true", help="Preview only, write nothing")
    ap.add_argument("--refresh", action="store_true", help="Ignore probe cache")
    ap.add_argument("--prefer-local", action="store_true", help="Include local Ollama")
    ap.add_argument("--providers", help="Comma-separated provider slugs")
    ap.add_argument("--apply", action="store_true", help="Write config")
    ap.add_argument("--json", action="store_true", help="Emit JSON summary")
    ap.add_argument("--top", type=int, help="Override probe budget")
    ap.add_argument("--full", action="store_true", help="Probe every candidate (ignore top-N budget)")
    ap.add_argument("--set-weight", action="append", metavar="KEY=VALUE",
                    help="Set a ranking weight, e.g. release_date=0.5 (repeatable)")
    ap.add_argument("--set-priority", action="append", metavar="PROVIDER=VALUE",
                    help="Set provider priority, e.g. openrouter=1.0 (repeatable)")
    ap.add_argument("--set-missing-release", type=float, metavar="SCORE",
                    help="Score (0..1) used when a model has no release date")
    ap.add_argument("--show-ranking", action="store_true",
                    help="Print the current ranking matrix and exit")
    ap.add_argument("--model", action="append", metavar="MODEL_ID",
                    help="Retest a specific model id on the selected providers (repeatable)")
    ap.add_argument("--reasoning", action="store_true",
                    help="Probe with reasoning_effort=high and report reasoning chars")
    ap.add_argument("--ban", action="append", metavar="MODEL_ID",
                    help="Ban a model id (persisted; filtered from candidates)")
    ap.add_argument("--unban", action="append", metavar="MODEL_ID",
                    help="Unban a model id")
    ap.add_argument("--list-banned", action="store_true",
                    help="Print banned model ids and exit")
    ap.add_argument("--boost", action="append", metavar="MODEL_ID",
                    help="Boost a model id (sorts to top)")
    ap.add_argument("--unboost", action="append", metavar="MODEL_ID",
                    help="Remove a model from the boosted list")
    ap.add_argument("--list-boosted", action="store_true",
                    help="Print boosted model ids and exit")
    ap.add_argument("--set-default", metavar="MODEL_ID",
                    help="Force model.default to this id (bypasses promotion gate)")
    ap.add_argument("--promotion-margin", type=int, metavar="N",
                    help=f"Consecutive clean passes required to be eligible (default {PROMOTION_MARGIN})")
    ap.add_argument("--hysteresis-days", type=int, metavar="D",
                    help=f"Days newer a challenger must be to displace incumbent (default {HYSTERESIS_DAYS})")
    ap.add_argument("--no-emergency", action="store_true",
                    help="Disable emergency promotion when incumbent is dead")
    ap.add_argument("--show-history", action="store_true",
                    help="Print promotion history table and exit")
    ap.add_argument("--no-promote", action="store_true",
                    help="Only refresh the fallback chain; leave model.default unchanged")
    ap.add_argument("--hermes-home", metavar="PATH",
                    help="Hermes home directory (overrides HERMES_HOME env / default)")
    ap.add_argument("--provider-cooldown", type=float, metavar="SEC",
                    help=f"Seconds to wait between starting providers (default {PROVIDER_COOLDOWN_SECONDS})")
    ap.add_argument("--openrouter-top", type=int, metavar="N",
                    help="Cap OpenRouter probes to N (default 3; 0 = no cap)")
    ap.add_argument("--capability", metavar="CAP",
                    help="Keep only models whose _capabilities contains CAP")
    ap.add_argument("--today", metavar="YYYY-MM-DD",
                    help="Treat this run as happening on the given date (testing/backfill)")
    args = ap.parse_args()

    if getattr(args, "hermes_home", None):
        import os as _os
        _os.environ["HERMES_HOME"] = args.hermes_home
        global HERMES_HOME, CONFIG_PATH, CACHE_PATH, BANNED_PATH, BOOSTED_PATH, PROMOTION_HISTORY_PATH
        HERMES_HOME = Path(args.hermes_home)
        CONFIG_PATH = HERMES_HOME / "config.yaml"
        CACHE_PATH = HERMES_HOME / "cache" / "free_model_probe_cache.json"
        BANNED_PATH = HERMES_HOME / "cache" / "banned_models.json"
        BOOSTED_PATH = HERMES_HOME / "cache" / "boosted_models.json"
        PROMOTION_HISTORY_PATH = HERMES_HOME / "cache" / "promotion_history.json"

    reasoning_mode = "high" if getattr(args, "reasoning", False) else None

    # --- ban list management ---
    banned = load_banned()
    if args.list_banned:
        print("Banned models:")
        for m in sorted(banned):
            print(f"  {m}")
        if not banned:
            print("  (none)")
        sys.exit(0)
    if args.ban:
        for m in args.ban:
            banned.add(m)
        save_banned(banned)
        print(f"Banned: {', '.join(args.ban)}")
    if args.unban:
        for m in args.unban:
            banned.discard(m)
        save_banned(banned)
        print(f"Unbanned: {', '.join(args.unban)}")
    if args.ban or args.unban:
        pass

    # --- boosted list management ---
    boosted = load_boosted()
    if args.list_boosted:
        for m in sorted(boosted):
            print(m)
        if not boosted:
            print("(none)")
        sys.exit(0)
    if args.boost:
        for m in args.boost:
            boosted.add(m)
        save_boosted(boosted)
        print(f"Boosted: {', '.join(args.boost)}")
    if args.unboost:
        for m in args.unboost:
            boosted.discard(m)
        save_boosted(boosted)
        print(f"Unboosted: {', '.join(args.unboost)}")
    if args.boost or args.unboost:
        sys.exit(0)
        if not (args.show_ranking or args.set_weight or args.set_priority
                or args.set_missing_release is not None or args.model):
            if not (args.dry_run or args.apply or args.providers or args.full
                    or args.top or args.json or args.show_history):
                sys.exit(0)
    if args.show_history:
        show_promotion_history(load_promotion_history())
        sys.exit(0)

    # --- ranking matrix management (no probing) ---
    if args.show_ranking or args.set_weight or args.set_priority or args.set_missing_release is not None:
        cfg, yaml_mod = load_config()
        if not isinstance(cfg, dict):
            cfg = {}
        before_ids = chain_delta.chain_ids_from_config(cfg) if chain_delta is not None else []
        mr = cfg.setdefault("model_ranking", {})
        if not isinstance(mr, dict):
            mr = {}
            cfg["model_ranking"] = mr
        if not isinstance(mr.get("weights"), dict):
            mr["weights"] = dict(DEFAULT_RANKING_WEIGHTS)
        else:
            merged_w = dict(DEFAULT_RANKING_WEIGHTS)
            merged_w.update(mr["weights"])
            mr["weights"] = merged_w
        mr.setdefault("provider_priority", dict(DEFAULT_PROVIDER_PRIORITY))
        mr.setdefault("missing_release_date", 0.0)

        if args.set_weight:
            for item in args.set_weight:
                if "=" not in item:
                    print(f"ERROR: expected KEY=VALUE, got {item!r}", file=sys.stderr)
                    sys.exit(2)
                k, v = item.split("=", 1)
                try:
                    mr["weights"][k.strip()] = float(v)
                except ValueError:
                    print(f"ERROR: invalid number {v!r}", file=sys.stderr)
                    sys.exit(2)
        if args.set_priority:
            for item in args.set_priority:
                if "=" not in item:
                    print(f"ERROR: expected PROVIDER=VALUE, got {item!r}", file=sys.stderr)
                    sys.exit(2)
                k, v = item.split("=", 1)
                try:
                    mr["provider_priority"][k.strip()] = float(v)
                except ValueError:
                    print(f"ERROR: invalid number {v!r}", file=sys.stderr)
                    sys.exit(2)
        if args.set_missing_release is not None:
            mr["missing_release_date"] = float(args.set_missing_release)

        # normalize weights for display
        w = mr.get("weights", {})
        try:
            total = sum(float(x) for x in w.values())
        except (TypeError, ValueError):
            total = 0.0
        if total > 0:
            norm = {k: round(float(v) / total, 4) for k, v in w.items()}
        else:
            norm = dict(w)

        print("Ranking matrix:")
        print("  weights (raw):", w)
        print("  weights (normalized):", norm)
        print("  provider_priority:", mr.get("provider_priority", {}))
        print("  missing_release_date:", mr.get("missing_release_date"))

        if (args.set_weight or args.set_priority or args.set_missing_release is not None) and not args.dry_run:
            try:
                import atomic_io
                import io
                buf = io.StringIO()
                yaml_mod.safe_dump(cfg, buf, default_flow_style=False, sort_keys=False, allow_unicode=True)
                atomic_io.atomic_write_text(CONFIG_PATH, buf.getvalue(), encoding="utf-8")
                print(f"Updated {CONFIG_PATH}")
            except Exception as e:
                print(f"ERROR writing config: {e}", file=sys.stderr)
                sys.exit(1)
        elif (args.set_weight or args.set_priority or args.set_missing_release is not None) and args.dry_run:
            print("DRY RUN -- config not written.")
        if chain_delta is not None:
            last = load_last_selection()
            if last:
                ranking = build_ranking_from_mr(mr)
                models = [dict(m) for m in last if isinstance(m, dict)]
                ranked = rank_models(models, ranking)
                after_ids = [m.get("id") if isinstance(m, dict) else str(m) for m in ranked]
                print("\nDraft delta (cached selection re-ranked):")
                print(chain_delta.format_delta(before_ids, after_ids))
            else:
                print("\n(no cached selection to re-rank; run a full pass first)")
        sys.exit(0)

    dry_run = args.dry_run

    if args.full:
        top_n = None
    else:
        top_n = args.top if args.top else 3

    user_list = None
    if args.providers:
        user_list = [x.strip() for x in args.providers.split(",") if x.strip()]

    import atomic_io
    with atomic_io.file_lock(str(CACHE_PATH) + '.lock'):
        cache = load_cache()
        ranking = load_ranking_config()
        configured = providers.configured_providers(user_list=user_list, prefer_local=args.prefer_local)

    if args.model:
        print("Targeted retest:")
        for slug in configured:
            cfg = providers.PROVIDERS[slug]
            print(f"\n=== {cfg['display']} ({slug}) ===")
            ok, vnote = providers.validate_key(cfg)
            if not ok:
                print(f"  key validation failed: {vnote}")
                continue
            key, _ = providers.get_api_key(cfg)
            for mid in args.model:
                if not cfg.get("probe", True):
                    print(f"  {mid}: unverified (listing-only provider)")
                    continue
                if args.refresh:
                    cr = None
                else:
                    cr = cached_result(cache, slug, mid, False)
                if cr is not None:
                    cached_passed, cached_note, cached_retried = cr
                    passed, note, retried = cached_passed, cached_note, cached_retried
                    note = f"[cache] {note}"
                else:
                    passed, note, retried = probe(slug, cfg, mid, key, timeout=cfg.get("probe_timeout"), reasoning=reasoning_mode)
                    put_cache(cache, slug, mid, passed, note, retried)
                clean = bool(passed) and not bool(retried)
                print(f"  {mid}: {'PASS' if clean else 'FAIL'} ({note})")
        import atomic_io
        with atomic_io.file_lock(str(CACHE_PATH) + '.lock'):
            save_cache(cache)
        sys.exit(0)

    all_passing = []
    probed_all = []
    summary = []

    banned_set = banned
    cache_lock = threading.Lock()
    print_lock = threading.Lock()

    # Parallelize across providers: one worker per provider keeps each provider's
    # quota isolated; shared cache/banned access is guarded by locks. Results are
    # merged on the main thread so config/cache writes stay serialized.
    provider_results = {}
    done = 0
    total_providers = len(configured)
    with ThreadPoolExecutor(max_workers=max(1, len(configured))) as ex:
        futures = {}
        cooldown = getattr(args, "provider_cooldown", None)
        if cooldown is None:
            cooldown = PROVIDER_COOLDOWN_SECONDS
        for i, slug in enumerate(configured):
            if i > 0 and cooldown and cooldown > 0:
                time.sleep(cooldown)
            cfg = providers.PROVIDERS[slug]
            futures[ex.submit(_process_provider, slug, cfg,
                             ranking=ranking, reasoning_mode=reasoning_mode,
                             top_n=top_n, args=args,
                             banned=banned_set, cache=cache, cache_lock=cache_lock,
                             print_lock=print_lock)] = slug
        for fut in as_completed(futures):
            slug = futures[fut]
            try:
                passing_list, probed_list, summary_entry = fut.result()
            except Exception as e:
                print(f"  provider {slug} failed: {e}", file=sys.stderr)
                passing_list, probed_list, summary_entry = [], [], {"provider": slug, "fetched": 0, "passed": 0, "error": str(e)}
            all_passing.extend(passing_list)
            probed_all.extend(probed_list)
            summary.append(summary_entry)
            done += 1
            print(f"  [{done}/{total_providers} providers complete]")

    import atomic_io
    with atomic_io.file_lock(str(CACHE_PATH) + '.lock'):
        save_cache(cache)

    if getattr(args, "capability", None):
        cap = args.capability.lower().strip()
        all_passing = [m for m in all_passing if cap in (m.get("_capabilities") or set())]

    all_passing = rank_models(all_passing, ranking)

    # --- promotion gate ---
    margin = args.promotion_margin if args.promotion_margin is not None else PROMOTION_MARGIN
    hyst = args.hysteresis_days if args.hysteresis_days is not None else HYSTERESIS_DAYS
    emergency = not args.no_emergency
    promote = not getattr(args, "no_promote", False)
    import atomic_io
    with atomic_io.file_lock(str(PROMOTION_HISTORY_PATH) + '.lock'):
        history = load_promotion_history()
        today = getattr(args, "today", None) or datetime.now(timezone.utc).date().isoformat()
        # validate
        try:
            date.fromisoformat(today)
        except ValueError:
            print(f"ERROR: --today expects YYYY-MM-DD, got '{today}'")
            sys.exit(2)

        dry = args.dry_run
        if not args.dry_run and not args.apply:
            dry = False
        if args.dry_run:
            dry = True

        if not dry:
            update_promotion_history(history, probed_all, today)
            removed = prune_promotion_history(history, today)
            if removed:
                print(f"  Pruned {len(removed)} stale history entr(ies): {', '.join(removed)}")
            save_promotion_history(history)

        cfg0, _ = load_config()
        current_default = ""
        if isinstance(cfg0, dict):
            mc = cfg0.get("model")
            if isinstance(mc, dict):
                current_default = str(mc.get("default") or "")
        chosen, reason = (None, "--no-promote: chain only") if not promote else choose_default(
            all_passing, probed_all, history, current_default, margin, hyst, emergency=emergency)
        if getattr(args, "set_default", None):
            forced = None
            for m in all_passing:
                if str(m.get("id")) == str(args.set_default):
                    forced = m
                    break
            if forced is not None:
                chosen = forced
                reason = f"forced by --set-default: {args.set_default}"
                print(f"\nPromotion gate: {reason}")
            else:
                print(f"\n--set-default: {args.set_default} not in passing pool; gate unchanged")
        elif promote:
            print(f"\nPromotion gate: {reason}")

        if not dry and promote and chosen is not None:
            mid = _model_id(chosen)
            rec = history.setdefault(mid, _blank_promo_record(today))
            # ensure fields
            for k, v in _blank_promo_record(today).items():
                rec.setdefault(k, v)
            if current_default != mid:
                rec["promotion_count"] = rec.get("promotion_count", 0) + 1
                rec["promoted_at"] = today
            save_promotion_history(history)

    save_last_selection(all_passing)

    print("\n" + "=" * 70)
    print(f"Passing models ({len(all_passing)}):")
    print("=" * 70)
    for i, m in enumerate(all_passing, 1):
        rt = m.get("_response_time_ms")
        rt_s = f"  resp={rt}ms" if isinstance(rt, int) else ""
        print(f"  {i}. [{m['_provider']}] {m['id']}  ctx={m.get('context_window')}  score={m.get('_score')}  release={m.get('_release_date')}{rt_s}")

    if args.json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "top_n": top_n,
            "passing_count": len(all_passing),
            "passing": [
                {
                    "id": m.get("id"),
                    "provider": m.get("_provider"),
                    "context_window": m.get("context_window"),
                    "release_date": m.get("_release_date"),
                    "score": m.get("_score"),
                    "score_parts": m.get("_score_parts"),
                    "response_time_ms": m.get("_response_time_ms"),
                    "base_url": m.get("_base_url"),
                }
                for m in all_passing
            ],
            "summary": summary,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    if not all_passing:
        print("\nNo models passed probing -- config untouched.")
        sys.exit(0)

    dry = args.dry_run
    if not args.dry_run and not args.apply:
        dry = False
    if args.dry_run:
        dry = True

    if dry:
        print("\nDRY RUN -- nothing written. Would set:")
        if chosen is not None:
            print(f"  model.default -> {chosen['id']}  ({reason})")
        elif current_default:
            print(f"  model.default -> {current_default}  (unchanged; gate kept incumbent)")
        elif all_passing:
            print(f"  model.default -> {all_passing[0]['id']}")
        eff_id = _model_id(chosen) if chosen is not None else current_default
        for m in all_passing:
            if eff_id and _model_id(m) == eff_id:
                continue
            print(f"  fallback: {m['id']} ({m['_provider']})")
        sys.exit(0)

    import yaml
    write_config(all_passing, yaml, chosen=chosen, current_default=current_default, promote=promote)
    print("Done.")


if __name__ == "__main__":
    main()
