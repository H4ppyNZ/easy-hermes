"""selector.probe_cache — extracted from select-models.py

Split out of the original monolithic select-models.py.  Function bodies are
moved verbatim; only module-level path state is routed through
``selector.paths`` so it stays mutable at runtime.
"""
from __future__ import annotations

from selector.paths import CACHE_TTL_DAYS
from datetime import datetime, timedelta, timezone


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