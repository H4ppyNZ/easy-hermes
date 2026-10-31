#!/usr/bin/env python3
"""Poll Cloudflare Workers AI for its model catalog (best-effort / unverified).

Cloudflare Workers AI is account-scoped: listing models requires an account id
and an API token scoped to Workers AI (read). Models consume Neurons from the
account's free daily allocation (10k/day) or paid balance; none are $0/token in
the Nous sense, so every entry is marked best-effort/unverified.

Environment:
  CLOUDFLARE_API_TOKEN / CLOUDFLARE_API_KEY   (Workers AI read/run)
  CLOUDFLARE_ACCOUNT_ID / CF_ACCOUNT_ID       (dashboard -> Workers AI -> Use REST API)
Optional override:
  CLOUDFLARE_BASE_URL  (proxy / AI Gateway root ending in /ai/v1 or /ai)
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

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

TOKEN_ENVS = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_KEY", "CLOUDFLARE_WORKERS_AI_TOKEN")
ACCOUNT_ENVS = ("CLOUDFLARE_ACCOUNT_ID", "CF_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT")

CHAT_TASKS = {
    "text generation", "text-generation", "chat", "chat completion",
    "text-generation (chat)", "conversational",
}

MAX_PAGES = 10
RETRY_BACKOFF = (1, 2, 4)


def load_api_key(env=None):
    if env is not None:
        try:
            for k in TOKEN_ENVS:
                v = env.get(k) if hasattr(env, "get") else None
                if v:
                    return str(v).strip()
        except Exception:
            pass
    for k in TOKEN_ENVS:
        v = os.environ.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return None


def load_account_id(env=None):
    if env is not None:
        try:
            for k in ACCOUNT_ENVS:
                v = env.get(k) if hasattr(env, "get") else None
                if v:
                    return str(v).strip()
        except Exception:
            pass
    for k in ACCOUNT_ENVS:
        v = os.environ.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return None


def _base_url(account_id, override=None):
    if override:
        return str(override).rstrip("/")
    if not account_id:
        return None
    return "https://api.cloudflare.com/client/v4/accounts/{}/ai/models/search".format(
        urllib.parse.quote(str(account_id), safe=""))


def candidate_endpoints(account_id=None, api_key=None, extra=None, override=None):
    out = []
    if account_id is None:
        account_id = load_account_id()
    base = _base_url(account_id, override or os.environ.get("CLOUDFLARE_BASE_URL"))
    if base:
        out.append(base)
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
        "User-Agent": "easy-hermes/poll-cloudflare",
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
        raise RuntimeError("http_get_json failed: " + str(err))
    return data


def _result_list(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get("result"), list):
            return payload.get("result")
        if isinstance(payload.get("data"), list):
            return payload.get("data")
        if isinstance(payload.get("models"), list):
            return payload.get("models")
    if isinstance(payload, list):
        return payload
    return []


def parse_models_payload(payload):
    items = _result_list(payload)
    out = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            if isinstance(item, str):
                item = {"id": item, "name": item}
            else:
                continue
        mid = (item.get("name") or item.get("model") or item.get("slug")
               or item.get("model_id") or item.get("id"))
        if mid is None:
            for v in item.values():
                if isinstance(v, str) and v.strip():
                    mid = v.strip()
                    break
        mid = str(mid).strip() if mid is not None else ""
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(dict(item))
    return out


def _is_chat_task(item):
    task = ""
    if isinstance(item, dict):
        t = item.get("task") or item.get("task_name") or item.get("type")
        if isinstance(t, dict):
            task = str(t.get("name") or t.get("id") or "").strip().lower()
        elif t is not None:
            task = str(t).strip().lower()
    if not task:
        return True  # unknown -> keep, let downstream filter
    return task in CHAT_TASKS or "generation" in task or "chat" in task


def normalize_model(item, source=None):
    if isinstance(item, str):
        mid = item.strip()
        raw = {}
    elif isinstance(item, dict):
        mid = (item.get("name") or item.get("model") or item.get("slug")
               or item.get("model_id") or item.get("id"))
        mid = str(mid).strip() if mid is not None else ""
        raw = item
    else:
        mid = str(item).strip()
        raw = {}
    rec = {
        "id": mid,
        "name": mid,
        "provider": "cloudflare",
        "free": True,
    }
    # context window hints (Cloudflare lists vary by model; keep if present)
    for k in ("context_window", "max_context_window", "context_length",
              "contextWindow", "max_input_tokens"):
        if k in raw and raw.get(k) is not None:
            try:
                rec["context_window"] = int(raw.get(k))
            except (TypeError, ValueError):
                rec["context_window"] = raw.get(k)
            break
    # Cloudflare provides created_at (use as release-date source) and a
    # properties array (price, async_queue, realtime, ...).
    if "created_at" in raw and raw.get("created_at") is not None:
        rec["created"] = raw.get("created_at")
        rec["created_at"] = raw.get("created_at")
    props = raw.get("properties")
    if isinstance(props, list):
        for prop in props:
            if not isinstance(prop, dict):
                continue
            pid = prop.get("property_id")
            if pid == "price":
                val = prop.get("value")
                if val is not None:
                    rec["price"] = val
            elif pid in ("async_queue", "realtime"):
                rec[pid] = prop.get("value")
    t = raw.get("task")
    if isinstance(t, dict):
        t = t.get("name") or t.get("id")
    if t is not None:
        rec["task"] = t
    if "description" in raw and raw.get("description") is not None:
        rec["description"] = raw.get("description")
    if source is not None:
        rec["source"] = source
    for k in ("created", "modified", "updated_at", "author"):
        if k in raw and raw.get(k) is not None:
            rec[k] = raw.get(k)
    return rec


def is_free(model):
    # Cloudflare models consume Neurons; treat listing as best-effort free-tier
    # (usable within the 10k/day free allocation). None are $0/token.
    if isinstance(model, str):
        return True
    if not isinstance(model, dict):
        return True
    return True


def is_free_model(model):
    return is_free(model)


def filter_free(models):
    out = []
    for m in models or []:
        if isinstance(m, dict):
            if _is_chat_task(m) and is_free(m):
                mm = dict(m)
                mm["free"] = True
                out.append(mm)
        elif isinstance(m, str):
            out.append({"id": m, "name": m, "provider": "cloudflare", "free": True})
    return out


def fetch_endpoint(url, api_key=None, timeout=20, retries=3, account_id=None):
    # Cloudflare returns {success, result, ...}; paginate via page/per_page
    collected = []
    visited = set()
    current_url = url
    pages = 0
    while pages < MAX_PAGES:
        if current_url in visited:
            break
        visited.add(current_url)
        try:
            payload = http_get_json(current_url, api_key=api_key, timeout=timeout, retries=retries)
        except Exception:
            break
        page_items = parse_models_payload(payload)
        if not page_items:
            # still accept a single empty page to stop
            break
        collected.extend(page_items)
        pages += 1
        # pagination: result_info.total_pages / page
        info = payload.get("result_info") if isinstance(payload, dict) else None
        nxt = None
        if isinstance(info, dict):
            try:
                cur = int(info.get("page", pages))
                total = int(info.get("total_pages", pages))
            except (TypeError, ValueError):
                cur, total = pages, pages
            if cur < total:
                # rebuild url with page param
                parsed = urllib.parse.urlsplit(current_url)
                q = urllib.parse.parse_qs(parsed.query)
                q["page"] = [str(cur + 1)]
                q.setdefault("per_page", ["100"])
                nxt = urllib.parse.urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path,
                     urllib.parse.urlencode(q, doseq=True), parsed.fragment))
        if not nxt:
            break
        current_url = safe_url.safe_next(nxt, url) or nxt
        if not current_url:
            break
    return collected


def fetch_models(endpoints=None, api_key=None, timeout=20, retries=3,
                 free_only=True, account_id=None, **kwargs):
    if endpoints is None:
        endpoints = kwargs.get("urls") or kwargs.get("endpoint") or kwargs.get("url")
    if isinstance(endpoints, str):
        endpoints = [endpoints]
    if api_key is None:
        api_key = kwargs.get("token") or kwargs.get("key") or kwargs.get("api_key")
    if account_id is None:
        account_id = kwargs.get("account_id") or kwargs.get("account")
    if timeout is None:
        timeout = kwargs.get("to", 20)
    if retries is None:
        retries = kwargs.get("retry", 3)

    key = api_key if api_key is not None else load_api_key()
    if isinstance(key, dict):
        key = load_api_key(key)
    if account_id is None:
        account_id = load_account_id()

    if not account_id:
        # cannot build an endpoint without an account id
        return []

    override = None
    if isinstance(endpoints, list) and endpoints:
        # allow a fully-qualified override
        override = endpoints[0]

    cand = candidate_endpoints(account_id=account_id, api_key=key,
                              extra=endpoints if isinstance(endpoints, list) else None,
                              override=override)
    # if user passed explicit endpoints, prefer them
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
            raw = fetch_endpoint(url, api_key=key, timeout=timeout, retries=retries,
                                account_id=account_id)
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
        return []

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


def fetch_cloudflare_models(*a, **kw):
    return fetch_models(*a, **kw)


def poll(*a, **kw):
    try:
        return fetch_models(*a, **kw)
    except (Exception, SystemExit):
        return []


def poll_cloudflare(*a, **kw):
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


def filter_free_models(models):
    return filter_free(models)


def render_table(models):
    lines = []
    lines.append("=" * 78)
    lines.append("CLOUDFLARE WORKERS AI MODELS  (best-effort / unverified; consumes Neurons)")
    lines.append("=" * 78)
    if not models:
        lines.append("(none)")
        lines.append("=" * 78)
        return "\n".join(lines)
    lines.append(f"{'#':>3}  {'Model ID':<46} {'Task'}")
    lines.append("-" * 78)
    for i, m in enumerate(models, 1):
        mid = m.get("id") if isinstance(m, dict) else str(m)
        task = m.get("task") if isinstance(m, dict) else ""
        lines.append(f"{i:>3}  {str(mid):<46} {task}")
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
        "status": "best-effort / unverified (consumes Neurons)",
        "source": source,
        "candidate_endpoints": list(endpoints or []),
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


def write_json(path, output):
    return write_output(path, output)


def save_json(path, output):
    return write_output(path, output)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Poll Cloudflare Workers AI models (best-effort/unverified).")
    parser.add_argument("--out", default="out/cloudflare.json", help="Output JSON path")
    parser.add_argument("--endpoint", action="append", default=None, help="Force endpoint (repeatable)")
    parser.add_argument("--endpoints", nargs="*", default=None)
    parser.add_argument("--api-key", default=None, help="Cloudflare API token (prefer env)")
    parser.add_argument("--account-id", default=None, help="Cloudflare account id (prefer env)")
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
    account_id = args.account_id or load_account_id()

    if not args.quiet:
        print("Polling Cloudflare Workers AI model catalog (best-effort / unverified)...")

    try:
        models = fetch_models(endpoints=endpoints if endpoints else None,
                             api_key=api_key, account_id=account_id,
                             timeout=args.timeout, retries=args.retries,
                             free_only=True)
    except Exception as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return 1

    source = models[0].get("source") if models and isinstance(models[0], dict) else None
    if source is None:
        source = (endpoints[0] if endpoints else None)
    if source is None and account_id:
        source = "https://api.cloudflare.com/client/v4/accounts/{}/ai/models/search".format(account_id)

    if not args.no_table and not args.quiet:
        print(render_table(models))

    output = build_output(models, source, endpoints if endpoints else None)

    if args.json:
        print(json.dumps(output, indent=2, ensure_ascii=False))

    if not args.dry_run:
        try:
            write_output(args.out, output)
            if not args.quiet:
                print("Wrote {} model(s) to {}".format(len(models), args.out))
        except Exception as e:
            print("ERROR writing output: " + str(e), file=sys.stderr)
            return 1
    else:
        if not args.quiet:
            print("Dry run: output not written.")

    if not args.quiet:
        print("STATUS: best-effort / unverified. Confirm model access with your own account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
