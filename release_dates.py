"""Release-date resolution helpers (moved out of hermes-model-selector.py)."""
from __future__ import annotations
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime

_OR_MAP = None


def get_openrouter_map() -> dict:
    global _OR_MAP
    if _OR_MAP is None:
        try:
            req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            _OR_MAP = {m.get("id"): m.get("created") for m in data.get("data", []) if m.get("id")}
            print(f"  OpenRouter: indexed {len(_OR_MAP)} models")
        except Exception as e:
            print(f"  OpenRouter list fetch failed ({e}); continuing without it")
            _OR_MAP = {}
    return _OR_MAP


def get_openrouter_release_date(model_id: str):
    created = get_openrouter_map().get(model_id.replace(":free", ""))
    if created:
        return (datetime.fromtimestamp(created).strftime("%Y-%m-%d"), "openrouter")
    return None


def get_openrouter_web_release_date(model_id: str):
    try:
        _mid = urllib.parse.quote(model_id.replace(":free", ""), safe="/")
        req = urllib.request.Request(f"https://openrouter.ai/{_mid}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        match = re.search(r"RELEASED\s*([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})", html)
        if match:
            dt = datetime.strptime(match.group(1), "%b %d, %Y")
            return (dt.strftime("%Y-%m-%d"), "openrouter_web")
    except Exception:
        pass
    return None


def get_hf_release_date(model_id: str):
    try:
        _mid = urllib.parse.quote(model_id.replace(":free", ""), safe="/")
        req = urllib.request.Request(f"https://huggingface.co/api/models/{_mid}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            created = json.loads(resp.read()).get("createdAt", "")
        if created:
            return (created[:10], "huggingface")
    except Exception:
        pass
    return None


HARDCODED_DATES = {
    "meituan/longcat-2.5-preview:free": "2026-09-25",
    "upstage/solar-mini4:free": "2026-09-23",
    "meituan/longcat-2.0:free": "2026-07-20",
    "stepfun/step-3.7-flash:free": "2025-05-28",
}


def get_release_date(model_id: str):
    for fn in (get_openrouter_release_date, get_openrouter_web_release_date, get_hf_release_date):
        if (result := fn(model_id)):
            return result
    if model_id in HARDCODED_DATES:
        return (HARDCODED_DATES[model_id], "hardcoded")
    import re as _re
    ym = _re.search(r"(20\d{2})", str(model_id))
    if ym:
        return (f"{ym.group(1)}-01-01", "approx")
    return None