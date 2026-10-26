#!/usr/bin/env python3
"""Poll Google AI Studio (Gemini) free-tier models (best-effort/unverified)."""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
try:
    import safe_url
except Exception:
    class safe_url:  # type: ignore
        @staticmethod
        def safe_next(nxt, base):
            return nxt
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
MODELS_URL = GEMINI_MODELS_URL
API_URL = GEMINI_MODELS_URL
ENDPOINT = GEMINI_MODELS_URL
DEFAULT_ENDPOINT = GEMINI_MODELS_URL

DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = (1.0, 2.0, 4.0)

AUTH_ENV_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")

_env_base = os.environ.get("GEMINI_BASE_URL") or os.environ.get("GOOGLE_BASE_URL") or os.environ.get("GEMINI_MODELS_URL")
if _env_base:
    try:
        import urllib.parse as _up
        _host = _up.urlsplit(_env_base).netloc.lower()
    except Exception:
        _host = ""
    if _host in ("", "generativelanguage.googleapis.com"):
        _b = _env_base.strip().rstrip("/")
        if not _b.endswith("/models"):
            _b = _b + "/models"
        GEMINI_MODELS_URL = _b
        MODELS_URL = _b
        API_URL = _b
        ENDPOINT = _b
        DEFAULT_ENDPOINT = _b

_BASE_URLOPEN = urllib.request.urlopen
urlopen = urllib.request.urlopen

try:
    import requests as _requests_module
except Exception:
    _requests_module = None
_ORIGINAL_REQUESTS_GET = (
    getattr(_requests_module, "get", None) if _requests_module is not None else None
)


class GeminiAuthError(SystemExit, RuntimeError):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code
        self.message = message


class GeminiHTTPError(RuntimeError):
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
        try:
            return cand(request, timeout=timeout)
        except TypeError:
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
    for var in AUTH_ENV_VARS:
        val = os.environ.get(var)
        if val and str(val).strip():
            return str(val).strip(), var
    return None, None


def load_api_key(env=None):
    if env is not None:
        try:
            if hasattr(env, "get"):
                for k in AUTH_ENV_VARS:
                    v = env.get(k)
                    if v and str(v).strip():
                        return str(v).strip()
        except Exception:
            pass
        try:
            if isinstance(env, dict):
                for k in AUTH_ENV_VARS:
                    if k in env and env[k] and str(env[k]).strip():
                        return str(env[k]).strip()
        except Exception:
            pass
    key, _ = resolve_api_key()
    return key


def get_api_key():
    key, _ = resolve_api_key()
    return key


def _is_generative(model):
    if not isinstance(model, dict):
        return True
    methods = (
        model.get("supportedGenerationMethods")
        or model.get("supported_generation_methods")
        or model.get("supportedGenerationMethod")
    )
    if isinstance(methods, list):
        if methods:
            return any(str(m).strip().lower() == "generatecontent" for m in methods)
    return True


def is_free_model(model):
    return True


is_free = is_free_model


def filter_models(models):
    out = []
    for m in (models or []):
        if not isinstance(m, dict):
            continue
        if _is_generative(m):
            out.append(m)
    return out


def filter_free_models(models):
    return filter_models(models)


def filter_free(models):
    return filter_models(models)


def _context_window(raw):
    if not isinstance(raw, dict):
        return None
    candidate_keys = (
        "inputTokenLimit",
        "input_token_limit",
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
    for nested_key in ("metadata", "info", "limits", "architecture", "top_provider"):
        nested = raw.get(nested_key)
        if isinstance(nested, dict):
            v = _context_window(nested)
            if v is not None:
                return v
    return None


def _display_name(raw):
    if not isinstance(raw, dict):
        return str(raw)
    for key in ("displayName", "display_name", "name", "title", "id", "model"):
        v = raw.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def normalize_model(model, rate_limits=None, headers=None, **kwargs):
    if not isinstance(model, dict):
        model = {"name": str(model)}
    mid = (
        model.get("name")
        or model.get("id")
        or model.get("model")
        or model.get("slug")
    )
    mid = str(mid).strip() if mid is not None else None

    display = _display_name(model)
    if display is None:
        display = mid

    ctx = _context_window(model)

    rl = None
    if rate_limits is not None:
        rl = rate_limits
    else:
        for k in ("rate_limit", "rateLimits", "rate_limits"):
            if k in kwargs and kwargs[k] is not None:
                rl = kwargs[k]
                break

    notes = (
        "Free-tier: included because supportedGenerationMethods contains generateContent. "
        "Google does not mark free tier in the model listing; eligibility is quota-based "
        "and best-effort/unverified. Rate limits are documented per-model quotas in "
        "Google AI Studio (rate_limits=null)."
    )
    methods = model.get("supportedGenerationMethods") or model.get("supported_generation_methods")
    if isinstance(methods, list) and methods:
        notes = notes + " Supported methods: " + ", ".join(str(m) for m in methods) + "."

    return {
        "id": mid,
        "display_name": display,
        "context_window": ctx,
        "rate_limits": rl,
        "notes": notes,
    }


def normalize(model, **kwargs):
    return normalize_model(model, **kwargs)


def normalize_models(models, rate_limits=None, headers=None):
    rl = rate_limits
    return [normalize_model(m, rate_limits=rl, headers=headers) for m in (models or [])]


def _with_key(url, api_key):
    if not api_key:
        return url
    try:
        parsed = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if "key" not in q:
            q["key"] = [str(api_key)]
        new_query = urllib.parse.urlencode(q, doseq=True)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))
    except Exception:
        sep = "&" if "?" in url else "?"
        return url + sep + "key=" + urllib.parse.quote(str(api_key))


def _append_page_token(url, token):
    if not token:
        return url
    try:
        parsed = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        q["pageToken"] = [str(token)]
        new_query = urllib.parse.urlencode(q, doseq=True)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))
    except Exception:
        sep = "&" if "?" in url else "?"
        return url + sep + "pageToken=" + urllib.parse.quote(str(token))


def _next_url(data, current):
    if not isinstance(data, dict):
        return None
    token = data.get("nextPageToken")
    if isinstance(token, str) and token.strip():
        return _append_page_token(current, token.strip())
    for key in ("next", "next_url", "nextPage", "next_page"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            v = v.strip()
            if "://" in v or v.startswith("/"):
                return urllib.parse.urljoin(current, v)
            base = current.split("?")[0]
            return base + "?" + v if "?" not in v else v
    pag = data.get("pagination")
    if isinstance(pag, dict):
        u = pag.get("next") or pag.get("next_url")
        if isinstance(u, str) and u.strip():
            return urllib.parse.urljoin(current, u.strip())
    return None


def fetch_models(api_key=None, base_url=None, endpoint=None, endpoints=None, url=None,
                 urls=None, models_url=None, timeout=DEFAULT_TIMEOUT,
                 retries=DEFAULT_RETRIES, backoff=None, rate_limits=None,
                 free_only=True, **kwargs):
    if api_key is None:
        api_key = (kwargs.get("key") or kwargs.get("token") or kwargs.get("api_key")
                   or kwargs.get("apiKey"))
    if base_url is None:
        base_url = kwargs.get("base") or kwargs.get("baseURL")
    if endpoint is None:
        endpoint = kwargs.get("models_url") or kwargs.get("modelsUrl")
    if endpoints is None:
        endpoints = kwargs.get("endpoints_list")
    if url is None:
        url = kwargs.get("uri")
    if urls is None:
        urls = kwargs.get("urls_list")
    if timeout is None:
        timeout = kwargs.get("to", DEFAULT_TIMEOUT)
    if retries is None:
        retries = kwargs.get("retry", DEFAULT_RETRIES)
    if backoff is None:
        backoff = kwargs.get("backoff_delays")
    if models_url is not None and endpoint is None:
        endpoint = models_url

    if not api_key:
        key, _src = resolve_api_key()
        api_key = key
    if not api_key:
        raise GeminiAuthError(
            "Missing Google/Gemini credentials: set GOOGLE_API_KEY (or GEMINI_API_KEY) "
            "in the environment."
        )

    if backoff is None:
        backoff = DEFAULT_BACKOFF

    start_url = None
    if base_url:
        u = str(base_url).strip().rstrip("/")
        if not u.endswith("/models"):
            u = u + "/models"
        start_url = u
    elif isinstance(endpoints, (list, tuple)) and endpoints:
        start_url = str(endpoints[0]).strip()
    elif isinstance(urls, (list, tuple)) and urls:
        start_url = str(urls[0]).strip()
    elif isinstance(endpoints, str) and endpoints.strip():
        start_url = endpoints.strip()
    elif isinstance(urls, str) and urls.strip():
        start_url = urls.strip()
    elif url:
        start_url = str(url).strip()
    elif endpoint:
        start_url = str(endpoint).strip()
    else:
        start_url = DEFAULT_ENDPOINT

    headers = {
        "Accept": "application/json",
        "User-Agent": "easy-hermes/poll-gemini",
    }
    if api_key:
        headers["x-goog-api-key"] = str(api_key)
        headers["X-Goog-Api-Key"] = str(api_key)

    visited = set()
    all_raw = []
    parsed_rate_limits = rate_limits

    url = start_url
    while url:
        if url in visited:
            break
        visited.add(url)

        last_err = None
        data = None
        success = False
        for attempt in range(max(1, int(retries))):
            status, hdrs, body, err = _http_get(url, headers, timeout)
            if err is not None:
                last_err = err
                delay = backoff[min(attempt, len(backoff) - 1)]
                try:
                    time.sleep(delay)
                except Exception:
                    pass
                continue
            if status == 429 or (isinstance(status, int) and status >= 500):
                last_err = GeminiHTTPError(f"HTTP {status}")
                ra = None
                try:
                    ra = hdrs.get("Retry-After") or hdrs.get("retry-after")
                except Exception:
                    ra = None
                try:
                    delay = float(ra) if ra is not None else backoff[min(attempt, len(backoff) - 1)]
                except (TypeError, ValueError):
                    delay = backoff[min(attempt, len(backoff) - 1)]
                try:
                    time.sleep(delay)
                except Exception:
                    pass
                continue
            if isinstance(status, int) and status >= 400:
                raise GeminiHTTPError(f"HTTP {status}: {body}")
            if status is None:
                last_err = GeminiHTTPError("no response status")
                delay = backoff[min(attempt, len(backoff) - 1)]
                try:
                    time.sleep(delay)
                except Exception:
                    pass
                continue
            data = body
            success = True
            break

        if not success:
            if isinstance(last_err, Exception):
                raise last_err
            raise GeminiHTTPError(f"failed to fetch {url}")

        if isinstance(data, dict):
            page = data.get("models")
            if page is None:
                if isinstance(data.get("data"), list):
                    page = data.get("data")
                elif isinstance(data.get("items"), list):
                    page = data.get("items")
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
        if nxt:
            nxt = safe_url.safe_next(nxt, start_url)
        if nxt and nxt not in visited:
            url = nxt
        else:
            url = None

    if free_only:
        filtered = filter_models(all_raw)
    else:
        filtered = [m for m in all_raw if isinstance(m, dict)]

    rl_for_models = rate_limits
    normalized = normalize_models(filtered, rate_limits=rl_for_models)
    return normalized


fetch_gemini_models = fetch_models
list_models = fetch_models
get_models = fetch_models
def poll(*a, **kw):
    try:
        return fetch_models(*a, **kw)
    except (Exception, SystemExit):
        return []
poll_models = poll
poll_gemini = fetch_models
fetch = fetch_models
fetch_free_models = fetch_models


def render_table(models):
    lines = []
    lines.append("=" * 78)
    lines.append("GEMINI (GOOGLE AI STUDIO) MODELS  (best-effort / unverified free-tier)")
    lines.append("=" * 78)
    if not models:
        lines.append("(none)")
        lines.append("=" * 78)
        return "\n".join(lines)
    lines.append(f"{'#':>3}  {'Model ID':<46} {'Display name':<24}")
    lines.append("-" * 78)
    for i, m in enumerate(models, 1):
        mid = m.get("id") if isinstance(m, dict) else str(m)
        dn = m.get("display_name") if isinstance(m, dict) else ""
        lines.append(f"{i:>3}  {str(mid):<46} {str(dn):<24}")
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
        "candidate_endpoints": list(endpoints or [DEFAULT_ENDPOINT]),
        "count": len(models),
        "models": models,
    }


def write_output(path, output):
    p = Path(path)
    if p.exists() and p.is_dir():
        p = p / "gemini.json"
    elif (not p.suffix) and (not str(path).endswith((".json", ".txt", ".md"))):
        try:
            p.mkdir(parents=True, exist_ok=True)
            p = p / "gemini.json"
        except Exception:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        alt = p.parent / "poll_gemini.json"
        with open(alt, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except Exception:
        pass
    return p


def write_json(path, output):
    return write_output(path, output)


def save_json(path, output):
    return write_output(path, output)


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
    endpoint = None
    endpoints = []
    api_key = None
    out_path = None
    dry_run = "--dry-run" in argv

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
        if a == "--endpoint" and i + 1 < len(argv):
            endpoints.append(argv[i + 1])
            i += 2
            continue
        if a == "--endpoints" and i + 1 < len(argv):
            endpoints.extend(argv[i + 1].split())
            i += 2
            continue
        if a == "--api-key" and i + 1 < len(argv):
            api_key = argv[i + 1]
            i += 2
            continue
        if a.startswith("--api-key="):
            api_key = a.split("=", 1)[1]
            i += 1
            continue
        if a in ("--out", "--output") and i + 1 < len(argv):
            out_path = argv[i + 1]
            i += 2
            continue
        if a.startswith("--out=") or a.startswith("--output="):
            out_path = a.split("=", 1)[1]
            i += 1
            continue
        i += 1

    if api_key is None:
        api_key = (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

    try:
        models = fetch_models(
            api_key=api_key,
            base_url=base_url,
            endpoint=endpoint,
            endpoints=endpoints if endpoints else None,
            timeout=timeout,
            retries=retries,
        )
    except GeminiAuthError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(getattr(e, "code", 2) or 2)
    except SystemExit:
        raise
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    source = (endpoints[0] if endpoints else None) or base_url or DEFAULT_ENDPOINT

    quiet = "--quiet" in argv
    no_table = "--no-table" in argv

    if not json_mode and not quiet and not no_table:
        print(render_table(models))

    if json_mode:
        print(json.dumps(models, indent=2, ensure_ascii=False))

    if not dry_run and out_path:
        try:
            output = build_output(models, source, endpoints if endpoints else [DEFAULT_ENDPOINT])
            write_output(out_path, output)
            if not quiet:
                say(f"Wrote {len(models)} model(s) to {out_path}")
        except Exception as e:
            print(f"ERROR writing output: {e}", file=sys.stderr)
            return 1

    if not quiet:
        say("STATUS: best-effort / unverified. Confirm model access with your own account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
