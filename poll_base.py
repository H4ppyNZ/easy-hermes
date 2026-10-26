"""Shared base helpers for pollers.

Goal: new providers should only need to supply (a) a fetch of raw records,
(b) is_free_model, (c) normalize_model. Everything else (HTTP retry, pagination
safety, JSON output) lives here.

HTTP helpers accept injectable ``urlopen``/``sleep`` so existing tests that
patch ``poll_x.urllib.request.urlopen`` keep working when a poller delegates.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


def http_get_json(url, headers=None, timeout=30, backoff=(1.0, 2.0, 4.0),
                  urlopen=None, sleep=None):
    """Fetch url and return (data, error_str). Retries on 429/5xx and network errors."""
    if urlopen is None:
        urlopen = urllib.request.urlopen
    if sleep is None:
        sleep = time.sleep
    headers = dict(headers or {})
    last_error = None
    delays = (0,) + tuple(backoff)
    for idx, delay in enumerate(delays):
        if delay:
            sleep(delay)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            try:
                return json.loads(raw.decode("utf-8")), None
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                return None, f"invalid JSON: {e}"
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                body = ""
            last_error = f"HTTP {e.code}: {e.reason} {body}".strip()
            retryable = e.code == 429 or e.code >= 500
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            retryable = True

        if idx == len(delays) - 1 or not retryable:
            break
    return None, last_error


def safe_origin_check(nxt, base):
    """Return nxt if same-origin as base (or relative), else None."""
    try:
        import safe_url
        return safe_url.safe_next(nxt, base)
    except Exception:
        return nxt


def paginate(fetch_fn, start_url, next_fn, visited=None):
    """Generic pagination loop.

    fetch_fn(url) -> (data, err). next_fn(data, current_url) -> next url or None.
    Returns (list_of_data, last_error_or_None).
    """
    if visited is None:
        visited = set()
    visited.add(start_url)
    current = start_url
    data, err = fetch_fn(current)
    if err or not isinstance(data, dict):
        return ([data] if data is not None else []), err
    pages = [data]
    while True:
        nxt = next_fn(data, current)
        nxt = safe_origin_check(nxt, start_url)
        if not nxt or nxt in visited:
            break
        visited.add(nxt)
        d, e = fetch_fn(nxt)
        if e or not isinstance(d, dict):
            if e:
                return pages, e
            break
        pages.append(d)
        current = nxt
        data = d
    return pages, None


def write_json_output(path, source, count, models):
    import pathlib
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"source": source, "count": count, "models": models}
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def http_get_full(url, headers=None, timeout=30, backoff=(1.0, 2.0, 4.0),
                  urlopen=None, sleep=None):
    """Like http_get_json but also returns response headers.

    Returns (data, headers_dict_or_None, err). Useful for pollers that need
    Retry-After / rate-limit headers (e.g. NVIDIA, Gemini).
    """
    if urlopen is None:
        urlopen = urllib.request.urlopen
    if sleep is None:
        sleep = time.sleep
    headers = dict(headers or {})
    last_error = None
    resp_headers = None
    delays = (0,) + tuple(backoff)
    for idx, delay in enumerate(delays):
        if delay:
            sleep(delay)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                resp_headers = dict(resp.headers or {})
            try:
                return json.loads(raw.decode("utf-8")), resp_headers, None
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                return None, resp_headers, f"invalid JSON: {e}"
        except urllib.error.HTTPError as e:
            try:
                resp_headers = dict(e.headers or {})
            except Exception:
                resp_headers = {}
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                body = ""
            last_error = f"HTTP {e.code}: {e.reason} {body}".strip()
            retryable = e.code == 429 or e.code >= 500
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            retryable = True

        if idx == len(delays) - 1 or not retryable:
            break
    return None, resp_headers, last_error
