"""Reusable helpers for computing the delta between two model chains.

A "chain" is an ordered list of model ids (primary first, then fallbacks).
These helpers are intentionally pure (no I/O, no global state) so they can be
reused by the selector and by future phases (drafting, apply, reporting).
"""
from __future__ import annotations


def _as_ids(chain) -> list[str]:
    """Coerce a chain of ids/dicts/config entries into a list of id strings."""
    out: list[str] = []
    if chain is None:
        return out
    if isinstance(chain, str):
        return [chain] if chain else out
    if not hasattr(chain, "__iter__"):
        return out
    for item in chain:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("model") or item.get("name")
            if mid is not None:
                out.append(str(mid))
        else:
            out.append(str(item))
    return out


def compute_delta(before, after) -> dict:
    """Return a structured delta between two ordered chains.

    before/after may be lists of ids or dicts (see _as_ids). Result keys:
      - before: list[str]
      - after:  list[str]
      - added:   list[str]   (in `after`, not in `before`, in new order)
      - removed: list[str]   (in `before`, not in `after`, in old order)
      - moved:   list[dict]  {id, from, to} where positions changed
      - unchanged: list[str] (same id, same position)
    """
    b = _as_ids(before)
    a = _as_ids(after)
    b_index = {mid: i for i, mid in enumerate(b)}
    a_index = {mid: i for i, mid in enumerate(a)}

    added = [mid for mid in a if mid not in b_index]
    removed = [mid for mid in b if mid not in a_index]
    moved = []
    unchanged = []
    for mid in a:
        if mid in b_index:
            if b_index[mid] != a_index[mid]:
                moved.append({"id": mid, "from": b_index[mid], "to": a_index[mid]})
            else:
                unchanged.append(mid)

    return {
        "before": b,
        "after": a,
        "added": added,
        "removed": removed,
        "moved": moved,
        "unchanged": unchanged,
    }


def format_delta(before, after, *, label_before="before", label_after="after") -> str:
    """Human-readable delta. Omits unchanged entries; shows moves/adds/removes."""
    d = compute_delta(before, after)
    lines: list[str] = []
    if not d["before"] and not d["after"]:
        return "(empty chains)"
    if d["before"] != d["after"]:
        if d["before"]:
            lines.append(f"{label_before}: " + " -> ".join(d["before"]))
        if d["after"]:
            lines.append(f"{label_after}:  " + " -> ".join(d["after"]))
    if d["before"] and d["after"] and d["before"][0] != d["after"][0]:
        lines.append(f"primary: {d['before'][0]} -> {d['after'][0]}")
    for m in d["moved"]:
        arrow = "up" if m["to"] < m["from"] else "down"
        lines.append(f"  {arrow}: {m['id']}  pos {m['from'] + 1} -> {m['to'] + 1}")
    for mid in d["added"]:
        pos = d["after"].index(mid) + 1
        lines.append(f"  +: {mid}  (new at pos {pos})")
    for mid in d["removed"]:
        pos = d["before"].index(mid) + 1
        lines.append(f"  -: {mid}  (was pos {pos})")
    if not d["moved"] and not d["added"] and not d["removed"]:
        lines.append("  (no change)")
    lines.append(f"  unchanged: {len(d['unchanged'])}")
    return "\n".join(lines)


def chain_ids_from_config(cfg) -> list[str]:
    """Extract the ordered model-id chain from a Hermes config dict."""
    if not isinstance(cfg, dict):
        return []
    out: list[str] = []
    mcfg = cfg.get("model")
    if isinstance(mcfg, dict):
        d = mcfg.get("default")
        if d:
            out.append(str(d))
    elif isinstance(mcfg, str) and mcfg:
        out.append(mcfg)
    fb = cfg.get("fallback_providers")
    if isinstance(fb, list):
        for e in fb:
            if isinstance(e, dict):
                mid = e.get("model")
                if mid:
                    out.append(str(mid))
            elif isinstance(e, str):
                out.append(e)
    return out
