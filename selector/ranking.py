"""selector.ranking — extracted from select-models.py

Split out of the original monolithic select-models.py.  Function bodies are
moved verbatim; only module-level path state is routed through
``selector.paths`` so it stays mutable at runtime.
"""
from __future__ import annotations

from selector.paths import DEFAULT_PROVIDER_PRIORITY, DEFAULT_RANKING_WEIGHTS
from selector.configio import load_config
from selector.lists import load_boosted
import json
import threading
from datetime import datetime, timezone
import urllib.request
_OR_CONTEXT_MAP = None
_OR_CTX_LOCK = threading.Lock()


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