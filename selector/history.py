"""selector.history — extracted from select-models.py

Split out of the original monolithic select-models.py.  Function bodies are
moved verbatim; only module-level path state is routed through
``selector.paths`` so it stays mutable at runtime.
"""
from __future__ import annotations

from selector import paths
from selector.paths import HISTORY_PRUNE_DAYS
import json
import re
import sys
from datetime import date
from selector.paths import load_last_selection


def _model_id(m):
    if isinstance(m, dict):
        return str(m.get("id") or m.get("model") or m.get("name"))
    return str(m)


def _release_date(m):
    if isinstance(m, dict):
        return m.get("_release_date") or m.get("release_date")
    return None


def load_promotion_history():
    try:
        if paths.PROMOTION_HISTORY_PATH.exists():
            data = json.loads(paths.PROMOTION_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_promotion_history(history):
    try:
        import atomic_io
        atomic_io.atomic_write_json(paths.PROMOTION_HISTORY_PATH, history, indent=2)
    except Exception as e:
        print(f"  (promotion history write failed: {e})", file=sys.stderr)


def _blank_promo_record(today):
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


def update_promotion_history(history, probed, today):
    seen = set()
    for m in probed:
        mid = m["model"]
        seen.add(mid)
        rec = history.setdefault(mid, _blank_promo_record(today))
        for k, v in _blank_promo_record(today).items():
            rec.setdefault(k, v)
        rec["last_seen"] = today
        rec["total_runs"] += 1
        if m.get("test_passed"):
            rec["last_pass"] = today
            if m.get("clean_pass"):
                rec["clean_runs"] += 1
                if rec.get("last_clean") != today:
                    rec["consecutive_clean_passes"] += 1
                    rec["last_clean"] = today
            else:
                rec["unclean_runs"] += 1
                rec["consecutive_clean_passes"] = 0
        else:
            rec["failed_runs"] += 1
            rec["consecutive_clean_passes"] = 0
            rec["last_fail"] = today
            rec["last_fail_note"] = (m.get("fail_note") or "")[:120]
    for mid, rec in history.items():
        if mid not in seen:
            if rec.get("consecutive_clean_passes"):
                rec["consecutive_clean_passes"] = 0
            rec["missed_runs"] = rec.get("missed_runs", 0) + 1


def prune_promotion_history(history, today):
    cutoff = date.fromisoformat(today).toordinal() - HISTORY_PRUNE_DAYS
    removed = []
    for mid in list(history):
        last = (history[mid].get("last_seen") or "")[:10]
        try:
            if date.fromisoformat(last).toordinal() < cutoff:
                removed.append(mid)
                del history[mid]
        except ValueError:
            removed.append(mid)
            del history[mid]
    return removed


def show_promotion_history(history):
    if not history:
        print("Promotion history is empty (no runs recorded yet).")
        return
    print(f"\n{'=' * 104}")
    print("PROMOTION HISTORY  (consecutive_clean_passes is what the gate reads)")
    print(f"{'=' * 104}")
    print(f"  {'model':<42} {'streak':>6} {'runs':>5} {'clean':>6} {'uncln':>6} "
          f"{'fail':>5} {'miss':>5} {'prom':>5}  last_clean")
    print(f"  {'-' * 100}")
    for mid in sorted(history, key=lambda k: (-history[k].get("consecutive_clean_passes", 0), k)):
        r = history[mid]
        print(f"  {mid:<42} {r.get('consecutive_clean_passes', 0):>6} "
              f"{r.get('total_runs', 0):>5} {r.get('clean_runs', 0):>6} "
              f"{r.get('unclean_runs', 0):>6} {r.get('failed_runs', 0):>5} "
              f"{r.get('missed_runs', 0):>5} {r.get('promotion_count', 0):>5}  "
              f"{r.get('last_clean') or '-'}")
    print(f"{'=' * 104}\n")


def show_response_times():
    """Print recorded probe response times (ms) from the probe cache + last selection."""
    rows = []
    seen = set()
    try:
        if paths.CACHE_PATH.exists():
            cache = json.loads(paths.CACHE_PATH.read_text(encoding="utf-8"))
            entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
            for key, v in entries.items():
                if not isinstance(v, dict):
                    continue
                note = v.get("note")
                ms_list = re.findall(r"(\d+)\s*ms", str(note)) if note else []
                if not ms_list:
                    continue
                try:
                    ms = int(ms_list[-1])
                except (TypeError, ValueError):
                    continue
                if "::" in key:
                    prov, model = key.split("::", 1)
                else:
                    prov, model = "", key
                dedup_key = (prov, model)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                rows.append((ms, prov, model, bool(v.get("passed"))))
    except Exception as e:
        print(f"  (cache read failed: {e})", file=sys.stderr)
    last = load_last_selection()
    if isinstance(last, list):
        for m in last:
            rt = m.get("_response_time_ms")
            if not isinstance(rt, int):
                continue
            prov = m.get("_provider") or ""
            model = m.get("id") or ""
            dedup_key = (prov, model)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            rows.append((rt, prov, model, True))
    if not rows:
        print("No recorded response times found.")
        return
    rows.sort(key=lambda r: r[0])
    print("\n" + "=" * 90)
    print(f"RECORDED RESPONSE TIMES  ({len(rows)} models, sorted fastest first)")
    print("=" * 90)
    print(f"  {'ms':>7}  {'status':<6}  {'provider':<14}  model")
    print(f"  {'-' * 86}")
    for ms, prov, model, passed in rows:
        status = "PASS" if passed else "FAIL"
        print(f"  {ms:>7}  {status:<6}  {prov:<14}  {model}")
    print("=" * 90 + "\n")


def promo_streak(history, model_id):
    return int(history.get(model_id, {}).get("consecutive_clean_passes", 0))


def choose_default(passed, probed_all, history, current_default, margin, hysteresis_days, emergency=True):
    if not passed:
        return None, "no models passed probing -- leaving model.default unchanged"

    by_date = sorted(passed, key=lambda m: (_release_date(m) or "1970-01-01",
                                          promo_streak(history, _model_id(m)), _model_id(m)),
                      reverse=True)

    probed_ids = {_model_id(m) for m in probed_all}
    passed_ids = {_model_id(m) for m in passed}
    incumbent_in_scope = current_default in probed_ids
    incumbent_passed = current_default in passed_ids
    incumbent_dead = incumbent_in_scope and not incumbent_passed
    incumbent_streak = promo_streak(history, current_default)
    incumbent_healthy = incumbent_in_scope and incumbent_passed and incumbent_streak >= margin

    eligible = [m for m in by_date if promo_streak(history, _model_id(m)) >= margin]

    if incumbent_in_scope:
        inc_state = "healthy" if incumbent_healthy else ("DEAD (failed probing)" if incumbent_dead
                                                         else f"in scope, streak {incumbent_streak}")
    else:
        inc_state = "out of scope (not a free candidate)"
    print(f"\n{'─' * 80}")
    print(f"PROMOTION GATE  (margin={margin} clean pass{'es' if margin != 1 else ''}, "
          f"hysteresis={hysteresis_days}d)")
    print(f"{'─' * 80}")
    print(f"  incumbent: {current_default or '(none)'}  [{inc_state}]")
    print(f"  {'model':<42} {'streak':>6}  eligible")
    for m in by_date:
        s = promo_streak(history, _model_id(m))
        mark = "yes" if s >= margin else f"no (needs {margin - s} more)"
        star = " <- incumbent" if _model_id(m) == current_default else ""
        print(f"  {_model_id(m):<42} {s:>6}  {mark}{star}")
    print(f"{'─' * 80}")

    # first-run bootstrap: no incumbent at all
    if not current_default:
        if eligible:
            w = eligible[0]
            return w, (f"first run: promoting best eligible '{_model_id(w)}' "
                       f"(streak {promo_streak(history, _model_id(w))})")
        if passed:
            w = by_date[0]
            return w, (f"first run: promoting best available '{_model_id(w)}' (bootstrap)")

    # Case 1: incumbent genuinely dead
    if incumbent_dead:
        if eligible:
            w = eligible[0]
            return w, (f"incumbent '{current_default}' FAILED probing this run; "
                       f"promoting best eligible '{_model_id(w)}' "
                       f"(streak {promo_streak(history, _model_id(w))})")
        if emergency:
            w = by_date[0]
            return w, (f"EMERGENCY: incumbent '{current_default}' failed probing and no model "
                       f"has {margin} clean passes; promoting best available "
                       f"'{_model_id(w)}' (streak {promo_streak(history, _model_id(w))})")
        return None, (f"incumbent '{current_default}' failed and nothing reached {margin} clean "
                      "passes -- leaving model.default unchanged")

    # Case 2: nothing earned the slot
    if not eligible:
        return None, (f"no model has {margin} clean passes yet -- leaving model.default "
                      f"'{current_default}' unchanged")

    # Case 3: qualified challenger
    challenger = next((m for m in eligible if _model_id(m) != current_default), None)
    if challenger is None:
        return None, (f"'{current_default}' is the newest qualified model -- unchanged")

    incumbent_entry = next((m for m in passed if _model_id(m) == current_default), None)
    if incumbent_entry:
        inc_date = _release_date(incumbent_entry) or "1970-01-01"
        ch_date = _release_date(challenger) or "1970-01-01"
        try:
            days_newer = (date.fromisoformat(ch_date) - date.fromisoformat(inc_date)).days
        except ValueError:
            days_newer = 0
        threshold = max(1, hysteresis_days)
        if days_newer < threshold:
            why = "not newer" if days_newer <= 0 else f"only {days_newer}d newer (needs {threshold}d)"
            return None, (f"keeping '{current_default}': challenger '{_model_id(challenger)}' is {why}")

    return challenger, (f"promoting '{_model_id(challenger)}' over '{current_default}': "
                        f"streak {promo_streak(history, _model_id(challenger))}"
                        + ("" if incumbent_in_scope else " (incumbent out of scope)"))