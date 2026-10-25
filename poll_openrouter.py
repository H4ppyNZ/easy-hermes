#!/usr/bin/env python3
"""
Poll OpenRouter for its free-tier models and print a normalized list.

Status: verified (public API). OpenRouter's GET /api/v1/models endpoint is public
and OpenAI-compatible; no authentication is required. An optional API key raises
the rate limit (sent as Bearer).

This is a *poller*, not a selector: it does not probe models or rewrite config.yaml.
It only lists which models OpenRouter currently advertises as free, normalised to
a stable shape so other tooling can consume it.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ENDPOINT = "https://openrouter.ai/api/v1/models"
DEFAULT_OUT_DIR = "out"
DEFAULT_OUT_FILE = "openrouter.json"
BACKOFF = (2, 5, 10)
DEFAULT_TIMEOUT = 30
POLL_ENDPOINT = ENDPOINT


def _zero_price(value) -> bool:
    if isinstance(value, bool):
        return False
    if value is None:
        return False
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return str(value).strip() == "0"


def is_free_model(model) -> bool:
    if not isinstance(model, dict):
        return False
    pricing = model.get("pricing")
    if isinstance(pricing, dict):
        prompt = pricing.get("prompt")
        completion = pricing.get("completion")
        if _zero_price(prompt) and _zero_price(completion):
            return True
    mid = model.get("id") or model.get("model") or model.get("name") or ""
    return str(mid).endswith(":free")


is_free = is_free_model
is_free_entry = is_free_model


def normalize_model(model) -> dict:
    if not isinstance(model, dict):
        model = {}
    top = model.get("top_provider") if isinstance(model.get("top_provider"), dict) else {}

    context_window = model.get("context_length")
    if context_window is None:
        context_window = top.get("context_length")

    rate_limits = model.get("per_request_limits", None)
    if rate_limits is None:
        rate_limits = model.get("rate_limits", None)

    mid = model.get("id")
    if rate_limits is None:
        notes = ("Free model per OpenRouter listing; rate limits are not provided "
                 "by the listing endpoint (per_request_limits=null).")
    else:
        notes = "Free model per OpenRouter listing; rate limits taken from listing."

    return {
        "id": mid,
        "display_name": model.get("name") or mid,
        "context_window": context_window,
        "rate_limits": rate_limits,
        "notes": notes,
    }


normalize = normalize_model


def _build_headers(api_key=None):
    headers = {
        "User-Agent": "easy-hermes/poll_openrouter",
        "Accept": "application/json",
    }
    key = api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def http_get_json(url, headers=None, timeout=DEFAULT_TIMEOUT, backoff=BACKOFF):
    headers = headers or {}
    last_error = None
    delays = (0,) + tuple(backoff)
    for idx, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def _next_url(payload, current, endpoint):
    if not isinstance(payload, dict):
        return None

    candidate = None
    links = payload.get("links")
    if isinstance(links, dict):
        candidate = links.get("next") or links.get("next_url") or links.get("next_cursor")
    if candidate is None:
        candidate = payload.get("next") or payload.get("next_url") or payload.get("next_cursor")

    if isinstance(candidate, str):
        candidate = candidate.strip()
        if not candidate:
            return None
        if candidate.startswith("http"):
            return candidate
        if candidate.startswith("/"):
            return urllib.parse.urljoin(endpoint, candidate)
        sep = "&" if "?" in endpoint else "?"
        return f"{endpoint}{sep}cursor={urllib.parse.quote(candidate)}"

    if isinstance(candidate, dict):
        cursor = candidate.get("cursor") or candidate.get("next") or candidate.get("token")
        if cursor:
            sep = "&" if "?" in endpoint else "?"
            return f"{endpoint}{sep}cursor={urllib.parse.quote(str(cursor))}"
        return None

    cursor = payload.get("cursor")
    if isinstance(cursor, str) and cursor.strip():
        sep = "&" if "?" in endpoint else "?"
        return f"{endpoint}{sep}cursor={urllib.parse.quote(cursor.strip())}"

    return None


def fetch_all_models(api_key=None, timeout=DEFAULT_TIMEOUT, endpoint=ENDPOINT,
                    backoff=BACKOFF):
    models = []
    visited = set()
    url = endpoint
    while url:
        if url in visited:
            break
        visited.add(url)

        data, err = http_get_json(url, headers=_build_headers(api_key),
                                  timeout=timeout, backoff=backoff)
        if err:
            print(f"  ERROR fetching {url}: {err}")
            break
        if not isinstance(data, dict):
            print(f"  ERROR: unexpected response shape from {url}")
            break

        page = data.get("data")
        if isinstance(page, list):
            models.extend(page)
        elif isinstance(data.get("models"), list):
            models.extend(data["models"])
        elif isinstance(data, list):
            models.extend(data)

        nxt = _next_url(data, url, endpoint)
        url = nxt if (nxt and nxt not in visited) else None

    seen = set()
    unique = []
    for m in models:
        key = m.get("id") if isinstance(m, dict) else m
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)
    return unique


fetch_models = fetch_all_models
fetch_openrouter_models = fetch_all_models
list_models = fetch_all_models
fetch_free_models = fetch_all_models
get_free_models = fetch_all_models
get_models = fetch_all_models


def poll(api_key=None, timeout=DEFAULT_TIMEOUT, endpoint=ENDPOINT, backoff=BACKOFF):
    try:
        raw = fetch_all_models(api_key=api_key, timeout=timeout, endpoint=endpoint,
                               backoff=backoff)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        raw = []
    free = [m for m in raw if isinstance(m, dict) and is_free_model(m)]
    return [normalize_model(m) for m in free]


def _print_table(models):
    print()
    print(f"{'ID':<46} {'DISPLAY NAME':<34} {'CONTEXT':>10}  RATE LIMITS")
    print("-" * 110)
    for m in models:
        ctx = m.get("context_window")
        ctx_s = f"{ctx:,}" if isinstance(ctx, int) else (str(ctx) if ctx is not None else "-")
        rl = m.get("rate_limits")
        rl_s = "null" if rl is None else json.dumps(rl, separators=(",", ":"))
        print(f"{str(m.get('id'))[:46]:<46} {str(m.get('display_name'))[:34]:<34} {ctx_s:>10}  {rl_s}")
    print("-" * 110)
    print(f"Total free models: {len(models)}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    write_json = "--json" in argv
    out_dir = DEFAULT_OUT_DIR
    if "--out" in argv:
        try:
            out_dir = argv[argv.index("--out") + 1]
        except (ValueError, IndexError):
            print("ERROR: --out requires a directory argument")
            return 2
    if "--output" in argv:
        try:
            out_dir = argv[argv.index("--output") + 1]
        except (ValueError, IndexError):
            print("ERROR: --output requires a directory argument")
            return 2

    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")
    print("Polling OpenRouter free models...")
    print(f"Endpoint: {ENDPOINT}")
    if api_key:
        print("Auth: OPENROUTER_API_KEY present (Bearer sent for higher rate limits)")
    else:
        print("Auth: none (public listing; set OPENROUTER_API_KEY for higher limits)")

    try:
        models = poll(api_key=api_key)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        models = []

    _print_table(models)

    if not models:
        print("No free models found (or fetch failed).")

    if write_json:
        out_path = Path(out_dir) / DEFAULT_OUT_FILE
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "source": ENDPOINT,
                "count": len(models),
                "models": models,
            }
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(f"Wrote JSON: {out_path}")
        except OSError as e:
            print(f"ERROR writing {out_path}: {e}")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
