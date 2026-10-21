#!/usr/bin/env python3
"""
Probe the (free) models served by a local Ollama daemon and fold the ones that
actually answer into Hermes' config -- as fallback providers, and optionally as
model.default.

This is the Ollama counterpart of hermes-model-selector.py (which handles the
Nous Portal free tier). It is kept SEPARATE for now and is meant to be merged
into hermes-setup.ps1 step 5 ("model selector") later. It does not touch the
Nous chain unless --apply-fallbacks is given (default on) -- see --dry-run.

All models served by a local Ollama are free (nothing leaves the machine), so
there is no promotion gate here: a model that answers the probe cleanly is
admitted. The probe is still worth running because a pulled model can be broken
(wrong quant, missing template, OOM on load) and we do not want those in the
chain.

Usage:
    python hermes-ollama-selector.py                      # probe + merge into fallbacks
    python hermes-ollama-selector.py --dry-run            # preview, write nothing
    python hermes-ollama-selector.py --base-url http://host:11434
    python hermes-ollama-selector.py --set-default        # also promote best model to default
    python hermes-ollama-selector.py --replace             # replace fallback chain (not merge)
    python hermes-ollama-selector.py --no-apply            # probe + cache only
    python hermes-ollama-selector.py --hermes-home C:\\\\Users\\\\me\\\\AppData\\\\Local\\\\hermes
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import date

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

def resolve_hermes_src(hermes_home):
    candidates = [
        hermes_home / "hermes-agent",
        Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hermes-agent",
    ]
    env = os.environ.get("HERMES_AGENT_HOME") or os.environ.get("HERMES_SRC")
    if env:
        candidates.insert(0, Path(env))
    for c in candidates:
        if c and (c / "utils.py").exists():
            return c
    return hermes_home / "hermes-agent"


LOCALAPPDATA = Path(os.environ["LOCALAPPDATA"])
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (LOCALAPPDATA / "hermes"))
CONFIG_PATH = HERMES_HOME / "config.yaml"
OLLAMA_CACHE_PATH = HERMES_HOME / "cache" / "ollama_model_cache.json"
HERMES_SRC = resolve_hermes_src(HERMES_HOME)

PROBE_MAX_TOKENS = 64
PROBE_RETRY_MAX_TOKENS = 256
RATE_LIMIT_BACKOFF = (2, 5, 10)


def load_yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        print("ERROR: PyYAML is required. Run with the Hermes venv python.")
        sys.exit(1)


def _strip_ollama_suffixes(url):
    u = url.rstrip("/")
    for suf in ("/api/tags", "/v1/models", "/api", "/v1"):
        if u.endswith(suf):
            u = u[: -len(suf)].rstrip("/")
    return u


def resolve_base_url(explicit=None):
    if explicit:
        return _strip_ollama_suffixes(explicit)
    env = os.environ.get("OLLAMA_HOST", "").strip()
    if env:
        if "://" not in env:
            env = "http://" + env
        return _strip_ollama_suffixes(env)
    # configured provider
    try:
        import yaml
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        prov = ((cfg.get("providers") or {}).get("ollama") or {})
        bu = prov.get("base_url") or prov.get("api") or prov.get("url")
        if bu:
            return _strip_ollama_suffixes(str(bu))
        mc = cfg.get("model") or {}
        if isinstance(mc, dict) and mc.get("provider") in ("ollama", "custom") and mc.get("base_url"):
            return _strip_ollama_suffixes(str(mc["base_url"]))
    except Exception:
        pass
    return "http://localhost:11434"


def fetch_models(base_url):
    """Return list of {name, size, context_length, parameter_size} from /api/tags."""
    out = []
    # try native /api/tags first
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/api/tags",
                                    headers={"User-Agent": "hermes-ollama-selector"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for m in (data.get("models") or []):
            details = m.get("details") or {}
            out.append({
                "name": m.get("name") or m.get("model"),
                "size": m.get("size"),
                "context_length": details.get("context_length") or m.get("context_length"),
                "parameter_size": details.get("parameter_size"),
                "capabilities": m.get("capabilities") or details.get("capabilities") or [],
            })
        if out:
            return out
    except Exception as e:
        print(f"  native /api/tags failed: {e}")
    # fallback: OpenAI /v1/models
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/v1/models",
                                    headers={"User-Agent": "hermes-ollama-selector"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for m in (data.get("data") or []):
            out.append({"name": m.get("id"), "size": None, "context_length": None,
                        "parameter_size": None, "capabilities": []})
    except Exception as e:
        print(f"  /v1/models failed: {e}")
    return out


def _post_chat(base_url, model_id, max_tokens, timeout=60):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model_id,
               "messages": [{"role": "user", "content": "Say 'ok' and nothing else."}],
               "max_tokens": max_tokens}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json",
                                         "User-Agent": "hermes-ollama-selector"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return ("ok", json.loads(resp.read()), resp.status)
    except urllib.error.HTTPError as e:
        return ("http_error", e.read().decode()[:200], e.code)
    except Exception as e:
        return ("error", f"{type(e).__name__}: {e}", None)


def probe_model(model_id, base_url, fast=False):
    backoff = (0,) if fast else RATE_LIMIT_BACKOFF
    retried = False
    last = "no attempt"
    for delay in (0,) + (tuple() if fast else RATE_LIMIT_BACKOFF):
        if delay:
            retried = True
            time.sleep(delay)
        status, body, code = _post_chat(base_url, model_id, PROBE_MAX_TOKENS)
        if code == 429 or (code is not None and code >= 500):
            last = f"HTTP {code} (retrying)"
            continue
        if status != "ok":
            return (False, f"HTTP {code}: {str(body)[:90]}", retried)
        try:
            _body = body if isinstance(body, dict) else {}
            choice = (_body.get("choices") or [{}])[0]
            msg = choice.get("message", {}) or {} if isinstance(choice, dict) else {}
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
            finish = choice.get("finish_reason") if isinstance(choice, dict) else None
        except (KeyError, IndexError, TypeError) as e:
            return (False, f"malformed response: {e}", retried)
        if content:
            return (True, f"finish={finish} reasoning={len(reasoning)}ch", retried)
        if finish == "length" and reasoning:
            # thinking model consumed budget on reasoning; retry with bigger budget
            v2, b2, _ = _post_chat(base_url, model_id, PROBE_RETRY_MAX_TOKENS)
            if v2 == "ok":
                try:
                    _b2 = b2 if isinstance(b2, dict) else {}
                    _choices = _b2.get("choices") or [{}]
                    m2 = (_choices[0].get("message", {}) or {}) if isinstance(_choices[0], dict) else {}
                    c2 = (m2.get("content") or "").strip()
                    if c2:
                        return (True, "passed after budget retry", True)
                except Exception:
                    pass
            return (False, f"empty content (finish={finish})", retried)
        return (False, f"empty content (finish={finish})", retried)
    return (False, last, retried)


def param_sort_key(m):
    # bigger parameter size first; unknowns last
    ps = m.get("parameter_size") or ""
    # parse like "8.2B" -> 8.2
    num = 0.0
    try:
        s = ps.strip().upper()
        if s.endswith("B"):
            num = float(s[:-1])
        elif s.endswith("M"):
            num = float(s[:-1]) / 1000.0
        else:
            num = float(s)
    except (ValueError, AttributeError):
        num = 0.0
    return (-num, m.get("name") or "")


def load_config():
    yaml = load_yaml()
    if not CONFIG_PATH.exists():
        return {}, yaml
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}), yaml


def openai_base(base_url):
    b = base_url.rstrip("/")
    if not b.endswith("/v1"):
        b = b + "/v1"
    return b


def main():
    global CONFIG_PATH, OLLAMA_CACHE_PATH, HERMES_HOME, HERMES_SRC, base_url_arg
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv
    no_apply = "--no-apply" in argv
    apply = not (dry_run or no_apply)
    set_default = "--set-default" in argv
    replace = "--replace" in argv
    fast = "--fast" in argv
    base_url_arg = None
    if "--base-url" in argv:
        base_url_arg = argv[argv.index("--base-url") + 1]
    hermes_home = None
    if "--hermes-home" in argv:
        hermes_home = argv[argv.index("--hermes-home") + 1]
        global HERMES_HOME, CONFIG_PATH, OLLAMA_CACHE_PATH, HERMES_SRC
        HERMES_HOME = Path(hermes_home)
        CONFIG_PATH = HERMES_HOME / "config.yaml"
        OLLAMA_CACHE_PATH = HERMES_HOME / "cache" / "ollama_model_cache.json"
        HERMES_SRC = resolve_hermes_src(HERMES_HOME)

    if "--config" in argv:
        CONFIG_PATH = Path(argv[argv.index("--config") + 1])
    if "--cache" in argv:
        OLLAMA_CACHE_PATH = Path(argv[argv.index("--cache") + 1])
    base_url = resolve_base_url(base_url_arg)
    oa_base = openai_base(base_url)
    print(f"Ollama endpoint: {base_url}")
    print(f"OpenAI-compat base: {oa_base}")

    print("Fetching model list...")
    models = fetch_models(base_url)
    print(f"Found {len(models)} model(s)")
    for m in models:
        print(f"  - {m['name']}  ctx={m.get('context_length')}  params={m.get('parameter_size')}")

    if not models:
        print("No models found. Is Ollama running? (ollama serve)")
        sys.exit(1)

    # probe
    print("\nProbing models...")
    passed = []
    probed_all = []
    for m in models:
        mid = m["name"]
        print(f"\n--- {mid} ---")
        ok, note, retried = probe_model(mid, oa_base, fast=fast)
        print(f"  {'PASS' if ok else 'FAIL'} ({note})")
        m["test_passed"] = ok
        m["probe_note"] = note
        m["clean_pass"] = ok and not retried
        probed_all.append(m)
        if ok:
            passed.append(m)

    passed.sort(key=param_sort_key)

    # cache
    cache = {
        "base_url": base_url,
        "probed_at": time.time(),
        "probed_date": date.today().isoformat(),
        "models": probed_all,
        "passed": [m["name"] for m in passed],
    }
    if apply or dry_run or True:
        OLLAMA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OLLAMA_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        print(f"\nSaved cache to {OLLAMA_CACHE_PATH}")

    print(f"\n{'='*70}")
    print(f"Passed models ({len(passed)}):")
    print(f"{'='*70}")
    for i, m in enumerate(passed, 1):
        print(f"  {i}. {m['name']}  (ctx={m.get('context_length')}, params={m.get('parameter_size')})")

    if not passed:
        print("\nNo models passed probing -- config untouched.")
        sys.exit(0 if not apply else 0)

    # build entries
    entries = [{"provider": "custom", "model": m["name"], "base_url": oa_base} for m in passed]

    if set_default:
        # don't put the primary in its own fallback chain
        entries = [e for e in entries if e["model"] != passed[0]["name"]]

    if dry_run:
        print(f"\n{'='*70}")
        print("DRY RUN -- nothing written. Would merge these into fallback_providers:")
        for e in entries:
            print(f"  {e['model']}  ({e['base_url']})")
        if set_default:
            print(f"\n  model.default -> {passed[0]['name']} (provider custom)")
        return

    if not apply:
        print("\n--no-apply: skipping config write.")
        return

    # write config
    yaml = load_yaml()
    cfg, _ = load_config()
    if not isinstance(cfg, dict):
        cfg = {}

    # fallback merge
    existing_chain = cfg.get("fallback_providers")
    if not isinstance(existing_chain, list):
        existing_chain = []

    # If promoting a model to default, drop it from any existing chain entries.
    if set_default:
        chosen_name = passed[0]["name"]
        existing_chain = [e for e in existing_chain
                         if not (isinstance(e, dict) and str(e.get("model", "")) == chosen_name)]

    def entry_key(e):
        return (str(e.get("provider", "")), str(e.get("model", "")), str(e.get("base_url", "")))

    existing_keys = {entry_key(e) for e in existing_chain if isinstance(e, dict)}
    new_entries = [e for e in entries if entry_key(e) not in existing_keys]

    if replace:
        merged = entries
    else:
        merged = list(existing_chain) + new_entries

    cfg["fallback_providers"] = merged
    print(f"\nfallback_providers: {len(existing_chain)} existing + {len(new_entries)} new = {len(merged)} total")

    if set_default:
        mcfg = cfg.setdefault("model", {})
        if not isinstance(mcfg, dict):
            mcfg = {"default": mcfg} if isinstance(mcfg, str) else {}
            cfg["model"] = mcfg
        mcfg["default"] = passed[0]["name"]
        mcfg["provider"] = "custom"
        mcfg["base_url"] = oa_base
        # clear stale creds
        for k in ("api_key", "key_env"):
            mcfg.pop(k, None)
        print(f"model.default -> {passed[0]['name']} (provider custom)")

    # write preserving comments if possible
    HERMES_SRC_STR = str(HERMES_SRC)
    if HERMES_SRC_STR not in sys.path:
        sys.path.insert(0, HERMES_SRC_STR)
    try:
        from utils import atomic_roundtrip_yaml_save
        atomic_roundtrip_yaml_save(CONFIG_PATH, cfg)
        print(f"Updated {CONFIG_PATH}")
    except Exception as e:
        print(f"  ruamel writer unavailable ({e}); falling back to yaml.dump (comments lost)")
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        print(f"Updated {CONFIG_PATH}")

    print("Done. Run 'hermes fallback list' to verify.")


if __name__ == "__main__":
    main()
