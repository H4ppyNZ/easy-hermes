"""Shared helpers for safe pagination / origin validation.

Pollers must never follow a server-supplied pagination URL to a different
origin, and must never send credentials cross-origin.
"""
from __future__ import annotations

import urllib.parse


def origin_of(url):
    try:
        p = urllib.parse.urlsplit(str(url))
    except Exception:
        return None
    if not p.scheme or not p.netloc:
        return None
    return (p.scheme.lower(), p.netloc.lower())


def same_origin(url, base):
    """True if url is relative or shares scheme+host with base."""
    u = origin_of(url)
    if u is None:
        return True  # relative / no origin -> resolve against base
    b = origin_of(base)
    if b is None:
        return True
    return u == b


def safe_next(nxt, base):
    """Return nxt if it is same-origin as base, else None."""
    if not nxt:
        return None
    if not same_origin(nxt, base):
        return None
    return nxt
