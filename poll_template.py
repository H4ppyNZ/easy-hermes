"""Template poller using poll_base. Copy this file for a new provider.

Only three provider-specific pieces are needed:
  1. ENDPOINT / auth env vars
  2. is_free_model(raw) -> bool
  3. normalize_model(raw) -> normalized dict
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import poll_base


ENDPOINT = "https://example.com/api/models"


def _build_headers(api_key=None):
    headers = {"Accept": "application/json", "User-Agent": "easy-hermes/poll-template"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def is_free_model(raw):
    # TODO: provider-specific free rule
    return True


def normalize_model(raw):
    # TODO: map to normalized schema
    return {
        "id": raw.get("id"),
        "display_name": raw.get("name"),
        "context_window": raw.get("context_length"),
        "rate_limits": None,
        "notes": "template",
    }


def fetch_models(api_key=None, timeout=30, **kw):
    import urllib.request
    headers = _build_headers(api_key)
    data, err = poll_base.http_get_json(ENDPOINT, headers=headers, timeout=timeout,
                                        urlopen=urllib.request.urlopen)
    if err or not isinstance(data, dict):
        return []
    raws = data.get("data") or data.get("models") or []
    return [normalize_model(m) for m in raws if isinstance(m, dict) and is_free_model(m)]


def poll(*a, **kw):
    try:
        return fetch_models(*a, **kw)
    except (Exception, SystemExit):
        return []


if __name__ == "__main__":
    import json
    models = poll()
    print(json.dumps({"source": ENDPOINT, "count": len(models), "models": models}, indent=2))
