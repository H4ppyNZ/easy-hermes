"""Poll Nous Portal free models from the local Hermes endpoint metadata cache.

Nous is handled specially by select-models.py (auth + probe). This
poller only lists the free models already cached in
~/.hermes/cache/endpoint_model_metadata.json so select-models.py can include
Nous in its candidate pool (listing only, no probing here).
"""
import json
import os
from pathlib import Path
try:
    import release_dates as _rd
    HARDCODED_DATES = _rd.HARDCODED_DATES
except Exception:
    HARDCODED_DATES = {}

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (LOCALAPPDATA / "hermes"))
META_CACHE_PATH = HERMES_HOME / "cache" / "endpoint_model_metadata.json"
NOUS_ENDPOINT = "https://inference-api.nousresearch.com/v1"


def _load_cache():
    if not META_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(META_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def is_free_model(info):
    if not isinstance(info, dict):
        return False
    pricing = info.get("pricing", {})
    try:
        return float(pricing.get("prompt", "0")) == 0 and float(pricing.get("completion", "0")) == 0
    except (TypeError, ValueError):
        return False


def normalize_model(model_id, info):
    if not isinstance(info, dict):
        info = {}
    seen = set()
    created = HARDCODED_DATES.get(str(model_id))
    if created is None:
        # fall back to any date field the cache might carry
        for key in ("created", "release_date", "updated_at", "modified_at"):
            v = info.get(key)
            if v:
                created = v if isinstance(v, str) else str(v)
                break
    return {
        "id": model_id,
        "display_name": info.get("name", model_id),
        "context_window": info.get("context_length"),
        "created": created,
        "rate_limits": None,
        "notes": "Nous Portal free model (from endpoint_model_metadata cache).",
    }


def fetch_models(**kwargs):
    data = _load_cache()
    bucket = data.get(NOUS_ENDPOINT, {}) if isinstance(data, dict) else {}
    models = bucket.get("models", {}) if isinstance(bucket, dict) else {}
    out = []
    seen_names = set()
    for mid, info in (models or {}).items():
        if not is_free_model(info):
            continue
        name = (info or {}).get("name", mid)
        if name in seen_names:
            continue
        seen_names.add(name)
        out.append(normalize_model(mid, info))
    return out


def fetch_free_models(**kwargs):
    return fetch_models(**kwargs)


def poll(**kwargs):
    return fetch_models(**kwargs)


DEFAULT_OUT_FILE = "nous.json"
DEFAULT_OUT_DIR = "out"

if __name__ == "__main__":
    import argparse, json as _json
    from pathlib import Path
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", "--output", dest="out_dir", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()
    models = poll()
    if args.json:
        out_path = Path(args.out_dir) / DEFAULT_OUT_FILE
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"source": str(META_CACHE_PATH), "count": len(models), "models": models}
            out_path.write_text(_json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"Wrote JSON: {out_path}")
        except OSError as e:
            print(f"ERROR writing {out_path}: {e}")
    else:
        for m in models:
            print(m["id"], m.get("context_window"))
