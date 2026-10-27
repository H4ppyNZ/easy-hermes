#!/usr/bin/env python3
"""Poll NVIDIA NIM free-tier models."""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_url

try:
    import poll_base
except Exception:
    poll_base = None
import urllib.request
from urllib.parse import urljoin
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

NVIDIA_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
MODELS_URL = NVIDIA_MODELS_URL
API_URL = NVIDIA_MODELS_URL
BASE_URL = "https://integrate.api.nvidia.com/v1"
ENDPOINT = NVIDIA_MODELS_URL
DEFAULT_RATE_LIMIT_RPM = 40
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = (1.0, 2.0, 4.0)

_env_base = os.environ.get("NVIDIA_BASE_URL") or os.environ.get("NIM_BASE_URL")
if _env_base:
    try:
        import urllib.parse as _up
        _parsed = _up.urlsplit(_env_base)
        _host = _parsed.netloc.lower()
        _scheme = _parsed.scheme.lower()
    except Exception:
        _host = ""
        _scheme = ""
    if _host and _host in ("integrate.api.nvidia.com",) and _scheme in ("", "http", "https"):
        _b = _env_base.rstrip("/")
        if not _b.endswith("/models"):
            _b = _b + "/models"
        NVIDIA_MODELS_URL = _b
        MODELS_URL = _b
        API_URL = _b
        # update ENDPOINT/DEFAULT? keep default host guard only


_BASE_URLOPEN = urllib.request.urlopen
urlopen = urllib.request.urlopen

try:
    import requests as _requests_module
except Exception:
    _requests_module = None
_ORIGINAL_REQUESTS_GET = (
    getattr(_requests_module, "get", None) if _requests_module is not None else None
)


class NvidiaAuthError(SystemExit, RuntimeError):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code
        self.message = message


class NvidiaHTTPError(RuntimeError):
    pass


class _HeaderMap:
    def __init__(self, raw):
        self._raw = raw

    def get(self, key, default=None):
        if self._raw is None:
            return default
        if hasattr(self._raw, "get"):
            try:
                v = self._raw.get(key, None)
                if v is not None:
                    return v
            except Exception:
                pass
        try:
            for k, v in (self._raw.items() if hasattr(self._raw, "items") else []):
                if str(k).lower() == str(key).lower():
                    return v
        except Exception:
            pass
        return default

    def items(self):
        try:
            return self._raw.items()
        except Exception:
            return []


def _normalize_headers(raw):
    return _HeaderMap(raw)


def _urlopen(request, timeout=None):
    mod = sys.modules.get(__name__)
    cand = getattr(mod, "urlopen", None) if mod is not None else None
    if callable(cand) and cand is not _BASE_URLOPEN:
        return cand(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def _http_get(url, headers, timeout):
    mod = sys.modules.get(__name__)
    rq = getattr(mod, "requests", _requests_module) if mod is not None else _requests_module
    get_fn = getattr(rq, "get", None) if rq is not None else None
    use_requests = False
    if get_fn is not None:
        if _ORIGINAL_REQUESTS_GET is None or get_fn is not _ORIGINAL_REQUESTS_GET:
            use_requests = True

    if use_requests:
        try:
            resp = get_fn(url, headers=headers, timeout=timeout)
        except Exception as e:
            return None, _HeaderMap(None), None, e
        status = getattr(resp, "status_code", None)
        hdrs = _normalize_headers(getattr(resp, "headers", None))
        data = None
        if hasattr(resp, "json"):
            try:
                data = resp.json()
            except Exception:
                data = None
        if data is None:
            text = getattr(resp, "text", None)
            if text is None:
                content = getattr(resp, "content", None)
                if isinstance(content, (bytes, bytearray)):
                    text = content.decode("utf-8", errors="replace")
                elif content is not None:
                    text = str(content)
            if text is not None:
                try:
                    data = json.loads(text)
                except (ValueError, TypeError):
                    data = text
        if data is None:
            data = {}
        return status, hdrs, data, None

    req = urllib.request.Request(url, headers=headers, method="GET")
    status = None
    hdrs_raw = None
    body = b""
    try:
        with _urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                gc = getattr(resp, "getcode", None)
                status = gc() if callable(gc) else None
            hdrs_raw = getattr(resp, "headers", None)
            if hdrs_raw is None:
                gh = getattr(resp, "getheaders", None)
                if callable(gh):
                    try:
                        hdrs_raw = dict(gh())
                    except Exception:
                        hdrs_raw = None
            read = getattr(resp, "read", None)
            if callable(read):
                body = read()
    except urllib.error.HTTPError as e:
        status = e.code
        hdrs_raw = getattr(e, "headers", None)
        read = getattr(e, "read", None)
        if callable(read):
            try:
                body = read()
            except Exception:
                body = b""
    except Exception as e:
        return None, _HeaderMap(None), None, e

    if isinstance(body, (bytes, bytearray)):
        text = body.decode("utf-8", errors="replace")
    elif isinstance(body, str):
        text = body
    else:
        text = "" if body is None else str(body)
    try:
        data = json.loads(text) if text.strip() else {}
    except (ValueError, TypeError):
        data = text
    if not isinstance(data, (dict, list)):
        data = {"data": data} if data is not None else {}
    return status, _normalize_headers(hdrs_raw), data, None


def resolve_api_key():
    for var in ("NVIDIA_API_KEY", "NGC_API_KEY"):
        val = os.environ.get(var)
        if val and str(val).strip():
            return str(val).strip(), var
    return None, None


def get_api_key():
    key, _ = resolve_api_key()
    return key


def _is_chat_like(model_id):
    lid = str(model_id or "").lower()
    for token in ("embed", "rerank", "reward", "guard", "safety", "parse"):
        if token in lid:
            return False
    return True


def is_free_model(model):
    return isinstance(model, dict)


def filter_models(models):
    return [m for m in (models or []) if is_free_model(m)]


def filter_free_models(models):
    return filter_models(models)


def _context_window(raw):
    if not isinstance(raw, dict):
        return None
    candidate_keys = (
        "context_window",
        "context_length",
        "max_context_length",
        "max_context_window",
        "max_tokens",
        "n_ctx",
        "context",
        "max_seq_len",
        "max_sequence_length",
    )
    for key in candidate_keys:
        if key in raw and raw.get(key) is not None:
            v = raw.get(key)
            try:
                return int(v)
            except (TypeError, ValueError):
                return v
    for nested_key in ("metadata", "info", "limits", "architecture"):
        nested = raw.get(nested_key)
        if isinstance(nested, dict):
            v = _context_window(nested)
            if v is not None:
                return v
    return None


def _display_name(raw):
    if not isinstance(raw, dict):
        return str(raw)
    for key in ("display_name", "name", "title", "id", "model"):
        v = raw.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _parse_rate_limits(headers):
    if headers is None:
        return None
    h = headers if hasattr(headers, "get") else _normalize_headers(headers)
    mapping = {
        "limit": ("x-ratelimit-limit", "x-ratelimit-limit-requests", "ratelimit-limit", "x-ratelimit-limit-tokens"),
        "remaining": ("x-ratelimit-remaining", "x-ratelimit-remaining-requests", "ratelimit-remaining", "x-ratelimit-remaining-tokens"),
        "reset": ("x-ratelimit-reset", "x-ratelimit-reset-requests", "ratelimit-reset", "x-ratelimit-reset-tokens"),
        "retry_after": ("retry-after",),
    }
    out = {}
    for canonical, keys in mapping.items():
        for k in keys:
            v = h.get(k)
            if v is not None:
                out[canonical] = v
                break
    try:
        for k, v in h.items():
            kl = str(k).lower()
            if kl.startswith("x-ratelimit") or kl.startswith("ratelimit"):
                if kl not in {kk.lower() for kk in sum(mapping.values(), ())}:
                    out[kl] = v
    except Exception:
        pass
    if not out:
        return None
    out["source"] = "headers"
    return out


_DEFAULT_RATE_LIMITS = {
    "requests_per_minute": DEFAULT_RATE_LIMIT_RPM,
    "source": "documented (NIM free tier)",
}


def normalize_model(model, rate_limits=None, headers=None, **kwargs):
    if headers is not None and rate_limits is None:
        rate_limits = _parse_rate_limits(headers)
    if not isinstance(model, dict):
        model = {"id": str(model)}
    mid = (
        model.get("id")
        or model.get("model")
        or model.get("name")
        or model.get("slug")
    )
    mid = str(mid) if mid is not None else None

    display = _display_name(model)
    if display is None:
        display = mid

    ctx = _context_window(model)

    if rate_limits is None:
        for k in ("rate_limit", "rateLimits", "rate_limits"):
            if k in kwargs and kwargs[k] is not None:
                rate_limits = kwargs[k]
                break
    if rate_limits is None:
        rate_limits = dict(_DEFAULT_RATE_LIMITS)
    else:
        rate_limits = dict(rate_limits) if isinstance(rate_limits, dict) else rate_limits

    notes = (
        "Free-tier: all models listed on integrate.api.nvidia.com are usable on "
        "the free tier subject to rate limits (~40 RPM). Free-tier eligibility "
        "is quota-based, not a listing flag (best-effort)."
    )
    if mid and not _is_chat_like(mid):
        notes += " Non-chat/embedding model kept per filter rule (include all)."

    for key in ("notes",):
        v = model.get(key)
        if v is not None and str(v).strip():
            notes = f"{v} | {notes}"

    return {
        "id": mid,
        "display_name": display,
        "context_window": ctx,
        "created": model.get("created"),
        "rate_limits": rate_limits,
        "notes": notes,
    }


def normalize(model, **kwargs):
    return normalize_model(model, **kwargs)


def normalize_models(models, rate_limits=None, headers=None):
    rl = rate_limits
    return [normalize_model(m, rate_limits=rl, headers=headers) for m in (models or [])]


def _next_url(data, current):
    if not isinstance(data, dict):
        return None
    for key in ("next", "next_url", "next_cursor", "cursor", "after"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            v = v.strip()
            if "://" in v or v.startswith("/"):
                return urljoin(current, v)
            base = current.split("?")[0]
            return f"{base}?after={v}"
        if isinstance(v, dict):
            u = v.get("url") or v.get("href") or v.get("next")
            if isinstance(u, str) and u.strip():
                return urljoin(current, u.strip())
            c = v.get("cursor") or v.get("after") or v.get("last_id")
            if isinstance(c, str) and c.strip():
                base = current.split("?")[0]
                return f"{base}?after={c.strip()}"
    if data.get("has_more"):
        last_id = data.get("last_id") or data.get("lastId") or data.get("last")
        if last_id is not None:
            base = current.split("?")[0]
            return f"{base}?after={last_id}"
    pag = data.get("pagination")
    if isinstance(pag, dict):
        u = pag.get("next") or pag.get("next_url")
        if isinstance(u, str) and u.strip():
            return urljoin(current, u.strip())
        if pag.get("has_more") and pag.get("next_cursor"):
            base = current.split("?")[0]
            return f"{base}?after={pag['next_cursor']}"
    return None


def fetch_models(api_key=None, base_url=None, timeout=DEFAULT_TIMEOUT,
                retries=DEFAULT_RETRIES, backoff=None, rate_limits=None,
                **kwargs):
    if api_key is None:
        api_key = kwargs.get("key") or kwargs.get("token") or kwargs.get("api_key")
    if not api_key:
        key, _src = resolve_api_key()
        api_key = key
    if not api_key:
        raise NvidiaAuthError(
            "Missing NVIDIA credentials: set NVIDIA_API_KEY (or NGC_API_KEY) "
            "in the environment. NIM requires authentication even for listing."
        )

    if backoff is None:
        backoff = DEFAULT_BACKOFF

    if base_url:
        url = str(base_url).rstrip("/")
        if not url.endswith("/models"):
            url = url + "/models"
    else:
        url = MODELS_URL

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "easy-hermes-poll-nvidia",
    }

    visited = set()
    all_raw = []
    parsed_rate_limits = rate_limits

    base_origin_url = url
    _backoff = tuple(backoff[:max(0, int(retries) - 1)]) if isinstance(backoff, (list, tuple)) else backoff
    while url:
        if url in visited:
            break
        visited.add(url)

        if poll_base is not None:
            data, page_headers, err = poll_base.http_get_full(
                url, headers=headers, timeout=timeout, backoff=_backoff,
                urlopen=urlopen, sleep=time.sleep)
        else:
            # fallback (should not normally happen)
            data, page_headers, err = None, None, "poll_base unavailable"
        if err is not None:
            raise NvidiaHTTPError(err)
        if not isinstance(data, (dict, list)):
            data = {}

        rl = _parse_rate_limits(page_headers or {})
        if rl is not None:
            parsed_rate_limits = rl

        if isinstance(data, dict):
            page = data.get("data")
            if page is None:
                if isinstance(data.get("models"), list):
                    page = data.get("models")
                elif isinstance(data.get("results"), list):
                    page = data.get("results")
                else:
                    page = []
        elif isinstance(data, list):
            page = data
        else:
            page = []
        if not isinstance(page, list):
            page = []
        all_raw.extend([m for m in page if isinstance(m, dict)])

        nxt = _next_url(data, url) if isinstance(data, dict) else None
        url = safe_url.safe_next(nxt, base_origin_url) if nxt else None
        if url and url in visited:
            url = None

    filtered = filter_models(all_raw)

    rl_for_models = parsed_rate_limits if parsed_rate_limits is not None else rate_limits
    normalized = normalize_models(filtered, rate_limits=rl_for_models)
    return normalized


def poll(*a, **kw):
    try:
        return fetch_models(*a, **kw)
    except (Exception, SystemExit):
        return []


poll_models = poll
fetch = fetch_models


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    json_mode = "--json" in argv

    def say(msg):
        stream = sys.stderr if json_mode else sys.stdout
        print(msg, file=stream)

    timeout = DEFAULT_TIMEOUT
    retries = DEFAULT_RETRIES
    base_url = None
    out_dir = None
    chat_only = False

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--timeout" and i + 1 < len(argv):
            try:
                timeout = float(argv[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if a == "--retries" and i + 1 < len(argv):
            try:
                retries = int(argv[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if a == "--base-url" and i + 1 < len(argv):
            base_url = argv[i + 1]
            i += 2
            continue
        if a.startswith("--base-url="):
            base_url = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--out" and i + 1 < len(argv):
            out_dir = argv[i + 1]
            i += 2
            continue
        if a.startswith("--out="):
            out_dir = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--output" and i + 1 < len(argv):
            out_dir = argv[i + 1]
            i += 2
            continue
        if a == "--chat-only":
            chat_only = True
            i += 1
            continue
        i += 1

    try:
        models = fetch_models(base_url=base_url, timeout=timeout, retries=retries)
    except NvidiaAuthError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(getattr(e, "code", 2) or 2)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if chat_only:
        models = [m for m in models if _is_chat_like(m.get("id"))]

    if json_mode:
        print(json.dumps(models, indent=2, ensure_ascii=False))
    else:
        say(f"Found {len(models)} model(s) on NVIDIA NIM")
        for m in models:
            say(f"  - {m['id']}  display_name={m['display_name']}  "
                f"ctx={m['context_window']}  rate_limits={m['rate_limits']}")

    if not models:
        say("No models returned (empty listing).")

    if json_mode and (out_dir is not None or True):
        # Write flat file when --json (default out/nvidia.json) unless --no-write
        write_path = None
        if "--no-write" in argv:
            write_path = None
        else:
            d = out_dir if out_dir else "out"
            write_path = Path(d) / "nvidia.json"
        if write_path is not None:
            try:
                write_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {"source": MODELS_URL, "count": len(models), "models": models}
                with open(write_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                say(f"Wrote JSON: {write_path}")
            except OSError as e:
                say(f"ERROR writing {write_path}: {e}")

    sys.exit(0)


if __name__ == "__main__":
    main()
