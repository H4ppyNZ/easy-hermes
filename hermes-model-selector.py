#!/usr/bin/env python3
"""
Pull free models from Nous Portal, validate them, and update config.yaml.

Adds a PROMOTION GATE on top of update_fallbacks_v3.py: the newest model no longer
takes model.default automatically. It has to earn it by passing cleanly on several
consecutive runs first. The incumbent default keeps the slot until a challenger
qualifies -- that is the "reliable + bleeding edge" balance: reliability is the
entry ticket, recency decides the winner among those admitted.

Process:
1. Read free models from endpoint_model_metadata.json cache
2. Release dates: OpenRouter bulk API -> OpenRouter web -> HuggingFace -> hardcoded
3. Probe each model with a realistic token budget; record whether the pass was CLEAN
4. Update the non-expiring promotion history (consecutive clean passes per model)
5. Sort by release date (newest first)
6. GATE: promote the newest model that has >= PROMOTION_MARGIN consecutive clean passes
7. Write config.yaml through Hermes' comment-preserving writer

CLEAN PASS definition (the gate's unit of evidence)
---------------------------------------------------
A pass is CLEAN only when the model answered on the FIRST reasoning effort tried,
with no 429/5xx backoff retry consumed, and no token-budget retry. Anything else
is an unclean pass: good enough to stay in the fallback chain, not good enough to
be promoted. Rationale (measured): inclusionai/ling-3.1-flash passed at effort=high
only after a 429 retry, then failed 6/6 on re-probe minutes later. A single pass is
one sample from a distribution that includes 429, so the gate counts CLEAN passes
specifically.

WHY A SEPARATE HISTORY FILE
---------------------------
CACHE_TTL is 20h and this job runs every 24h, so fallback_model_cache.json is
ALWAYS stale on the nightly run and every probe result is discarded by design.
"Passed N nights running" cannot live there. promotion_history.json is never
TTL'd; it is pruned by age instead (HISTORY_PRUNE_DAYS).

Usage:
    python update_fallbacks.py                     # Full run
    python update_fallbacks.py --dry-run           # Show gate state, change nothing
    python update_fallbacks.py --show-history      # Print the history table and exit
    python update_fallbacks.py --fast              # Skip 429 backoff sleeps
    python update_fallbacks.py --promotion-margin 3
    python update_fallbacks.py --hysteresis-days 7
    python update_fallbacks.py --no-promote        # Only refresh the chain
    python update_fallbacks.py --default-effort medium
    python update_fallbacks.py --hermes-home C:\\Users\\me\\AppData\\Local\\hermes
    python update_fallbacks.py --config PATH --cache PATH --history PATH
"""

import yaml
import os
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, date

# ── Console encoding ──
# This script prints box-drawing characters (U+2500/U+2550). On Windows, when stdout is a
# pipe rather than a console -- exactly what happens under a scheduled task, or under
# PowerShell's `2>&1 | Out-String` -- Python picks the ANSI code page (cp1252) and dies with
# UnicodeEncodeError at the first banner. Reconfiguring here makes the script self-contained
# instead of depending on PYTHONUTF8 being exported by whoever launches it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # non-reconfigurable stream (e.g. already wrapped) -- not fatal

# ── Paths ──
# HERMES_HOME, not %LOCALAPPDATA%\hermes directly. The nightly task runs as SYSTEM, whose
# LOCALAPPDATA resolves to C:\Windows\System32\config\systemprofile\AppData\Local\hermes --
# a directory with no config.yaml and no auth.json. Deriving paths from LOCALAPPDATA made
# every SYSTEM-principal run die at "Loading auth credentials". HERMES_HOME is the documented
# override and is also what a non-default profile sets, so honour it first.
LOCALAPPDATA = Path(os.environ["LOCALAPPDATA"])
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (LOCALAPPDATA / "hermes"))
CONFIG_PATH = HERMES_HOME / "config.yaml"
AUTH_PATH = HERMES_HOME / "auth.json"
META_CACHE_PATH = HERMES_HOME / "cache" / "endpoint_model_metadata.json"
REASONING_CAPS_PATH = HERMES_HOME / "cache" / "reasoning_caps.json"
FALLBACK_CACHE_PATH = HERMES_HOME / "cache" / "fallback_model_cache.json"
PROMOTION_HISTORY_PATH = HERMES_HOME / "cache" / "promotion_history.json"
HERMES_SRC = HERMES_HOME / "hermes-agent"

# ── Cache TTL (seconds) — re-probe models at most once per day ──
CACHE_TTL = 72000  # 20 hours

# Bump when the probe harness changes, so results from an older harness are re-measured.
PROBE_VERSION = 3

# ── Gate parameters ──
PROMOTION_MARGIN = 2      # consecutive CLEAN passes required before a model may hold the default
HYSTERESIS_DAYS = 7       # challenger must be this many days newer to displace a HEALTHY incumbent
HISTORY_PRUNE_DAYS = 30   # drop history for models unseen this long
EMERGENCY_PROMOTE = True  # if the incumbent is dead, promote the best available even below margin

# ── Probe budget ──
PROBE_MAX_TOKENS = 2000
PROBE_RETRY_MAX_TOKENS = 8000
RATE_LIMIT_BACKOFF = (5, 15, 30)

DEFAULT_REASONING = "max"
REASONING_PRIORITY = ["max", "xhigh", "high", "medium", "low", "minimal", "none"]


# ══════════════════════════════════════════════════════════════════════════════
# Auth / caches
# ══════════════════════════════════════════════════════════════════════════════

class AuthError(RuntimeError):
    """auth.json is missing, unreadable, or has no usable Nous access token.

    Raised by load_auth() so every failure mode reports its own cause instead of
    surfacing as a bare traceback or as a confusing 401 much later, mid-probe.
    """


def load_auth() -> dict:
    """Load the Nous credentials from auth.json, failing with a specific reason.

    Returns the ``providers.nous`` dict. Raises AuthError -- never returns a silent
    ``{}`` -- for every state measured on this host:

      * file absent              -> previously an uncaught FileNotFoundError
      * malformed JSON           -> previously an uncaught JSONDecodeError
      * ``providers`` not a dict -> previously an uncaught AttributeError
      * ``nous`` not a dict      -> previously an uncaught AttributeError
      * no ``providers`` key     -> previously a silent {}
      * no ``nous`` entry        -> previously a silent {}
      * no/blank access_token    -> previously caught one step later, in main()
    """
    if not AUTH_PATH.exists():
        raise AuthError(f"auth.json not found at {AUTH_PATH}")

    try:
        with open(AUTH_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise AuthError(f"auth.json at {AUTH_PATH} is not valid JSON: {e}") from e
    except OSError as e:
        raise AuthError(f"auth.json at {AUTH_PATH} could not be read: {e}") from e

    if not isinstance(data, dict):
        raise AuthError(f"auth.json at {AUTH_PATH} is not a JSON object "
                        f"(got {type(data).__name__})")

    providers = data.get("providers")
    if not isinstance(providers, dict):
        raise AuthError(f"auth.json at {AUTH_PATH} has no 'providers' object")

    nous = providers.get("nous")
    if not isinstance(nous, dict):
        raise AuthError(f"auth.json at {AUTH_PATH} has no 'providers.nous' entry")

    if not nous.get("access_token"):
        raise AuthError(f"auth.json at {AUTH_PATH} has no Nous access token. "
                        f"Run 'hermes auth add nous' first.")

    return nous


def load_fallback_cache() -> dict:
    """Probe results. {} if missing, stale, or written by an older probe harness."""
    if not FALLBACK_CACHE_PATH.exists():
        return {}
    try:
        with open(FALLBACK_CACHE_PATH) as f:
            cache = json.load(f)
        if cache.get("_probe_version") != PROBE_VERSION:
            print(f"  Probe cache is v{cache.get('_probe_version')} (now v{PROBE_VERSION}) — re-probing")
            return {}
        cached_at = cache.get("_cached_at", 0)
        if time.time() - cached_at > CACHE_TTL:
            print(f"  Probe cache stale ({int((time.time() - cached_at) / 3600)}h old) — re-probing")
            return {}
        return cache
    except Exception:
        return {}


def save_fallback_cache(cache: dict) -> None:
    cache["_cached_at"] = time.time()
    cache["_probe_version"] = PROBE_VERSION
    FALLBACK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FALLBACK_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)
    print(f"  Saved probe cache to {FALLBACK_CACHE_PATH}")


# ── Promotion history (NEVER TTL'd) ───────────────────────────────────────────

def load_history() -> dict:
    """Load the cross-run promotion history. Deliberately has no TTL."""
    if not PROMOTION_HISTORY_PATH.exists():
        return {}
    try:
        with open(PROMOTION_HISTORY_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"  WARNING: promotion history unreadable ({e}); starting fresh")
        return {}


def save_history(history: dict) -> None:
    PROMOTION_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROMOTION_HISTORY_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(history, f, indent=2, sort_keys=True)
    os.replace(tmp, PROMOTION_HISTORY_PATH)   # atomic: never leave a half-written history
    print(f"  Saved promotion history to {PROMOTION_HISTORY_PATH}")


def _blank_record(today: str) -> dict:
    return {
        "first_seen": today,
        "last_seen": today,
        "last_pass": None,
        "last_clean": None,
        "last_fail": None,
        "last_fail_note": None,
        "consecutive_clean_passes": 0,
        "total_runs": 0,
        "clean_runs": 0,
        "unclean_runs": 0,
        "failed_runs": 0,
        "missed_runs": 0,
        "promotion_count": 0,
        "promoted_at": None,
    }


def update_history(history: dict, probed: list, today: str) -> None:
    """Fold this run's probe outcomes into the history.

    One increment per model per DAY, so a manual re-run cannot inflate the count.
    An unclean pass resets the streak to 0 -- that is the whole point of the gate.
    """
    seen = set()
    for m in probed:
        mid = m["model"]
        seen.add(mid)
        rec = history.setdefault(mid, _blank_record(today))
        for k, v in _blank_record(today).items():      # forward-compat: add new fields
            rec.setdefault(k, v)
        rec["last_seen"] = today
        rec["total_runs"] += 1

        if m.get("test_passed"):
            rec["last_pass"] = today
            if m.get("clean_pass"):
                rec["clean_runs"] += 1
                if rec.get("last_clean") != today:     # once per day only
                    rec["consecutive_clean_passes"] += 1
                    rec["last_clean"] = today
            else:
                rec["unclean_runs"] += 1
                rec["consecutive_clean_passes"] = 0
                rec["last_clean"] = rec.get("last_clean")
        else:
            rec["failed_runs"] += 1
            rec["consecutive_clean_passes"] = 0
            rec["last_fail"] = today
            rec["last_fail_note"] = (m.get("fail_note") or "")[:120]

    # A model absent from this run breaks its streak: "consecutive" means consecutive.
    for mid, rec in history.items():
        if mid not in seen:
            if rec.get("consecutive_clean_passes"):
                rec["consecutive_clean_passes"] = 0
            rec["missed_runs"] = rec.get("missed_runs", 0) + 1


def prune_history(history: dict, today: str) -> list:
    """Drop entries unseen for HISTORY_PRUNE_DAYS. Returns the removed ids."""
    cutoff = date.fromisoformat(today).toordinal() - HISTORY_PRUNE_DAYS
    removed = []
    for mid in list(history):
        last = (history[mid].get("last_seen") or "")[:10]
        try:
            if date.fromisoformat(last).toordinal() < cutoff:
                removed.append(mid)
                del history[mid]
        except ValueError:
            removed.append(mid)      # unparseable date: treat as stale
            del history[mid]
    return removed


def show_history(history: dict) -> None:
    if not history:
        print("Promotion history is empty (no runs recorded yet).")
        return
    print(f"\n{'='*104}")
    print("PROMOTION HISTORY  (consecutive_clean_passes is what the gate reads)")
    print(f"{'='*104}")
    print(f"  {'model':<42} {'streak':>6} {'runs':>5} {'clean':>6} {'uncln':>6} "
          f"{'fail':>5} {'miss':>5} {'prom':>5}  last_clean")
    print(f"  {'-'*100}")
    for mid in sorted(history, key=lambda k: (-history[k].get("consecutive_clean_passes", 0), k)):
        r = history[mid]
        print(f"  {mid:<42} {r.get('consecutive_clean_passes', 0):>6} "
              f"{r.get('total_runs', 0):>5} {r.get('clean_runs', 0):>6} "
              f"{r.get('unclean_runs', 0):>6} {r.get('failed_runs', 0):>5} "
              f"{r.get('missed_runs', 0):>5} {r.get('promotion_count', 0):>5}  "
              f"{r.get('last_clean') or '-'}")
    print(f"{'='*104}\n")


# ══════════════════════════════════════════════════════════════════════════════
# Release dates
# ══════════════════════════════════════════════════════════════════════════════

_OR_MAP = None


def get_openrouter_map() -> dict:
    """Fetch the OpenRouter list ONCE, indexed id -> created (unix ts)."""
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
    import re
    try:
        req = urllib.request.Request(f"https://openrouter.ai/{model_id.replace(':free','')}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        match = re.search(r'RELEASED\s*([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})', html)
        if match:
            dt = datetime.strptime(match.group(1), "%b %d, %Y")
            return (dt.strftime("%Y-%m-%d"), "openrouter_web")
    except Exception:
        pass
    return None


def get_hf_release_date(model_id: str):
    """Last resort: HF repo creation date (NOT the model release date)."""
    try:
        req = urllib.request.Request(f"https://huggingface.co/api/models/{model_id.replace(':free','')}",
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
    "stepfun/step-3.7-flash:free": "2026-05-28",
}


def get_release_date(model_id: str):
    """OpenRouter API -> OpenRouter web -> HuggingFace -> hardcoded -> None."""
    for fn in (get_openrouter_release_date, get_openrouter_web_release_date, get_hf_release_date):
        if (result := fn(model_id)):
            return result
    if model_id in HARDCODED_DATES:
        return (HARDCODED_DATES[model_id], "hardcoded")
    return None


def get_free_models_from_cache() -> list:
    with open(META_CACHE_PATH) as f:
        data = json.load(f)
    nous_models = data["https://inference-api.nousresearch.com/v1"]["models"]
    seen_names, free_models = set(), []
    for model_id, info in nous_models.items():
        pricing = info.get("pricing", {})
        try:
            if float(pricing.get("prompt", "1")) != 0 or float(pricing.get("completion", "1")) != 0:
                continue
        except (TypeError, ValueError):
            continue
        name = info.get("name", model_id)
        if name in seen_names:
            continue
        seen_names.add(name)
        free_models.append({"provider": "nous", "model": model_id, "name": name,
                            "context_length": info.get("context_length", 0)})
    return free_models


def get_reasoning_efforts(model_id: str):
    try:
        with open(REASONING_CAPS_PATH) as f:
            data = json.load(f)
    except Exception:
        return None
    caps = data.get("https://inference-api.nousresearch.com/v1/models", {}).get("caps", {})
    return (caps.get(model_id) or {}).get("supported_efforts")


# ══════════════════════════════════════════════════════════════════════════════
# Probe
# ══════════════════════════════════════════════════════════════════════════════

def _post(payload: dict, auth: dict, timeout: int = 60):
    base_url = auth.get("inference_base_url", "https://inference-api.nousresearch.com/v1")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {auth.get('access_token','')}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return ("ok", json.loads(resp.read()), resp.status)
    except urllib.error.HTTPError as e:
        return ("http_error", e.read().decode()[:200], e.code)
    except Exception as e:
        return ("error", f"{type(e).__name__}: {e}", None)


def probe_model(model_id: str, auth: dict, reasoning=None,
                max_tokens: int = PROBE_MAX_TOKENS, fast: bool = False):
    """Probe one model. Returns (verdict, note, retried).

    verdict: PASS / INCONCLUSIVE / FAIL. ``retried`` is True when a backoff retry was
    consumed to get here -- the gate treats a retried pass as UNCLEAN.
    ``reasoning=None`` OMITS the parameter (never sends a JSON null).
    """
    payload = {"model": model_id,
               "messages": [{"role": "user", "content": "Say 'ok' and nothing else."}],
               "max_tokens": max_tokens}
    if reasoning is not None:
        payload["reasoning_effort"] = reasoning

    retried = False
    backoff = (0,) if fast else (0,) + RATE_LIMIT_BACKOFF
    last = "no attempt"
    for delay in backoff:
        if delay:
            retried = True          # we only loop here because the previous attempt was 429/5xx
            time.sleep(delay)
        status, body, code = _post(payload, auth)

        if code == 429 or (code is not None and code >= 500):
            last = f"HTTP {code} (retrying)"
            continue
        if status != "ok":
            return ("FAIL", f"HTTP {code}: {str(body)[:90]}", retried)
        try:
            choice = body["choices"][0]
            msg = choice.get("message", {}) or {}
            content = (msg.get("content") or "").strip()
            rlen = len(msg.get("reasoning_content") or msg.get("reasoning") or "")
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as e:
            return ("FAIL", f"malformed response: {e}", retried)
        if content:
            return ("PASS", f"finish={finish} reasoning={rlen}ch", retried)
        if finish == "length":
            return ("INCONCLUSIVE", f"budget exhausted thinking ({rlen}ch)", retried)
        return ("FAIL", f"empty content (finish={finish})", retried)

    return ("FAIL", last, retried)


def probe_and_pick_effort(model: dict, auth: dict, fast: bool = False) -> dict:
    """Find the highest reasoning effort that yields a real answer; mark clean vs unclean."""
    model_id = model["model"]
    supported = get_reasoning_efforts(model_id)
    efforts = ([e for e in REASONING_PRIORITY if e in supported] if supported
               else ["max", "high", "medium", "low"])
    print(f"  Supported efforts: {supported or 'unknown'}")

    for idx, reasoning in enumerate(efforts):
        print(f"  probing reasoning={reasoning}...", end=" ", flush=True)
        verdict, note, retried = probe_model(model_id, auth, reasoning, fast=fast)
        print(f"{verdict} ({note})")
        if verdict == "PASS":
            model["reasoning"] = reasoning
            model["test_passed"] = True
            # CLEAN only if this was the first effort tried AND no retry was consumed.
            model["clean_pass"] = (idx == 0 and not retried)
            model["clean_note"] = note
            return model
        if verdict == "INCONCLUSIVE":
            print(f"  retrying with max_tokens={PROBE_RETRY_MAX_TOKENS}...", end=" ", flush=True)
            v2, n2, _ = probe_model(model_id, auth, reasoning,
                                    max_tokens=PROBE_RETRY_MAX_TOKENS, fast=fast)
            print(f"{v2} ({n2})")
            if v2 == "PASS":
                model["reasoning"] = reasoning
                model["test_passed"] = True
                model["clean_pass"] = False        # needed a budget retry => unclean
                model["clean_note"] = "needed budget retry"
                return model

    print("  probing with reasoning_effort omitted...", end=" ", flush=True)
    verdict, note, _ = probe_model(model_id, auth, None, fast=fast)
    print(f"{verdict} ({note})")
    if verdict == "PASS":
        model["reasoning"] = None
        model["test_passed"] = True
        model["clean_pass"] = False               # no effort worked at all => unclean
        model["clean_note"] = "no effort level worked"
        return model

    model["test_passed"] = False
    model["clean_pass"] = False
    model["fail_note"] = note
    return model


def enrich_models(models: list, auth: dict, dry_run: bool = False, fast: bool = False):
    """Attach release date + probe outcome. Returns (passed, probed_all)."""
    passed, probed_all = [], []
    cache = load_fallback_cache()
    cache_updated = False

    for model in models:
        model_id = model["model"]
        print(f"\n--- {model_id} ---")

        cached = cache.get(model_id)
        if cached and not dry_run:
            if not cached.get("test_passed"):
                print(f"  CACHED FAIL: {cached.get('fail_note', '')}")
                model.update({"test_passed": False, "clean_pass": False,
                              "reasoning": None, "fail_note": cached.get("fail_note"),
                              "release_date": cached.get("release_date")})
                probed_all.append(model)
                continue
            print(f"  CACHED: release_date={cached.get('release_date')} "
                  f"reasoning={cached.get('reasoning')} clean={cached.get('clean_pass')}")
            model.update({"release_date": cached["release_date"],
                          "date_source": cached.get("date_source", "cache"),
                          "reasoning": cached.get("reasoning"),
                          "test_passed": True,
                          "clean_pass": bool(cached.get("clean_pass"))})
            passed.append(model)
            probed_all.append(model)
            continue

        date_result = get_release_date(model_id)
        if date_result:
            model["release_date"], model["date_source"] = date_result
            print(f"  Release date: {model['release_date']} (via {model['date_source']})")
        else:
            print("  DISCARDED: no release date found")
            continue

        if dry_run:
            supported = get_reasoning_efforts(model_id)
            model["reasoning"] = (supported[0] if supported else None)
            model["test_passed"] = None
            model["clean_pass"] = None
            passed.append(model)
            continue

        probe_and_pick_effort(model, auth, fast=fast)
        cache[model_id] = {"release_date": model.get("release_date"),
                           "date_source": model.get("date_source"),
                           "reasoning": model.get("reasoning"),
                           "test_passed": bool(model.get("test_passed")),
                           "clean_pass": bool(model.get("clean_pass")),
                           "fail_note": model.get("fail_note")}
        cache_updated = True
        probed_all.append(model)
        if model.get("test_passed"):
            passed.append(model)

    if cache_updated and not dry_run:
        save_fallback_cache(cache)
    return passed, probed_all


def sort_models(models: list, sort_by: str = "release_date") -> list:
    if sort_by == "release_date":
        return sorted(models, key=lambda x: (x.get("release_date") or "1970-01-01", x["model"]),
                      reverse=True)
    if sort_by == "usage":
        usage = {"meituan/longcat-2.5-preview:free": 2, "poolside/laguna-s-2.1:free": 5,
                 "inclusionai/ling-3.0-flash-sante:free": 7}
        return sorted(models, key=lambda x: (usage.get(x["model"], 999), x["model"]))
    raise ValueError(f"Unknown sort key: {sort_by}")


# ══════════════════════════════════════════════════════════════════════════════
# THE GATE
# ══════════════════════════════════════════════════════════════════════════════

def streak(history: dict, model_id: str) -> int:
    return int(history.get(model_id, {}).get("consecutive_clean_passes", 0))


def choose_default(passed: list, probed_all: list, history: dict, current_default: str,
                   margin: int, hysteresis_days: int, emergency: bool = True):
    """Decide which model (if any) should hold model.default. Returns (model_or_None, reason).

    The incumbent is classified three ways, and the distinction matters:

      * IN SCOPE + HEALTHY   -- it is a free candidate and its streak >= margin. It keeps
                                the slot unless a challenger is newer by >= hysteresis_days
                                AND qualified.
      * IN SCOPE + DEAD      -- it is a free candidate that FAILED probing this run. This is
                                the only case that justifies the emergency path.
      * OUT OF SCOPE         -- it is not a free candidate at all (e.g. a paid/portal model
                                the operator chose deliberately, like stealth/space-bunny-alpha).
                                It is NOT dead -- it is simply outside this script's remit.
                                Never emergency-promote over it; wait for a challenger to
                                actually earn `margin` clean passes. Without this distinction
                                the emergency path fires every single night and the gate is
                                decorative.
    """
    if not passed:
        return None, "no models passed probing — leaving model.default unchanged"

    by_date = sorted(passed,
                     key=lambda m: (m.get("release_date") or "1970-01-01",
                                    streak(history, m["model"]), m["model"]),
                     reverse=True)

    probed_ids = {m["model"] for m in probed_all}
    passed_ids = {m["model"] for m in passed}
    incumbent_in_scope = current_default in probed_ids
    incumbent_passed = current_default in passed_ids
    incumbent_dead = incumbent_in_scope and not incumbent_passed
    incumbent_streak = streak(history, current_default)
    incumbent_healthy = incumbent_in_scope and incumbent_passed and incumbent_streak >= margin

    eligible = [m for m in by_date if streak(history, m["model"]) >= margin]

    if incumbent_in_scope:
        inc_state = "healthy" if incumbent_healthy else ("DEAD (failed probing)" if incumbent_dead
                                                         else f"in scope, streak {incumbent_streak}")
    else:
        inc_state = "out of scope (not a free candidate)"
    print(f"\n{'─'*80}")
    print(f"PROMOTION GATE  (margin={margin} clean pass{'es' if margin != 1 else ''}, "
          f"hysteresis={hysteresis_days}d)")
    print(f"{'─'*80}")
    print(f"  incumbent: {current_default or '(none)'}  [{inc_state}]")
    print(f"  {'model':<42} {'streak':>6}  eligible")
    for m in by_date:
        s = streak(history, m["model"])
        mark = "yes" if s >= margin else f"no (needs {margin - s} more)"
        star = " <- incumbent" if m["model"] == current_default else ""
        print(f"  {m['model']:<42} {s:>6}  {mark}{star}")
    print(f"{'─'*80}")

    # ── Case 1: incumbent is genuinely dead ──
    if incumbent_dead:
        if eligible:
            winner = eligible[0]
            return winner, (f"incumbent '{current_default}' FAILED probing this run; "
                            f"promoting best eligible '{winner['model']}' "
                            f"(streak {streak(history, winner['model'])})")
        if emergency:
            winner = by_date[0]
            return winner, (f"EMERGENCY: incumbent '{current_default}' failed probing and no model "
                            f"has {margin} clean passes; promoting best available "
                            f"'{winner['model']}' (streak {streak(history, winner['model'])})")
        return None, (f"incumbent '{current_default}' failed and nothing reached {margin} clean "
                      "passes — leaving model.default unchanged")

    # ── Case 2: nothing has earned the slot yet ──
    if not eligible:
        return None, (f"no model has {margin} clean passes yet — leaving model.default "
                      f"'{current_default}' unchanged")

    # ── Case 3: a qualified challenger exists ──
    challenger = next((m for m in eligible if m["model"] != current_default), None)
    if challenger is None:
        return None, (f"'{current_default}' is the newest qualified model — unchanged")

    # A challenger must be STRICTLY NEWER than the incumbent to displace it; hysteresis_days
    # raises that bar further. Without the strict-newer floor, hysteresis_days=0 lets an
    # OLDER qualified model displace a healthy incumbent (the by_date order only guarantees
    # eligible[0] is newest, not that every later entry is newer than the incumbent).
    incumbent_entry = next((m for m in passed if m["model"] == current_default), None)
    if incumbent_entry:
        inc_date = incumbent_entry.get("release_date") or "1970-01-01"
        ch_date = challenger.get("release_date") or "1970-01-01"
        try:
            days_newer = (date.fromisoformat(ch_date) - date.fromisoformat(inc_date)).days
        except ValueError:
            days_newer = 0
        threshold = max(1, hysteresis_days)
        if days_newer < threshold:
            why = ("not newer" if days_newer <= 0
                   else f"only {days_newer}d newer (needs {threshold}d)")
            return None, (f"keeping '{current_default}': challenger '{challenger['model']}' is {why}")

    return challenger, (f"promoting '{challenger['model']}' over '{current_default}': "
                        f"streak {streak(history, challenger['model'])}"
                        + ("" if incumbent_in_scope else " (incumbent out of scope)"))


def update_config(passed: list, chosen, reason: str, dry_run: bool = False,
                  promote: bool = True, default_effort: str = "inherit",
                  current_default: str = "") -> None:
    if not passed:
        print(f"\nNo models passed — config untouched. ({reason})")
        return

    chain = [{"provider": m["provider"], "model": m["model"]} for m in passed]
    default_model = None

    if promote and chosen:
        first = chosen
        default_model = {"provider": first["provider"], "model": first["model"]}
        for k in ("base_url", "api_key", "key_env"):
            if first.get(k):
                default_model[k] = first[k]

    # The effective default is either the model we just chose, or -- when the gate decided to
    # KEEP the incumbent -- whatever is already in model.default. Both must be filtered out of
    # the chain: a chain entry identical to the primary burns a retry on the same model.
    # (Filtering only on ``chosen`` missed the keep-incumbent path, which left the incumbent in
    # the chain as entry 1 while also being model.default.)
    effective_default = (default_model or {}).get("model") or (current_default if promote else "")
    if effective_default:
        before = len(chain)
        chain = [e for e in chain if e["model"] != effective_default]
        if before != len(chain):
            print(f"  Removed '{effective_default}' from the chain (it is model.default)")

    overrides = {m["model"]: m["reasoning"] for m in passed if m.get("reasoning")}

    if default_model:
        mid = default_model["model"]
        if default_effort == "probe":
            probed = next((m.get("reasoning") for m in passed if m["model"] == mid), None)
            if probed:
                overrides[mid] = probed
            else:
                overrides.pop(mid, None)
        elif default_effort == "inherit":
            overrides.pop(mid, None)
        else:
            overrides[mid] = default_effort

    if dry_run:
        print(f"\n{'='*80}")
        print("DRY RUN — nothing written. Would set:")
        print(f"{'='*80}")
        if default_model:
            eff = overrides.get(default_model["model"])
            print(f"\n  model.default  : {default_model['model']}   (CHANGED)")
            print(f"  model.provider : {default_model['provider']}")
            print(f"  reasoning      : {eff or '(none)'}"
                  f"  <- {'explicit override' if eff else 'inherits agent.reasoning_effort'}")
        elif promote and current_default:
            print(f"\n  model.default  : {current_default}   (UNCHANGED — gate kept the incumbent)")
        else:
            print("\n  model.default  : (unchanged)")
        print(f"\n  fallback_providers ({len(chain)}):")
        for i, e in enumerate(chain, 1):
            print(f"    {i}. {e['model']:<42} reasoning={overrides.get(e['model'])}")
        print()
        return

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if default_model:
        mcfg = config.setdefault("model", {})
        mcfg["default"] = default_model["model"]
        mcfg["provider"] = default_model["provider"]
        for k in ("base_url", "api_key", "key_env"):
            if k in default_model:
                mcfg[k] = default_model[k]

    config["fallback_providers"] = chain
    config.setdefault("agent", {})["reasoning_overrides"] = overrides

    sys.path.insert(0, str(HERMES_SRC))
    from utils import atomic_roundtrip_yaml_save     # ruamel: preserves comments/order
    atomic_roundtrip_yaml_save(CONFIG_PATH, config)

    print(f"\nUpdated {CONFIG_PATH}")
    if default_model:
        print(f"  model.default -> {default_model['model']}")
    elif promote and current_default:
        print(f"  model.default -> {current_default} (unchanged; chain refreshed)")


def main():
    global CONFIG_PATH, FALLBACK_CACHE_PATH, PROMOTION_HISTORY_PATH, PROBE_VERSION
    global HERMES_HOME, AUTH_PATH, META_CACHE_PATH, REASONING_CAPS_PATH, HERMES_SRC

    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv
    fast = "--fast" in argv
    promote = "--no-promote" not in argv
    sort_by = "usage" if ("--sort" in argv and "usage" in argv) else "release_date"
    default_effort = argv[argv.index("--default-effort") + 1] if "--default-effort" in argv else "inherit"
    margin = int(argv[argv.index("--promotion-margin") + 1]) if "--promotion-margin" in argv else PROMOTION_MARGIN
    hysteresis = int(argv[argv.index("--hysteresis-days") + 1]) if "--hysteresis-days" in argv else HYSTERESIS_DAYS

    # --hermes-home PATH: point the whole run at a specific Hermes home. Preferred over the
    # HERMES_HOME env var when a launcher (e.g. the nightly task) already knows the absolute
    # path, because an env var is easy to lose across a service boundary.
    if "--hermes-home" in argv:
        HERMES_HOME = Path(argv[argv.index("--hermes-home") + 1])
        CONFIG_PATH = HERMES_HOME / "config.yaml"
        AUTH_PATH = HERMES_HOME / "auth.json"
        META_CACHE_PATH = HERMES_HOME / "cache" / "endpoint_model_metadata.json"
        REASONING_CAPS_PATH = HERMES_HOME / "cache" / "reasoning_caps.json"
        FALLBACK_CACHE_PATH = HERMES_HOME / "cache" / "fallback_model_cache.json"
        PROMOTION_HISTORY_PATH = HERMES_HOME / "cache" / "promotion_history.json"
        HERMES_SRC = HERMES_HOME / "hermes-agent"

    if "--config" in argv:
        CONFIG_PATH = Path(argv[argv.index("--config") + 1])
    if "--cache" in argv:
        FALLBACK_CACHE_PATH = Path(argv[argv.index("--cache") + 1])
    if "--history" in argv:
        PROMOTION_HISTORY_PATH = Path(argv[argv.index("--history") + 1])

    # --today YYYY-MM-DD: treat this run as happening on that date. For testing the gate
    # across simulated nights and for backfilling history; never needed in normal use.
    if "--today" in argv:
        today = argv[argv.index("--today") + 1]
        try:
            date.fromisoformat(today)
        except ValueError:
            print(f"ERROR: --today expects YYYY-MM-DD, got '{today}'")
            sys.exit(2)
    else:
        today = date.today().isoformat()

    if "--show-history" in argv:
        show_history(load_history())
        return

    with open(CONFIG_PATH, encoding="utf-8") as f:
        current_default = ((yaml.safe_load(f) or {}).get("model") or {}).get("default") or ""

    print("Loading auth credentials...")
    try:
        auth = load_auth()
    except AuthError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Validate auth with a lightweight probe before running the full model sweep.
    # A 401 here means the token is invalid/expired -- no point probing all models.
    print("Validating auth token...")
    try:
        status, body, code = _post(
            {"model": "meituan/longcat-2.5-preview:free",
             "messages": [{"role": "user", "content": "hi"}],
             "max_tokens": 1},
            auth, timeout=15)
        if code == 401:
            print("\nERROR: Auth token is invalid or expired (HTTP 401).")
            print("Run 'hermes model' to re-authenticate, then re-run this selector.")
            sys.exit(1)
        if code is not None and code >= 500:
            print(f"\nWARNING: Auth validation got HTTP {code} -- proceeding anyway.")
        elif status != "ok":
            print(f"\nWARNING: Auth validation failed ({status}) -- proceeding anyway.")
        else:
            print("Auth OK.")
    except Exception as e:
        print(f"\nWARNING: Auth validation error ({e}) -- proceeding anyway.")

    history = load_history()
    print(f"Loaded promotion history: {len(history)} model(s) tracked")

    print("Reading free models from endpoint_model_metadata.json cache...")
    free_models = get_free_models_from_cache()
    print(f"Found {len(free_models)} free models in cache")

    print("\nEnriching models (release dates + probe queries)...")
    passed, probed_all = enrich_models(free_models, auth, dry_run=dry_run, fast=fast)

    # Fold outcomes into history BEFORE choosing, so tonight's clean pass counts toward the margin.
    if not dry_run:
        update_history(history, probed_all, today)
        removed = prune_history(history, today)
        if removed:
            print(f"  Pruned {len(removed)} stale history entr(ies): {', '.join(removed)}")
        # Persist immediately. Previously save_history() ran at the very END of main(), so any
        # crash after this point (e.g. the UnicodeEncodeError that used to fire inside
        # choose_default) discarded the entire night's probe evidence.
        save_history(history)
        print(f"  Saved promotion history to {PROMOTION_HISTORY_PATH}")

    passed = sort_models(passed, sort_by=sort_by)

    print(f"\n{'='*80}")
    print(f"Passed models, newest first ({len(passed)}):")
    print(f"{'='*80}")
    for i, m in enumerate(passed, 1):
        st = streak(history, m["model"])
        clean = "CLEAN" if m.get("clean_pass") else ("unclean" if m.get("clean_pass") is False else "?")
        print(f"  {i}. [{m.get('release_date','N/A')}] {m['model']}")
        print(f"     reasoning={m.get('reasoning')}  pass={clean}  streak={st}")

    chosen, reason = (None, "--no-promote: chain only")
    if promote:
        chosen, reason = choose_default(passed, probed_all, history, current_default,
                                        margin, hysteresis)
        print(f"\n  DECISION: {reason}")

    if not dry_run and chosen:
        mid = chosen["model"]
        rec = history.setdefault(mid, _blank_record(today))
        if current_default != mid:
            rec["promotion_count"] = rec.get("promotion_count", 0) + 1
            rec["promoted_at"] = today

    update_config(passed, chosen, reason, dry_run=dry_run, promote=promote,
                  default_effort=default_effort, current_default=current_default)

    if not dry_run:
        save_history(history)
        print("Done. Run 'hermes fallback list' to verify, or --show-history for gate state.")

if __name__ == "__main__":
    main()
