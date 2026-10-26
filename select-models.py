#!/usr/bin/env python3
"""Central free-model selector (Phase 2)."""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (LOCALAPPDATA / "hermes"))
CONFIG_PATH = HERMES_HOME / "config.yaml"
CACHE_PATH = HERMES_HOME / "cache" / "free_model_probe_cache.json"
REPO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_DIR))

import providers

CACHE_TTL_DAYS = 7
RANKING = None


def load_cache():
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"entries": {}}


def save_cache(cache):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  (cache write failed: {e})", file=sys.stderr)


def cache_key(provider, model_id):
    return f"{provider}::{model_id}"


def _failure_class(e):
    if e.get("failure_class"):
        return e["failure_class"]
    if e.get("passed"):
        return "standard"
    note = (e.get("note") or "").lower()
    if " 404" in note or "http 404" in note or "410" in note or "not found" in note or "gone" in note:
        return "permanent"
    if ("429" in note or "500" in note or "502" in note or "503" in note
            or "timeout" in note or "empty content" in note or "timed out" in note):
        return "transient"
    return "standard"


def _ttl_days(fc):
    return {"permanent": 30, "transient": 1, "standard": CACHE_TTL_DAYS}.get(fc, CACHE_TTL_DAYS)


def cached_result(cache, provider, model_id, refresh):
    if refresh:
        return None
    e = cache.get("entries", {}).get(cache_key(provider, model_id))
    if not e:
        return None
    try:
        probed = datetime.fromisoformat(e["probed_at"])
        fc = _failure_class(e)
        if (datetime.now(timezone.utc) - probed).days >= _ttl_days(fc):
            return None
    except Exception:
        return None
    return bool(e.get("passed")), e.get("note", ""), bool(e.get("retried", False))


def put_cache(cache, provider, model_id, passed, note, retried):
    fc = "standard" if passed else None
    low = str(note).lower()
    if not passed:
        if "404" in low or "410" in low or "not found" in low or "gone" in low:
            fc = "permanent"
        elif ("429" in low or "500" in low or "502" in low or "503" in low
              or "timeout" in low or "empty content" in low or "timed out" in low):
            fc = "transient"
        else:
            fc = "standard"
    cache.setdefault("entries", {})[cache_key(provider, model_id)] = {
        "passed": bool(passed),
        "note": str(note),
        "retried": bool(retried),
        "failure_class": fc,
        "probed_at": datetime.now(timezone.utc).isoformat(),
    }


DEFAULT_RANKING_WEIGHTS = {
    "release_date": 0.40,
    "context_window": 0.30,
    "provider_priority": 0.30,
}
DEFAULT_PROVIDER_PRIORITY = {
    "openrouter": 1.0,
    "gemini": 0.8,
    "nvidia": 0.7,
    "novita": 0.6,
    "ollama-cloud": 0.75,
    "ollama-local": 0.9,
}


def extract_release_date(m):
    if not isinstance(m, dict):
        return None
    for key in ("_release_date", "release_date", "created"):
        v = m.get(key)
        if v is None:
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            # unix timestamp (seconds or ms)
            ts = float(v)
            if ts > 1e12:
                ts = ts / 1000.0
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            except (ValueError, OSError, OverflowError):
                continue
        s = str(v).strip()
        if not s:
            continue
        # ISO-ish date
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.date().isoformat()
        except ValueError:
            pass
        # unix string
        try:
            ts = float(s)
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        except ValueError:
            continue
    return None


def load_ranking_config():
    cfg, _ = load_config()
    mr = cfg.get("model_ranking") if isinstance(cfg, dict) else None
    if not isinstance(mr, dict):
        mr = {}
    weights = mr.get("weights")
    if not isinstance(weights, dict):
        weights = dict(DEFAULT_RANKING_WEIGHTS)
    # normalize to sum 1
    try:
        total = sum(float(v) for v in weights.values())
    except (TypeError, ValueError):
        total = 0.0
    if total > 0:
        weights = {k: float(v) / total for k, v in weights.items()}
    else:
        weights = dict(DEFAULT_RANKING_WEIGHTS)
    pp = mr.get("provider_priority")
    if not isinstance(pp, dict):
        pp = dict(DEFAULT_PROVIDER_PRIORITY)
    else:
        # fill defaults for missing providers
        merged = dict(DEFAULT_PROVIDER_PRIORITY)
        merged.update(pp)
        pp = merged
    try:
        missing = float(mr.get("missing_release_date", 0.0))
    except (TypeError, ValueError):
        missing = 0.0
    return {"weights": weights, "provider_priority": pp, "missing_release_date": missing}


def _to_ts(rd):
    if rd is None:
        return None
    if isinstance(rd, bool):
        return None
    if isinstance(rd, (int, float)):
        ts = float(rd)
        if ts > 1e12:
            ts = ts / 1000.0
        return ts
    s = str(rd).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        pass
    try:
        ts = float(s)
        if ts > 1e12:
            ts = ts / 1000.0
        return ts
    except ValueError:
        return None


def rank_models(models, ranking=None):
    if ranking is None:
        ranking = load_ranking_config()
    if not models:
        return models
    weights = ranking["weights"]
    pp_map = ranking["provider_priority"]
    missing = ranking["missing_release_date"]

    # context min/max
    cws = [m.get("context_window") for m in models if isinstance(m.get("context_window"), (int, float))]
    cw_min = min(cws) if cws else 0
    cw_max = max(cws) if cws else 0
    # release ts min/max
    ts_list = [_to_ts(m.get("_release_date")) for m in models]
    ts_valid = [t for t in ts_list if t is not None]
    ts_min = min(ts_valid) if ts_valid else 0
    ts_max = max(ts_valid) if ts_valid else 0

    for idx, m in enumerate(models):
        # context norm
        cw = m.get("context_window")
        if isinstance(cw, (int, float)) and cw_max > cw_min:
            cw_norm = (cw - cw_min) / (cw_max - cw_min)
        elif isinstance(cw, (int, float)) and cws:
            cw_norm = 1.0
        else:
            cw_norm = 0.0
        # release norm
        ts = ts_list[idx]
        if ts is not None:
            if ts_max > ts_min:
                rel_norm = (ts - ts_min) / (ts_max - ts_min)
            else:
                rel_norm = 1.0
        else:
            rel_norm = missing
        # provider priority norm
        prov = m.get("_provider")
        raw_pp = pp_map.get(prov, 1.0)
        try:
            pp_norm = float(raw_pp)
        except (TypeError, ValueError):
            pp_norm = 1.0
        pp_norm = max(0.0, min(1.0, pp_norm))

        score = (weights.get("release_date", 0.0) * rel_norm
                 + weights.get("context_window", 0.0) * cw_norm
                 + weights.get("provider_priority", 0.0) * pp_norm)
        m["_score"] = round(score, 4)
        m["_score_parts"] = {
            "release": round(rel_norm, 3),
            "context": round(cw_norm, 3),
            "provider": round(pp_norm, 3),
        }

    return sorted(models, key=lambda m: (-(m.get("_score") or 0), -(m.get("context_window") or 0), str(m.get("id"))))


def fetch_provider(slug, cfg):
    if slug == "ollama-local":
        try:
            import hermes_ollama_selector as hos
            base = hos.resolve_base_url()
            raws = hos.fetch_models(base)
            out = []
            for m in raws or []:
                out.append({
                    "id": m.get("name") or m.get("model"),
                    "display_name": m.get("name") or m.get("model"),
                    "context_window": m.get("context_length"),
                    "rate_limits": None,
                    "notes": "local ollama (free)",
                })
            return out, None
        except Exception as e:
            return [], str(e)
    modname = cfg["module"]
    try:
        mod = __import__(modname)
    except Exception as e:
        return [], f"import failed: {e}"
    fn = None
    for name in ("poll", "fetch_models", "fetch", "fetch_free_models", "get_models"):
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


def sort_candidates(models):
    def key(m):
        cw = m.get("context_window")
        try:
            return (0, -int(cw)) if cw is not None else (1, 0)
        except (TypeError, ValueError):
            return (1, 0)
    return sorted(models, key=key)


import urllib.request
import urllib.error


def _openai_probe(model_id, base_url, api_key, timeout=30, extra_headers=None):
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url = url + "/v1"
    url = url + "/chat/completions"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Say 'ok' and nothing else."}],
        "max_tokens": 64,
    }
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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            content = (msg.get("content") or "").strip()
            if content:
                return True, f"ok: {content[:40]}", retried
            fr = choice.get("finish_reason") if isinstance(choice, dict) else None
            note = f"empty content (finish_reason={fr})" if fr else "empty content"
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


def _gemini_probe(model_id, api_key, base_url, timeout=30):
    mid = model_id
    if not mid.startswith("models/"):
        mid = "models/" + mid
    url = f"{base_url.rstrip('/')}/{mid}:generateContent?key={api_key}"
    payload = {"contents": [{"parts": [{"text": "Say 'ok' and nothing else."}]}],
               "generationConfig": {"maxOutputTokens": 64}}
    retried = False
    last = "no attempt"
    for delay in (0, 5, 15):
        if delay:
            retried = True
            time.sleep(delay)
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json",
                                             "User-Agent": "easy-hermes/select-models"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            cands = data.get("candidates") or []
            if cands:
                parts = (cands[0].get("content", {}) or {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
                if text:
                    return True, f"ok: {text[:40]}", retried
                fr = cands[0].get("finishReason")
                if fr:
                    last = f"empty content (finishReason={fr})"
                else:
                    last = "empty content"
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
        import hermes_ollama_selector as hos
    except Exception as e:
        return False, f"import failed: {e}", False
    try:
        ok, note, retried = hos.probe_model(model_id, hos.openai_base(base_url), fast=False)
        return bool(ok), str(note), bool(retried)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", False


def probe(slug, cfg, model_id, api_key, nightly, timeout=None):
    kind = cfg.get("probe_kind")
    if kind == "openai":
        base = cfg.get("probe_base_url") or ""
        t = timeout if timeout else cfg.get("probe_timeout", 30)
        return _openai_probe(model_id, base, api_key, timeout=t, extra_headers=cfg.get("probe_headers"))
    if kind == "gemini":
        base = cfg.get("probe_base_url") or "https://generativelanguage.googleapis.com/v1beta"
        t = timeout if timeout else cfg.get("probe_timeout", 30)
        return _gemini_probe(model_id, api_key, base, timeout=t)
    if kind == "ollama-local":
        try:
            import hermes_ollama_selector as hos
            bu = hos.resolve_base_url()
        except Exception:
            bu = "http://localhost:11434"
        return _ollama_local_probe(model_id, bu)
    return False, "unknown probe kind", False


def load_config():
    import yaml
    if not CONFIG_PATH.exists():
        return {}, yaml
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}), yaml
    except Exception:
        return {}, yaml


def write_config(passing, yaml_mod):
    if not passing:
        return
    try:
        if CONFIG_PATH.exists():
            bak = CONFIG_PATH.with_suffix(".yaml.bak")
            bak.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    cfg, _ = load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    if "model_ranking" not in cfg:
        cfg["model_ranking"] = {
            "weights": dict(DEFAULT_RANKING_WEIGHTS),
            "provider_priority": dict(DEFAULT_PROVIDER_PRIORITY),
            "missing_release_date": 0.0,
        }
    best = passing[0]
    best_provider = best.get("_provider", "custom")
    hermes_provider = best_provider if best_provider in ("nous", "openrouter", "custom") else "custom"

    fb = []
    for m in passing[1:]:
        prov = m.get("_provider", "custom")
        hp = prov if prov in ("nous", "openrouter", "custom") else "custom"
        entry = {"provider": hp, "model": m["id"]}
        if hp == "custom" and m.get("_base_url"):
            entry["base_url"] = m["_base_url"]
        fb.append(entry)

    mcfg = cfg.setdefault("model", {})
    if not isinstance(mcfg, dict):
        mcfg = {"default": mcfg} if isinstance(mcfg, str) else {}
        cfg["model"] = mcfg
    mcfg["default"] = best["id"]
    mcfg["provider"] = hermes_provider
    if hermes_provider == "custom" and best.get("_base_url"):
        mcfg["base_url"] = best["_base_url"]

    existing = cfg.get("fallback_providers")
    if not isinstance(existing, list):
        existing = []
    existing_keys = {(str(e.get("provider")), str(e.get("model"))) for e in existing if isinstance(e, dict)}
    new_fb = [e for e in fb if (str(e.get("provider")), str(e.get("model"))) not in existing_keys]
    cfg["fallback_providers"] = list(existing) + new_fb

    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml_mod.safe_dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        print(f"Updated {CONFIG_PATH}")
    except Exception as e:
        print(f"ERROR writing config: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Central free-model selector (Phase 2)")
    ap.add_argument("--dry-run", action="store_true", help="Preview only, write nothing")
    ap.add_argument("--nightly", action="store_true", help="Probe top-12 (out-of-hours)")
    ap.add_argument("--refresh", action="store_true", help="Ignore probe cache")
    ap.add_argument("--prefer-local", action="store_true", help="Include local Ollama")
    ap.add_argument("--providers", help="Comma-separated provider slugs")
    ap.add_argument("--apply", action="store_true", help="Write config")
    ap.add_argument("--json", action="store_true", help="Emit JSON summary")
    ap.add_argument("--top", type=int, help="Override probe budget")
    ap.add_argument("--full", action="store_true", help="Probe every candidate (ignore top-N budget)")
    args = ap.parse_args()

    dry_run = args.dry_run
    nightly = args.nightly
    if args.full:
        top_n = None
    else:
        top_n = args.top if args.top else (12 if nightly else 3)

    user_list = None
    if args.providers:
        user_list = [x.strip() for x in args.providers.split(",") if x.strip()]

    cache = load_cache()
    ranking = load_ranking_config()
    global RANKING
    RANKING = ranking
    configured = providers.configured_providers(user_list=user_list, prefer_local=args.prefer_local)

    all_passing = []
    summary = []

    for slug in configured:
        cfg = providers.PROVIDERS[slug]
        print(f"\n=== {cfg['display']} ({slug}) ===")
        ok, vnote = providers.validate_key(cfg)
        if not ok:
            print(f"  key validation failed: {vnote}")
            summary.append({"provider": slug, "fetched": 0, "passed": 0, "error": vnote})
            continue
        if vnote and vnote != "no key required":
            print(f"  {vnote}")
        models, err = fetch_provider(slug, cfg)
        if err:
            print(f"  fetch error: {err}")
            summary.append({"provider": slug, "fetched": 0, "passed": 0, "error": err})
            continue

        for m in models:
            if isinstance(m, dict) and "_release_date" not in m:
                m["_release_date"] = extract_release_date(m)

        key, _ = providers.get_api_key(cfg)

        if not cfg.get("probe", True):
            cands = rank_models(models, RANKING)[:top_n]
            print(f"  fetched {len(models)} candidate(s); listing-only, including top {len(cands)} (unverified)")
            passed_here = 0
            for m in cands:
                m["_provider"] = slug
                m["_base_url"] = cfg.get("probe_base_url")
                m["_probe_ok"] = None
                m["_probe_note"] = "unverified (listing only, no probe)"
                all_passing.append(m)
                passed_here += 1
            summary.append({"provider": slug, "fetched": len(models), "probed": 0,
                            "passed": passed_here})
            continue

        print(f"  fetched {len(models)} candidate(s)")
        if slug == "nvidia":
            try:
                import poll_nvidia as _pn
                cands = [m for m in models if _pn._is_chat_like(m.get("id") or "")]
                if top_n is not None:
                    cands = cands[:top_n]
                print(f"  nvidia: probing {len(cands)} chat-like models (account-gated; agreements matter)")
            except Exception:
                cands = rank_models(models, RANKING)
                if top_n is not None:
                    cands = cands[:top_n]
        else:
            cands = rank_models(models, RANKING)
            if top_n is not None:
                cands = cands[:top_n]
        print(f"  probing top {len(cands)}")

        can_probe = True
        if cfg.get("probe_kind") == "openai" and not key:
            can_probe = False

        passed_here = 0
        for m in cands:
            mid = m.get("id")
            if not can_probe:
                if passed_here >= 1:
                    print(f"    skip {mid}  (no API key to probe)")
                    continue
                m["_provider"] = slug
                m["_base_url"] = cfg.get("probe_base_url")
                m["_probe_ok"] = None
                m["_probe_note"] = "unverified (no API key to probe)"
                all_passing.append(m)
                passed_here += 1
                print(f"    UNVERIFIED {mid}  (no API key to probe)")
                continue
            cr = cached_result(cache, slug, mid, args.refresh)
            if cr is not None:
                cached_passed, cached_note, cached_retried = cr
                ok, note, retried = cached_passed, cached_note, cached_retried
                note = f"[cache] {note}"
            else:
                ok, note, retried = probe(slug, cfg, mid, key, nightly, timeout=cfg.get("probe_timeout"))
                put_cache(cache, slug, mid, ok, note, retried)
            clean = bool(ok) and not bool(retried)
            m["_provider"] = slug
            m["_base_url"] = cfg.get("probe_base_url")
            m["_probe_ok"] = clean
            m["_probe_note"] = note
            if clean:
                all_passing.append(m)
                passed_here += 1
            print(f"    {'PASS' if clean else 'fail'} {mid}  ({note})")
        summary.append({"provider": slug, "fetched": len(models), "probed": len(cands),
                        "passed": passed_here})

    save_cache(cache)

    all_passing = rank_models(all_passing, RANKING)

    print("\n" + "=" * 70)
    print(f"Passing models ({len(all_passing)}):")
    print("=" * 70)
    for i, m in enumerate(all_passing, 1):
        print(f"  {i}. [{m['_provider']}] {m['id']}  ctx={m.get('context_window')}  score={m.get('_score')}  release={m.get('_release_date')}")

    if args.json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "nightly": nightly,
            "top_n": top_n,
            "passing_count": len(all_passing),
            "passing": [
                {
                    "id": m.get("id"),
                    "provider": m.get("_provider"),
                    "context_window": m.get("context_window"),
                    "release_date": m.get("_release_date"),
                    "score": m.get("_score"),
                    "score_parts": m.get("_score_parts"),
                    "base_url": m.get("_base_url"),
                }
                for m in all_passing
            ],
            "summary": summary,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    if not all_passing:
        print("\nNo models passed probing -- config untouched.")
        sys.exit(0)

    dry = args.dry_run
    if not args.dry_run and not args.apply:
        dry = False
    if args.dry_run:
        dry = True

    if dry:
        print("\nDRY RUN -- nothing written. Would set:")
        if all_passing:
            print(f"  model.default -> {all_passing[0]['id']}")
        for m in all_passing[1:]:
            print(f"  fallback: {m['id']} ({m['_provider']})")
        sys.exit(0)

    import yaml
    write_config(all_passing, False, yaml)
    print("Done.")


if __name__ == "__main__":
    main()
