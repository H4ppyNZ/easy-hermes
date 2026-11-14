"""selector.probing — extracted from select-models.py

Split out of the original monolithic select-models.py.  Function bodies are
moved verbatim; only module-level path state is routed through
``selector.paths`` so it stays mutable at runtime.
"""
from __future__ import annotations

from selector import paths
from selector.paths import PROBE_MAX_TOKENS, PROBE_RETRY_MAX_TOKENS, REASONING_PRIORITY
import json
import re
import time
import urllib.error
import urllib.request
import providers


def _extract_elapsed_ms(note):
    if not note:
        return None
    matches = re.findall(r"(\d+)\s*ms", str(note))
    if not matches:
        return None
    try:
        return int(matches[-1])
    except (TypeError, ValueError):
        return None


def reasoning_score_from_note(note):
    if not note:
        return 0.0
    m = re.search(r"reasoning=(\d+)ch", str(note))
    if m:
        return 1.0 if int(m.group(1)) > 0 else 0.0
    return 0.0


def get_reasoning_efforts(model_id, base_url):
    import os
    cache_path = paths.HERMES_HOME / "cache" / "reasoning_caps.json"
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    # map base url to cache key
    keys = []
    if base_url:
        keys.append(base_url.rstrip("/") + "/models")
        keys.append(base_url.rstrip("/"))
    keys.append("https://openrouter.ai/api/v1/models")
    keys.append("https://inference-api.nousresearch.com/v1/models")
    for k in keys:
        bucket = data.get(k, {})
        caps = bucket.get("caps", {}) if isinstance(bucket, dict) else {}
        info = caps.get(model_id)
        if isinstance(info, dict):
            return info.get("supported_efforts")
    return None


def probe_reasoning_levels(model_id, cfg, api_key, timeout=None):
    base = cfg.get("probe_base_url") or ""
    t = timeout if (timeout is not None and timeout > 0) else (cfg.get("probe_timeout") or 15)
    if not t or t <= 0:
        t = 15
    extra = cfg.get("probe_headers")
    # Phase 1: initial polling WITHOUT reasoning (decides pass/fail).
    print("    base probe (no reasoning)...", end=" ", flush=True)
    passed0, note0, retried0 = _openai_probe(
        model_id, base, api_key, timeout=t, extra_headers=extra,
        reasoning=None, max_tokens=PROBE_MAX_TOKENS)
    retried_overall = bool(retried0)
    print(f"{'PASS' if passed0 else 'fail'} ({note0})")
    if not passed0:
        return False, note0, retried_overall

    # Phase 2: poll each advertised reasoning level (does not un-pass the model).
    supported = get_reasoning_efforts(model_id, base)
    if supported:
        efforts = [e for e in REASONING_PRIORITY if e in supported and e != "none"]
    else:
        efforts = [e for e in REASONING_PRIORITY if e != "none"]
    if not efforts:
        efforts = ["high"]

    chosen_note = note0 + " (no reasoning effort worked)"
    for effort in efforts:
        print(f"    probing reasoning={effort}...", end=" ", flush=True)
        passed, note, retried = _openai_probe(
            model_id, base, api_key, timeout=t, extra_headers=extra,
            reasoning=effort, max_tokens=PROBE_MAX_TOKENS)
        if retried:
            retried_overall = True
        print(f"{'PASS' if passed else 'fail'} ({note})")
        if passed:
            chosen_note = f"{note} (effort={effort})"
            # store chosen effort by appending to note only; pass already secured
            break
        if passed is False and note.startswith("budget exhausted"):
            print(f"    retrying with max_tokens={PROBE_RETRY_MAX_TOKENS}...", end=" ", flush=True)
            passed2, note2, retried2 = _openai_probe(
                model_id, base, api_key, timeout=t, extra_headers=extra,
                reasoning=effort, max_tokens=PROBE_RETRY_MAX_TOKENS)
            if retried2:
                retried_overall = True
            print(f"{'PASS' if passed2 else 'fail'} ({note2})")
            if passed2:
                chosen_note = f"{note2} (effort={effort}, budget retry)"
                break
    return True, chosen_note, retried_overall


def fetch_provider(slug, cfg):
    modname = cfg["module"]
    try:
        import importlib
        mod = importlib.import_module(modname)
    except Exception as e:
        return [], f"import failed: {e}"
    fn = None
    preferred = cfg.get("fetch_fn")
    names = ([preferred] if preferred else []) + ["poll", "fetch_models", "fetch", "fetch_free_models", "get_models"]
    for name in names:
        if not name:
            continue
        fn = getattr(mod, name, None)
        if callable(fn):
            break
    if fn is None:
        return [], "no fetch function"
    key, _ = providers.get_api_key(cfg)
    try:
        try:
            raws = fn(api_key=key)
        except TypeError:
            raws = fn()
    except Exception as e:
        return [], str(e)
    if not isinstance(raws, list):
        return [], "fetch did not return a list"
    out = []
    for r in raws:
        if not isinstance(r, dict):
            continue
        mid = r.get("id") if "id" in r else (r.get("id") or r.get("name") or r.get("model") or r.get("slug"))
        if mid is None:
            continue
        if "id" not in r:
            mid = r.get("id") or r.get("name") or r.get("model") or r.get("slug")
            out.append({
                "id": str(mid) if mid is not None else None,
                "display_name": r.get("display_name") or r.get("name") or mid,
                "context_window": r.get("context_window") or r.get("context_length"),
                "rate_limits": r.get("rate_limits"),
                "notes": r.get("notes", ""),
            })
        else:
            out.append(r)
    return out, None


def _openai_probe(model_id, base_url, api_key, timeout=15, extra_headers=None, reasoning=None, max_tokens=256):
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url = url + "/v1"
    url = url + "/chat/completions"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "If I have 3 apples and eat 1, how many are left? Answer with just the number."}],
        "max_tokens": max_tokens,
    }
    if reasoning is not None:
        payload["reasoning_effort"] = reasoning
    headers = {"Content-Type": "application/json", "User-Agent": "easy-hermes/select-models"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    retried = False
    last = "no attempt"
    for delay in (0, 2, 5):
        if delay:
            retried = True
            time.sleep(delay)
        req = urllib.request.Request(url, data=__import__("json").dumps(payload).encode(), headers=headers)
        try:
            start = time.perf_counter()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            content = (msg.get("content") or "").strip()
            reasoning_text = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
            if content:
                return True, f"ok: {content[:40]} (reasoning={len(reasoning_text)}ch, {elapsed_ms}ms)", retried
            fr = choice.get("finish_reason") if isinstance(choice, dict) else None
            if reasoning_text and fr == "length":
                return False, f"budget exhausted thinking ({len(reasoning_text)}ch, {elapsed_ms}ms)", retried
            note = f"empty content (finish_reason={fr}, {elapsed_ms}ms)" if fr else f"empty content ({elapsed_ms}ms)"
            return False, note, retried
        except urllib.error.HTTPError as e:
            code = e.code
            body = ""
            try:
                body = e.read().decode(errors="replace")
            except Exception:
                pass
            low = body.lower()
            if code == 429 and ("per day" in low or "daily" in low or "limit: 0" in low.replace(" ", "")):
                return False, f"HTTP 429: daily quota exceeded (not retryable)", retried
            if code == 429 or code >= 500:
                last = f"HTTP {code} (retrying)"
                continue
            return False, f"HTTP {code}: {body[:80]}", retried
        except Exception as e:
            return False, f"{type(e).__name__}: {e}", retried
    return False, last, retried


def _gemini_probe(model_id, api_key, base_url, timeout=15):
    import re as _re
    if not isinstance(model_id, str) or not _re.fullmatch(r"[A-Za-z0-9._\-/]+", model_id):
        return False, "invalid model id", False
    mid = model_id
    if not mid.startswith("models/"):
        mid = "models/" + mid
    from urllib.parse import quote
    mid_enc = "/".join(quote(seg, safe="") for seg in mid.split("/"))
    url = f"{base_url.rstrip('/')}/{mid_enc}:generateContent"
    payload = {"contents": [{"parts": [{"text": "If I have 3 apples and eat 1, how many are left? Answer with just the number."}]}],
               "generationConfig": {"maxOutputTokens": 256}}
    retried = False
    last = "no attempt"
    for delay in (0, 5, 15):
        if delay:
            retried = True
            time.sleep(delay)
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json",
                                             "x-goog-api-key": str(api_key),
                                             "User-Agent": "easy-hermes/select-models"})
        try:
            start = time.perf_counter()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            cands = data.get("candidates") or []
            if cands:
                parts = (cands[0].get("content", {}) or {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
                if text:
                    return True, f"ok: {text[:40]} ({elapsed_ms}ms)", retried
                fr = cands[0].get("finishReason")
                if fr:
                    last = f"empty content (finishReason={fr}, {elapsed_ms}ms)"
                else:
                    last = f"empty content ({elapsed_ms}ms)"
                return False, last, retried
            pf = data.get("promptFeedback") or {}
            br = pf.get("blockReason")
            if br:
                return False, f"blocked: {br}", retried
            return False, "empty content", retried
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace")
            except Exception:
                pass
            code = e.code
            low = body.lower()
            if code == 429 and ("per day" in low or "daily" in low or "limit: 0" in low.replace(" ", "")):
                return False, "HTTP 429: daily quota exceeded (not retryable)", retried
            if code == 429 or code >= 500:
                last = f"HTTP {code}: {body[:80]}"
                continue
            return False, f"HTTP {code}: {body[:80]}", retried
        except Exception as e:
            return False, f"{type(e).__name__}: {e}", retried
    return False, last, retried


def _ollama_local_probe(model_id, base_url, timeout=60):
    try:
        import poll_ollama_local as _pol
        ok, note, retried = _pol.probe_model(model_id, _pol.openai_base(base_url), fast=False)
        return bool(ok), str(note), bool(retried)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", False


def probe(slug, cfg, model_id, api_key, timeout=None, reasoning=None):
    kind = cfg.get("probe_kind")
    if kind == "openai":
        base = cfg.get("probe_base_url") or ""
        t = timeout if (timeout is not None and timeout > 0) else (cfg.get("probe_timeout") or 15)
        if not t or t <= 0:
            t = 15
        if reasoning:
            return probe_reasoning_levels(model_id, cfg, api_key, timeout=t)
        return _openai_probe(model_id, base, api_key, timeout=t, extra_headers=cfg.get("probe_headers"))
    if kind == "gemini":
        base = cfg.get("probe_base_url") or "https://generativelanguage.googleapis.com/v1beta"
        t = timeout if (timeout is not None and timeout > 0) else (cfg.get("probe_timeout") or 15)
        if not t or t <= 0:
            t = 15
        return _gemini_probe(model_id, api_key, base, timeout=t)
    if kind == "ollama-local":
        try:
            import poll_ollama_local as _pol
            bu = _pol.resolve_base_url()
        except Exception:
            bu = "http://localhost:11434"
        return _ollama_local_probe(model_id, bu)
    return False, "unknown probe kind", False