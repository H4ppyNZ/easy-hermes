"""selector.runner — extracted from select-models.py

Split out of the original monolithic select-models.py.  Function bodies are
moved verbatim; only module-level path state is routed through
``selector.paths`` so it stays mutable at runtime.
"""
from __future__ import annotations

from selector import paths
from selector.paths import DEFAULT_PROVIDER_PRIORITY, DEFAULT_RANKING_WEIGHTS, HYSTERESIS_DAYS, POLL_COOLDOWN_SECONDS, PROMOTION_MARGIN
from selector.configio import load_config, write_config
from selector.history import _blank_promo_record, _model_id, choose_default, load_promotion_history, prune_promotion_history, save_promotion_history, show_promotion_history, show_response_times, update_promotion_history
from selector.lists import is_banned, load_banned, load_boosted, save_banned, save_boosted
from selector.probe_cache import cached_result, put_cache
from selector.probing import _extract_elapsed_ms, fetch_provider, probe, reasoning_score_from_note
from selector.ranking import approximate_context, build_ranking_from_mr, extract_capabilities, extract_release_date, load_ranking_config, rank_models
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date
import providers
try:
    import chain_delta
except Exception:
    chain_delta = None
from selector.paths import load_cache, load_last_selection, save_cache, save_last_selection


def _process_provider(slug, cfg, *, ranking, reasoning_mode, top_n, args, banned, cache, cache_lock, print_lock):
    if slug == "ollama-local":
        try:
            from pollers import poll_ollama_local as _pol
            cfg = dict(cfg)
            cfg["probe_base_url"] = _pol.openai_base(_pol.resolve_base_url())
        except Exception:
            pass
    # Cap OpenRouter probing to avoid burning free-tier daily quota unless overridden.
    if slug == "openrouter":
        cap = getattr(args, "openrouter_top", None)
        if cap is None:
            cap = 3
        if cap and cap > 0 and (top_n is None or top_n > cap):
            top_n = cap
    passing_list = []
    probed_list = []
    _buf = []

    def _print(*a, **k):
        sep = k.get("sep", " ")
        end = k.get("end", "\n")
        _buf.append(sep.join(str(x) for x in a) + end)

    def _flush():
        if not _buf:
            return
        with print_lock:
            sys.stdout.write("".join(_buf))
            sys.stdout.flush()
        _buf.clear()

    _print(f"\n=== {cfg['display']} ({slug}) ===")
    ok, vnote = providers.validate_key(cfg)
    if not ok:
        _print(f"  key validation failed: {vnote}")
        _flush()
        return passing_list, probed_list, {"provider": slug, "fetched": 0, "passed": 0, "error": vnote}
    if vnote and vnote != "no key required":
        _print(f"  {vnote}")
    models, err = fetch_provider(slug, cfg)
    if err:
        _print(f"  fetch error: {err}")
        _flush()
        return passing_list, probed_list, {"provider": slug, "fetched": 0, "passed": 0, "error": err}

    models = [m for m in (models or []) if not is_banned(m.get("id") if isinstance(m, dict) else m, banned)]
    for m in models:
        if isinstance(m, dict) and "_release_date" not in m:
            m["_release_date"] = extract_release_date(m)
        if isinstance(m, dict):
            m["_capabilities"] = extract_capabilities(m)
        if isinstance(m, dict) and m.get("context_window") is None:
            approx_ctx = approximate_context(m)
            if approx_ctx is not None:
                m["context_window"] = approx_ctx

    key, _ = providers.get_api_key(cfg)

    # Boosted models keep their sort boost for ranking, but they must prove
    # they are functional RIGHT NOW to stay in the passing/fallback chain.
    # Bypass the probe cache so a stale "passed" entry cannot keep a dead
    # boosted model in the chain.
    try:
        boosted = load_boosted()
    except Exception:
        boosted = set()

    # If probing is enabled but no credential is available (and the provider
    # isn't a keyless local daemon), fall back to listing-only instead of
    # probing unauthenticated and failing every model.
    if paths.LIST_ONLY or (not cfg.get("probe", True)) or (key is None and slug != "ollama-local"):
        # listing-only path
        _listing_only = True
    else:
        _listing_only = False

    if _listing_only:
        cands = rank_models(models, ranking)
        if top_n is not None:
            cands = cands[:top_n]
        _print(f"  fetched {len(models)} candidate(s); listing-only, including top {len(cands)} (unverified, not added to passing)")
        for m in cands:
            m["_provider"] = slug
            m["_base_url"] = cfg.get("probe_base_url")
            m["_probe_ok"] = None
            m["_probe_note"] = "unverified (listing only, no probe)"
            m["_reasoning"] = 0.0
            m["_response_time_ms"] = None
            # Unverified models (no probe possible, e.g. missing API key) must
            # not enter passing_list / fallback / model.default. Track them in
            # probed_list for history only.
            probed_list.append({"model": m["id"], "test_passed": False, "clean_pass": False,
                               "fail_note": "listing-only (no probe)", "release_date": m.get("_release_date")})
        _flush()
        return passing_list, probed_list, {"provider": slug, "fetched": len(models), "probed": 0,
                                           "passed": 0}

    _print(f"  fetched {len(models)} candidate(s)")
    cf = cfg.get("chat_filter")
    if cf:
        try:
            mod_name, fn_name = cf.split(":", 1)
            import importlib
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name)
            cands = [m for m in models if fn(m.get("id") or "")]
            if top_n is not None:
                cands = cands[:top_n]
            _print(f"  {slug}: probing {len(cands)} chat-like models (account-gated; agreements matter)")
        except Exception:
            cands = rank_models(models, ranking)
            if top_n is not None:
                cands = cands[:top_n]
    else:
        cands = rank_models(models, ranking)
        if top_n is not None:
            cands = cands[:top_n]
    _print(f"  probing top {len(cands)}")

    can_probe = not (cfg.get("probe_kind") == "openai" and not key)
    passed_here = 0
    poll_cooldown = getattr(args, "poll_cooldown", None)
    if poll_cooldown is None:
        poll_cooldown = POLL_COOLDOWN_SECONDS
        if getattr(args, "full", False):
            poll_cooldown = max(poll_cooldown, 2.0)
    for i, m in enumerate(cands):
        if i > 0 and poll_cooldown and poll_cooldown > 0:
            time.sleep(poll_cooldown)
        mid = m.get("id")
        if not can_probe:
            if passed_here >= 1:
                _print(f"    skip {mid}  (no API key to probe)")
                continue
            m["_provider"] = slug
            m["_base_url"] = cfg.get("probe_base_url")
            m["_probe_ok"] = None
            m["_probe_note"] = "unverified (no API key to probe)"
            m["_reasoning"] = 0.0
            m["_response_time_ms"] = None
            passing_list.append(m)
            passed_here += 1
            _print(f"    UNVERIFIED {mid}  (no API key to probe)")
            probed_list.append({"model": mid, "test_passed": True, "clean_pass": False,
                               "fail_note": "no API key to probe", "release_date": m.get("_release_date")})
            continue
        # Boosted models are always re-probed live: a stale cached pass must
        # not keep a non-functional boosted model in passing/fallback.
        with cache_lock:
            cr = None if str(mid) in boosted else cached_result(cache, slug, mid, args.refresh)
        if cr is not None:
            cached_passed, cached_note, cached_retried = cr
            ok2, note, retried = cached_passed, cached_note, cached_retried
            note = f"[cache] {note}"
        else:
            ok2, note, retried = probe(slug, cfg, mid, key,
                                       timeout=cfg.get("probe_timeout"), reasoning=reasoning_mode)
            with cache_lock:
                put_cache(cache, slug, mid, ok2, note, retried)
        clean = bool(ok2) and not bool(retried)
        m["_provider"] = slug
        m["_base_url"] = cfg.get("probe_base_url")
        m["_probe_ok"] = clean
        m["_probe_note"] = note
        m["_reasoning"] = reasoning_score_from_note(note)
        m["_response_time_ms"] = _extract_elapsed_ms(note)
        if clean:
            passing_list.append(m)
            passed_here += 1
        _print(f"    {'PASS' if clean else 'fail'} {mid}  ({note})")
        probed_list.append({"model": mid, "test_passed": bool(ok2), "clean_pass": clean,
                           "fail_note": None if ok2 else note, "release_date": m.get("_release_date")})

    _flush()
    return passing_list, probed_list, {"provider": slug, "fetched": len(models), "probed": len(cands),
                                       "passed": passed_here}


def main():
    ap = argparse.ArgumentParser(description="Central free-model selector (Phase 2)")
    ap.add_argument("--dry-run", action="store_true", help="Preview only, write nothing")
    ap.add_argument("--refresh", action="store_true", help="Ignore probe cache")
    ap.add_argument("--prefer-local", action="store_true", help="Include local Ollama")
    ap.add_argument("--providers", help="Comma-separated provider slugs")
    ap.add_argument("--apply", action="store_true", help="Write config")
    ap.add_argument("--json", action="store_true", help="Emit JSON summary")
    ap.add_argument("--top", type=int, help="Override probe budget")
    ap.add_argument("--full", action="store_true", help="Probe every candidate (ignore top-N budget)")
    ap.add_argument("--set-weight", action="append", metavar="KEY=VALUE",
                    help="Set a ranking weight, e.g. release_date=0.5 (repeatable)")
    ap.add_argument("--set-priority", action="append", metavar="PROVIDER=VALUE",
                    help="Set provider priority, e.g. openrouter=1.0 (repeatable)")
    ap.add_argument("--set-missing-release", type=float, metavar="SCORE",
                    help="Score (0..1) used when a model has no release date")
    ap.add_argument("--show-ranking", action="store_true",
                    help="Print the current ranking matrix and exit")
    ap.add_argument("--model", action="append", metavar="MODEL_ID",
                    help="Retest a specific model id on the selected providers (repeatable)")
    ap.add_argument("--reasoning", action="store_true",
                    help="Probe with reasoning_effort=high and report reasoning chars")
    ap.add_argument("--ban", action="append", metavar="MODEL_ID",
                    help="Ban a model id (persisted; filtered from candidates)")
    ap.add_argument("--unban", action="append", metavar="MODEL_ID",
                    help="Unban a model id")
    ap.add_argument("--list-banned", action="store_true",
                    help="Print banned model ids and exit")
    ap.add_argument("--boost", action="append", metavar="MODEL_ID",
                    help="Boost a model id (sorts to top)")
    ap.add_argument("--unboost", action="append", metavar="MODEL_ID",
                    help="Remove a model from the boosted list")
    ap.add_argument("--list-boosted", action="store_true",
                    help="Print boosted model ids and exit")
    ap.add_argument("--set-default", metavar="MODEL_ID",
                    help="Force model.default to this id (bypasses promotion gate)")
    ap.add_argument("--promotion-margin", type=int, metavar="N",
                    help=f"Consecutive clean passes required to be eligible (default {PROMOTION_MARGIN})")
    ap.add_argument("--hysteresis-days", type=int, metavar="D",
                    help=f"Days newer a challenger must be to displace incumbent (default {HYSTERESIS_DAYS})")
    ap.add_argument("--no-emergency", action="store_true",
                    help="Disable emergency promotion when incumbent is dead")
    ap.add_argument("--show-history", action="store_true",
                    help="Print promotion history table and exit")
    ap.add_argument("--show-response-times", action="store_true",
                    help="Print recorded probe response times (ms) and exit")
    ap.add_argument("--list-only", action="store_true",
                    help="List candidates without probing; include top models unverified")
    ap.add_argument("--no-promote", action="store_true",
                    help="Only refresh the fallback chain; leave model.default unchanged")
    ap.add_argument("--hermes-home", metavar="PATH",
                    help="Hermes home directory (overrides HERMES_HOME env / default)")
    ap.add_argument("--poll-cooldown", type=float, metavar="SEC",
                    help=f"Seconds to wait between model probes within a provider (default {POLL_COOLDOWN_SECONDS}; {2.0} when --full)")
    ap.add_argument("--openrouter-top", type=int, metavar="N",
                    help="Cap OpenRouter probes to N (default 3; 0 = no cap)")
    ap.add_argument("--capability", metavar="CAP",
                    help="Keep only models whose _capabilities contains CAP")
    ap.add_argument("--today", metavar="YYYY-MM-DD",
                    help="Treat this run as happening on the given date (testing/backfill)")
    args = ap.parse_args()

    if getattr(args, "hermes_home", None):
        import os as _os
        _os.environ["HERMES_HOME"] = args.hermes_home
        paths.rebind_hermes_home(args.hermes_home)

    reasoning_mode = "high" if getattr(args, "reasoning", False) else None

    paths.LIST_ONLY = bool(getattr(args, "list_only", False))

    # Load Nous Portal credential from Hermes auth store into env (if not already set),
    # so Nous models can be probed like any other provider.
    if not os.environ.get("NOUS_API_KEY"):
        try:
            import json as _j
            _auth_path = paths.HERMES_HOME / "auth.json"
            if _auth_path.exists():
                _auth = _j.loads(_auth_path.read_text(encoding="utf-8"))
                _cp = _auth.get("credential_pool", {})
                _nous_creds = _cp.get("nous", []) if isinstance(_cp, dict) else []
                for _c in _nous_creds:
                    _tok = _c.get("access_token") if isinstance(_c, dict) else None
                    if _tok:
                        os.environ["NOUS_API_KEY"] = str(_tok).strip()
                        break
        except Exception:
            pass

    # --- ban list management ---
    banned = load_banned()
    if args.list_banned:
        print("Banned models:")
        for m in sorted(banned):
            print(f"  {m}")
        if not banned:
            print("  (none)")
        sys.exit(0)
    if args.ban:
        for m in args.ban:
            banned.add(m)
        save_banned(banned)
        print(f"Banned: {', '.join(args.ban)}")
    if args.unban:
        for m in args.unban:
            banned.discard(m)
        save_banned(banned)
        print(f"Unbanned: {', '.join(args.unban)}")
    if args.ban or args.unban:
        pass

    # --- boosted list management ---
    boosted = load_boosted()
    if args.list_boosted:
        for m in sorted(boosted):
            print(m)
        if not boosted:
            print("(none)")
        sys.exit(0)
    if args.boost:
        for m in args.boost:
            boosted.add(m)
        save_boosted(boosted)
        print(f"Boosted: {', '.join(args.boost)}")
    if args.unboost:
        for m in args.unboost:
            boosted.discard(m)
        save_boosted(boosted)
        print(f"Unboosted: {', '.join(args.unboost)}")
    if args.boost or args.unboost:
        sys.exit(0)
        if not (args.show_ranking or args.set_weight or args.set_priority
                or args.set_missing_release is not None or args.model):
            if not (args.dry_run or args.apply or args.providers or args.full
                    or args.top or args.json or args.show_history):
                sys.exit(0)
    if args.show_history:
        show_promotion_history(load_promotion_history())
        sys.exit(0)
    if args.show_response_times:
        show_response_times()
        sys.exit(0)

    # --- ranking matrix management (no probing) ---
    if args.show_ranking or args.set_weight or args.set_priority or args.set_missing_release is not None:
        cfg, yaml_mod = load_config()
        if not isinstance(cfg, dict):
            cfg = {}
        before_ids = chain_delta.chain_ids_from_config(cfg) if chain_delta is not None else []
        mr = cfg.setdefault("model_ranking", {})
        if not isinstance(mr, dict):
            mr = {}
            cfg["model_ranking"] = mr
        if not isinstance(mr.get("weights"), dict):
            mr["weights"] = dict(DEFAULT_RANKING_WEIGHTS)
        else:
            merged_w = dict(DEFAULT_RANKING_WEIGHTS)
            merged_w.update(mr["weights"])
            mr["weights"] = merged_w
        mr.setdefault("provider_priority", dict(DEFAULT_PROVIDER_PRIORITY))
        mr.setdefault("missing_release_date", 0.0)

        if args.set_weight:
            for item in args.set_weight:
                if "=" not in item:
                    print(f"ERROR: expected KEY=VALUE, got {item!r}", file=sys.stderr)
                    sys.exit(2)
                k, v = item.split("=", 1)
                try:
                    mr["weights"][k.strip()] = float(v)
                except ValueError:
                    print(f"ERROR: invalid number {v!r}", file=sys.stderr)
                    sys.exit(2)
        if args.set_priority:
            for item in args.set_priority:
                if "=" not in item:
                    print(f"ERROR: expected PROVIDER=VALUE, got {item!r}", file=sys.stderr)
                    sys.exit(2)
                k, v = item.split("=", 1)
                try:
                    mr["provider_priority"][k.strip()] = float(v)
                except ValueError:
                    print(f"ERROR: invalid number {v!r}", file=sys.stderr)
                    sys.exit(2)
        if args.set_missing_release is not None:
            mr["missing_release_date"] = float(args.set_missing_release)

        # normalize weights for display
        w = mr.get("weights", {})
        try:
            total = sum(float(x) for x in w.values())
        except (TypeError, ValueError):
            total = 0.0
        if total > 0:
            norm = {k: round(float(v) / total, 4) for k, v in w.items()}
        else:
            norm = dict(w)

        print("Ranking matrix:")
        print("  weights (raw):", w)
        print("  weights (normalized):", norm)
        print("  provider_priority:", mr.get("provider_priority", {}))
        print("  missing_release_date:", mr.get("missing_release_date"))

        if (args.set_weight or args.set_priority or args.set_missing_release is not None) and not args.dry_run:
            try:
                import atomic_io
                import io
                buf = io.StringIO()
                yaml_mod.safe_dump(cfg, buf, default_flow_style=False, sort_keys=False, allow_unicode=True)
                atomic_io.atomic_write_text(paths.CONFIG_PATH, buf.getvalue(), encoding="utf-8")
                print(f"Updated {paths.CONFIG_PATH}")
            except Exception as e:
                print(f"ERROR writing config: {e}", file=sys.stderr)
                sys.exit(1)
        elif (args.set_weight or args.set_priority or args.set_missing_release is not None) and args.dry_run:
            print("DRY RUN -- config not written.")
        if chain_delta is not None:
            last = load_last_selection()
            if last:
                ranking = build_ranking_from_mr(mr)
                models = [dict(m) for m in last if isinstance(m, dict)]
                ranked = rank_models(models, ranking)
                after_ids = [m.get("id") if isinstance(m, dict) else str(m) for m in ranked]
                print("\nDraft delta (cached selection re-ranked):")
                print(chain_delta.format_delta(before_ids, after_ids))
            else:
                print("\n(no cached selection to re-rank; run a full pass first)")
        sys.exit(0)

    dry_run = args.dry_run

    if args.full:
        top_n = None
    else:
        top_n = args.top if args.top else 3

    user_list = None
    if args.providers:
        user_list = [x.strip() for x in args.providers.split(",") if x.strip()]

    import atomic_io
    with atomic_io.file_lock(str(paths.CACHE_PATH) + '.lock'):
        cache = load_cache()
        ranking = load_ranking_config()
        configured = providers.configured_providers(user_list=user_list, prefer_local=args.prefer_local)

    # Hint about optional provider keys that are missing.
    missing_key_providers = []
    for slug, cfg in providers.PROVIDERS.items():
        if user_list is not None and slug not in user_list:
            continue
        env_any = cfg.get("env_any", [])
        if env_any and not any(os.environ.get(k) for k in env_any):
            missing_key_providers.append((slug, cfg.get("display", slug), env_any))
    if missing_key_providers:
        print("Optional provider keys not set (skipping those providers):")
        for slug, display, envs in missing_key_providers:
            print(f"  {display} ({slug}): set one of {', '.join(envs)}")
        print("  Run Set-EnvKeys.ps1 to configure them securely, then re-run with --refresh.")

    if args.model:
        print("Targeted retest:")
        for slug in configured:
            cfg = providers.PROVIDERS[slug]
            print(f"\n=== {cfg['display']} ({slug}) ===")
            ok, vnote = providers.validate_key(cfg)
            if not ok:
                print(f"  key validation failed: {vnote}")
                continue
            key, _ = providers.get_api_key(cfg)
            for mid in args.model:
                if not cfg.get("probe", True):
                    print(f"  {mid}: unverified (listing-only provider)")
                    continue
                if args.refresh:
                    cr = None
                else:
                    cr = cached_result(cache, slug, mid, False)
                if cr is not None:
                    cached_passed, cached_note, cached_retried = cr
                    passed, note, retried = cached_passed, cached_note, cached_retried
                    note = f"[cache] {note}"
                else:
                    passed, note, retried = probe(slug, cfg, mid, key, timeout=cfg.get("probe_timeout"), reasoning=reasoning_mode)
                    put_cache(cache, slug, mid, passed, note, retried)
                clean = bool(passed) and not bool(retried)
                print(f"  {mid}: {'PASS' if clean else 'FAIL'} ({note})")
        import atomic_io
        with atomic_io.file_lock(str(paths.CACHE_PATH) + '.lock'):
            save_cache(cache)
        sys.exit(0)

    all_passing = []
    probed_all = []
    summary = []

    banned_set = banned
    cache_lock = threading.Lock()
    print_lock = threading.Lock()

    # Parallelize across providers: one worker per provider keeps each provider's
    # quota isolated; shared cache/banned access is guarded by locks. Results are
    # merged on the main thread so config/cache writes stay serialized.
    provider_results = {}
    done = 0
    total_providers = len(configured)
    with ThreadPoolExecutor(max_workers=max(1, len(configured))) as ex:
        futures = {}
        for slug in configured:
            cfg = providers.PROVIDERS[slug]
            futures[ex.submit(_process_provider, slug, cfg,
                             ranking=ranking, reasoning_mode=reasoning_mode,
                             top_n=top_n, args=args,
                             banned=banned_set, cache=cache, cache_lock=cache_lock,
                             print_lock=print_lock)] = slug
        for fut in as_completed(futures):
            slug = futures[fut]
            try:
                passing_list, probed_list, summary_entry = fut.result()
            except Exception as e:
                print(f"  provider {slug} failed: {e}", file=sys.stderr)
                passing_list, probed_list, summary_entry = [], [], {"provider": slug, "fetched": 0, "passed": 0, "error": str(e)}
            all_passing.extend(passing_list)
            probed_all.extend(probed_list)
            summary.append(summary_entry)
            done += 1
            print(f"  [{done}/{total_providers} providers complete]")

    import atomic_io
    with atomic_io.file_lock(str(paths.CACHE_PATH) + '.lock'):
        save_cache(cache)

    if getattr(args, "capability", None):
        cap = args.capability.lower().strip()
        all_passing = [m for m in all_passing if cap in (m.get("_capabilities") or set())]

    all_passing = rank_models(all_passing, ranking)

    # --- promotion gate ---
    margin = args.promotion_margin if args.promotion_margin is not None else PROMOTION_MARGIN
    hyst = args.hysteresis_days if args.hysteresis_days is not None else HYSTERESIS_DAYS
    emergency = not args.no_emergency
    promote = not getattr(args, "no_promote", False)
    import atomic_io
    with atomic_io.file_lock(str(paths.PROMOTION_HISTORY_PATH) + '.lock'):
        history = load_promotion_history()
        today = getattr(args, "today", None) or datetime.now(timezone.utc).date().isoformat()
        # validate
        try:
            date.fromisoformat(today)
        except ValueError:
            print(f"ERROR: --today expects YYYY-MM-DD, got '{today}'")
            sys.exit(2)

        dry = args.dry_run
        if not args.dry_run and not args.apply:
            dry = False
        if args.dry_run:
            dry = True

        if not dry:
            update_promotion_history(history, probed_all, today)
            removed = prune_promotion_history(history, today)
            if removed:
                print(f"  Pruned {len(removed)} stale history entr(ies): {', '.join(removed)}")
            save_promotion_history(history)

        cfg0, _ = load_config()
        current_default = ""
        if isinstance(cfg0, dict):
            mc = cfg0.get("model")
            if isinstance(mc, dict):
                current_default = str(mc.get("default") or "")
        chosen, reason = (None, "--no-promote: chain only") if not promote else choose_default(
            all_passing, probed_all, history, current_default, margin, hyst, emergency=emergency)
        if getattr(args, "set_default", None):
            forced = None
            for m in all_passing:
                if str(m.get("id")) == str(args.set_default):
                    forced = m
                    break
            if forced is not None:
                chosen = forced
                reason = f"forced by --set-default: {args.set_default}"
                print(f"\nPromotion gate: {reason}")
            else:
                print(f"\n--set-default: {args.set_default} not in passing pool; gate unchanged")
        elif promote:
            print(f"\nPromotion gate: {reason}")

        if not dry and promote and chosen is not None:
            mid = _model_id(chosen)
            rec = history.setdefault(mid, _blank_promo_record(today))
            # ensure fields
            for k, v in _blank_promo_record(today).items():
                rec.setdefault(k, v)
            if current_default != mid:
                rec["promotion_count"] = rec.get("promotion_count", 0) + 1
                rec["promoted_at"] = today
            save_promotion_history(history)

    save_last_selection(all_passing)

    print("\n" + "=" * 70)
    print(f"Passing models ({len(all_passing)}):")
    print("=" * 70)
    for i, m in enumerate(all_passing, 1):
        rt = m.get("_response_time_ms")
        rt_s = f"  resp={rt}ms" if isinstance(rt, int) else ""
        print(f"  {i}. [{m['_provider']}] {m['id']}  ctx={m.get('context_window')}  score={m.get('_score')}  release={m.get('_release_date')}{rt_s}")

    if args.json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
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
                    "response_time_ms": m.get("_response_time_ms"),
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
        if chosen is not None:
            print(f"  model.default -> {chosen['id']}  ({reason})")
        elif current_default:
            print(f"  model.default -> {current_default}  (unchanged; gate kept incumbent)")
        elif all_passing:
            print(f"  model.default -> {all_passing[0]['id']}")
        eff_id = _model_id(chosen) if chosen is not None else current_default
        for m in all_passing:
            if eff_id and _model_id(m) == eff_id:
                continue
            print(f"  fallback: {m['id']} ({m['_provider']})")
        sys.exit(0)

    import yaml
    write_config(all_passing, yaml, chosen=chosen, current_default=current_default, promote=promote,
                 probed_all=probed_all)
    print("Done.")