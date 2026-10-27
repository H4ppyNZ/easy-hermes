#!/usr/bin/env python3
"""Poll Ollama Cloud for its (free-tier) model catalog (best-effort/unverified)."""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_url
try:
    import poll_base
except Exception:
    poll_base = None
from datetime import datetime, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_ORIGINAL_URLOPEN = urllib.request.urlopen
urlopen = _ORIGINAL_URLOPEN

DEFAULT_CANDIDATE_ENDPOINTS = [
    "https://ollama.com/api/tags",
    "https://api.ollama.com/v1/models",
    "https://ollama.com/v1/models",
    "https://api.ollama.com/api/tags",
]

MAX_PAGES = 10
RETRY_BACKOFF = (1, 2, 4)


def load_api_key(env=None):
    if env is not None:
        try:
            for k in ("OLLAMA_API_KEY", "OLLAMA_CLOUD_API_KEY"):
                v = env.get(k) if hasattr(env, "get") else None
                if v:
                    return str(v).strip()
        except Exception:
            pass
    for k in ("OLLAMA_API_KEY", "OLLAMA_CLOUD_API_KEY"):
        v = os.environ.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return None


def candidate_endpoints(api_key=None, extra=None):
    out = list(DEFAULT_CANDIDATE_ENDPOINTS)
    if extra:
        for u in extra:
            if u and u not in out:
                out.insert(0, u)
    return out


def _open(req, timeout):
    hook = globals().get("urlopen")
    current = urllib.request.urlopen
    if callable(hook) and hook is not _ORIGINAL_URLOPEN:
        opener = hook
    elif current is not _ORIGINAL_URLOPEN:
        opener = current
    else:
        opener = _ORIGINAL_URLOPEN
    return opener(req, timeout=timeout)


def http_get_json(url, api_key=None, timeout=20, retries=3):
    headers = {
        "User-Agent": "easy-hermes/poll-ollama-cloud",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    if poll_base is None:
        raise RuntimeError("poll_base unavailable")
    data, err = poll_base.http_get_json(
        url, headers=headers, timeout=timeout, backoff=RETRY_BACKOFF,
        urlopen=urllib.request.urlopen, sleep=time.sleep)
    if err:
        raise RuntimeError(f"http_get_json failed: {err}")
    return data


def next_page_url(payload, current_url, collected):
    if not isinstance(payload, dict):
        return None
    for key in ("next", "next_url", "next_page", "nextPage"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for key in ("next_cursor", "cursor", "after"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            base = current_url.split("?")[0]
            return base + "?" + urllib.parse.urlencode({key: v.strip()})
    if payload.get("has_more") is True and collected:
        last = collected[-1].get("id") if isinstance(collected[-1], dict) else None
        if last:
            base = current_url.split("?")[0]
            q = urllib.parse.urlencode({"after": last, "limit": 100})
            return base + "?" + q
    return None


def _merge_metadata(payload):
    out = {}
    models = payload.get("models") if isinstance(payload, dict) else None
    if isinstance(models, list):
        for m in models:
            if not isinstance(m, dict):
                continue
            key = m.get("slug") or m.get("id") or m.get("name") or m.get("model")
            if key is not None:
                out[str(key)] = m
    return out


def normalize_model(item, source=None):
    if isinstance(item, str):
        mid = item.strip()
        raw = {}
    elif isinstance(item, dict):
        mid = (item.get("id") or item.get("name") or item.get("model")
               or item.get("slug") or item.get("model_id") or item.get("model_name"))
        if mid is None:
            for v in item.values():
                if isinstance(v, str) and v.strip():
                    mid = v.strip()
                    break
        mid = str(mid).strip() if mid is not None else ""
        raw = item
    else:
        mid = str(item).strip()
        raw = {}
    rec = {
        "id": mid,
        "name": mid,
        "provider": "ollama-cloud",
        "free": True,
    }
    caps = raw.get("capabilities")
    if caps is None:
        caps = raw.get("input_modalities")
    if isinstance(caps, list):
        rec["capabilities"] = [str(c) for c in caps]
    for k in ("context_window", "max_context_window", "context_length"):
        if k in raw and raw.get(k) is not None:
            try:
                rec[k] = int(raw.get(k))
            except (TypeError, ValueError):
                rec[k] = raw.get(k)
    rlevels = raw.get("supported_reasoning_levels")
    if isinstance(rlevels, list):
        rec["reasoning_levels"] = [str(x.get("effort") if isinstance(x, dict) else x)
                                   for x in rlevels]
    if source is not None:
        rec["source"] = source
    for k in ("visibility", "supported_in_api", "owned_by", "object", "created", "modified_at", "updated_at"):
        if k in raw and raw.get(k) is not None:
            rec[k] = raw.get(k)
    return rec


def is_free(model):
    if isinstance(model, str):
        return True
    if not isinstance(model, dict):
        return True
    if "free" in model and isinstance(model.get("free"), bool):
        return bool(model.get("free"))
    tags = model.get("tags")
    if isinstance(tags, (list, tuple)):
        for t in tags:
            if "free" in str(t).lower():
                return True
    for key in ("pricing", "price", "cost", "rates"):
        if key not in model:
            continue
        val = model.get(key)
        if isinstance(val, bool):
            return val
        if isinstance(val, dict):
            nums = []
            for v in val.values():
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    nums.append(v)
                elif isinstance(v, str):
                    try:
                        nums.append(float(v))
                    except ValueError:
                        pass
                elif isinstance(v, dict):
                    for vv in v.values():
                        if isinstance(vv, bool):
                            continue
                        if isinstance(vv, (int, float)):
                            nums.append(vv)
                        elif isinstance(vv, str):
                            try:
                                nums.append(float(vv))
                            except ValueError:
                                pass
            if nums and any(n != 0 for n in nums):
                return False
            if nums:
                return True
            return True
        if val is None:
            continue
        try:
            num = float(val)
            return num == 0
        except (TypeError, ValueError):
            s = str(val).strip().lower()
            if s in ("", "free", "0", "0.0", "none", "null"):
                return True
            if "free" in s:
                return True
            return True
    tier = model.get("tier") or model.get("plan") or model.get("access")
    if isinstance(tier, str):
        t = tier.strip().lower()
        if t in ("free", "free-tier", "free_tier"):
            return True
        if t in ("pro", "max", "paid", "premium"):
            return False
    return True


def filter_free(models):
    out = []
    for m in models or []:
        if isinstance(m, dict):
            if is_free(m):
                mm = dict(m)
                mm["free"] = True
                out.append(mm)
        elif isinstance(m, str):
            out.append({"id": m, "name": m, "provider": "ollama-cloud", "free": True})
        else:
            out.append(m)
    return out


def parse_models_payload(payload, endpoint=None):
    if not isinstance(payload, dict):
        if isinstance(payload, list):
            return [x for x in payload if x is not None]
        return []
    meta = _merge_metadata(payload)
    out = []
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                if isinstance(item, str):
                    out.append({"id": item})
                continue
            merged = dict(item)
            mid = merged.get("id") or merged.get("name") or merged.get("model")
            if mid is not None and str(mid) in meta:
                for k, v in meta[str(mid)].items():
                    if k not in merged or merged.get(k) is None:
                        merged[k] = v
            out.append(merged)
    elif isinstance(payload.get("models"), list):
        for item in payload["models"]:
            if isinstance(item, dict):
                out.append(dict(item))
            elif isinstance(item, str):
                out.append({"name": item})
    seen = set()
    deduped = []
    for m in out:
        key = None
        if isinstance(m, dict):
            key = m.get("id") or m.get("name") or m.get("model") or m.get("slug")
        key = str(key) if key is not None else None
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        deduped.append(m)
    return deduped


def fetch_endpoint(url, api_key=None, timeout=20, retries=3):
    payload = http_get_json(url, api_key=api_key, timeout=timeout, retries=retries)
    collected_raw = parse_models_payload(payload, url)
    visited = {url}
    pages = 1
    current_url = url
    current_payload = payload
    collected = list(collected_raw)
    while pages < MAX_PAGES:
        nxt = next_page_url(current_payload, current_url, collected)
        nxt = safe_url.safe_next(nxt, url)
        if not nxt or nxt in visited:
            break
        visited.add(nxt)
        try:
            p = http_get_json(nxt, api_key=api_key, timeout=timeout, retries=retries)
        except Exception:
            break
        page_raw = parse_models_payload(p, nxt)
        if not page_raw:
            break
        collected.extend(page_raw)
        current_url = nxt
        current_payload = p
        pages += 1
    return collected


def fetch_models(endpoints=None, api_key=None, timeout=20, retries=3,
                 free_only=True, **kwargs):
    if endpoints is None:
        endpoints = kwargs.get("urls") or kwargs.get("endpoint") or kwargs.get("url")
    if isinstance(endpoints, str):
        endpoints = [endpoints]
    if endpoints is None:
        endpoints = kwargs.get("candidates")
    if api_key is None:
        api_key = kwargs.get("token") or kwargs.get("key")
    if timeout is None:
        timeout = kwargs.get("to", 20)
    if retries is None:
        retries = kwargs.get("retry", 3)

    key = api_key if api_key is not None else load_api_key()
    if isinstance(key, dict):
        key = load_api_key(key)

    cand = candidate_endpoints(key, extra=None)
    if isinstance(endpoints, list) and endpoints:
        merged = []
        for u in endpoints:
            if u not in merged:
                merged.append(u)
        for u in cand:
            if u not in merged:
                merged.append(u)
        cand = merged

    errors = []
    chosen_source = None
    raw = None
    for url in cand:
        try:
            raw = fetch_endpoint(url, api_key=key, timeout=timeout, retries=retries)
            if raw is None:
                raw = []
            if len(raw) > 0:
                chosen_source = url
                break
            if chosen_source is None:
                chosen_source = url
        except Exception as e:
            errors.append((url, repr(e)))
            continue
    if raw is None:
        raise RuntimeError("All candidate endpoints failed: " + "; ".join(f"{u}: {e}" for u, e in errors))

    to_normalize = raw
    if free_only:
        to_normalize = filter_free(raw)
    normalized = [normalize_model(m, chosen_source) for m in to_normalize]
    seen = set()
    deduped = []
    for m in normalized:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        deduped.append(m)

    for m in deduped:
        m.setdefault("source", chosen_source)
        m.setdefault("free", True)
    return deduped


def fetch_ollama_cloud_models(*a, **kw):
    return fetch_models(*a, **kw)


def poll(*a, **kw):
    try:
        return fetch_models(*a, **kw)
    except (Exception, SystemExit):
        return []


def poll_ollama_cloud(*a, **kw):
    return fetch_models(*a, **kw)


def get_models(*a, **kw):
    return fetch_models(*a, **kw)


def list_models(*a, **kw):
    return fetch_models(*a, **kw)


def fetch_free_models(*a, **kw):
    kw.setdefault("free_only", True)
    return fetch_models(*a, **kw)


def normalize(item, source=None):
    return normalize_model(item, source)


def normalize_models(items, source=None):
    return [normalize_model(i, source) for i in (items or [])]


def is_free_model(model):
    return is_free(model)


def filter_free_models(models):
    return filter_free(models)


def render_table(models):
    lines = []
    lines.append("=" * 78)
    lines.append("OLLAMA CLOUD MODELS  (best-effort / unverified free-tier)")
    lines.append("=" * 78)
    if not models:
        lines.append("(none)")
        lines.append("=" * 78)
        return "\n".join(lines)
    lines.append(f"{'#':>3}  {'Model ID':<46} {'Free':<5} {'Capabilities'}")
    lines.append("-" * 78)
    for i, m in enumerate(models, 1):
        mid = m.get("id") if isinstance(m, dict) else str(m)
        free = "yes" if (m.get("free", True) if isinstance(m, dict) else True) else "no"
        caps = ""
        if isinstance(m, dict):
            c = m.get("capabilities")
            if isinstance(c, list):
                caps = ", ".join(c)
        lines.append(f"{i:>3}  {str(mid):<46} {free:<5} {caps}")
    lines.append("-" * 78)
    lines.append(f"Total: {len(models)}")
    lines.append("=" * 78)
    return "\n".join(lines)


def print_table(models):
    print(render_table(models))


def build_output(models, source, endpoints=None):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "best_effort": True,
        "unverified": True,
        "status": "best-effort / unverified",
        "source": source,
        "candidate_endpoints": list(endpoints or DEFAULT_CANDIDATE_ENDPOINTS),
        "count": len(models),
        "models": models,
    }


def write_output(path, output):
    p = Path(path)
    if ".." in p.parts:
        raise ValueError("unsafe out path")
    p.parent.mkdir(parents=True, exist_ok=True)
    import atomic_io
    atomic_io.atomic_write_text(p, json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def save_json(path, output):
    return write_output(path, output)


def write_json(path, output):
    return write_output(path, output)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Poll Ollama Cloud free models (best-effort/unverified).")
    parser.add_argument("--out", default="out/ollama-cloud.json", help="Output JSON path")
    parser.add_argument("--endpoint", action="append", default=None, help="Force endpoint (repeatable)")
    parser.add_argument("--endpoints", nargs="*", default=None)
    parser.add_argument("--api-key", default=None, help="Ollama Cloud API key (prefer env)")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="Do not write output file")
    parser.add_argument("--json", action="store_true", help="Print normalized JSON to stdout")
    parser.add_argument("--no-table", action="store_true", help="Suppress table output")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--hermes-home", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--cache", default=None)
    args = parser.parse_args(argv)

    endpoints = []
    if args.endpoint:
        endpoints.extend(args.endpoint)
    if args.endpoints:
        endpoints.extend(args.endpoints)

    api_key = args.api_key or load_api_key()

    if not args.quiet:
        print("Polling Ollama Cloud model catalog (best-effort / unverified)...")

    try:
        models = fetch_models(endpoints=endpoints if endpoints else None,
                             api_key=api_key, timeout=args.timeout, retries=args.retries,
                             free_only=True)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    source = models[0].get("source") if models and isinstance(models[0], dict) else None
    if source is None:
        source = (endpoints[0] if endpoints else None) or (DEFAULT_CANDIDATE_ENDPOINTS[0])

    if not args.no_table and not args.quiet:
        print(render_table(models))

    output = build_output(models, source, endpoints if endpoints else DEFAULT_CANDIDATE_ENDPOINTS)

    if args.json:
        print(json.dumps(output, indent=2, ensure_ascii=False))

    if not args.dry_run:
        try:
            write_output(args.out, output)
            if not args.quiet:
                print(f"Wrote {len(models)} model(s) to {args.out}")
        except Exception as e:
            print(f"ERROR writing output: {e}", file=sys.stderr)
            return 1
    else:
        if not args.quiet:
            print("Dry run: output not written.")

    if not args.quiet:
        print("STATUS: best-effort / unverified. Confirm model access with your own account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
