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
    netloc = p.netloc.lower()
    # normalize default ports
    if netloc.endswith(":80") and p.scheme.lower() == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and p.scheme.lower() == "https":
        netloc = netloc[:-4]
    return (p.scheme.lower(), netloc)


def same_origin(url, base):
    """True if url is relative or shares scheme+host with base."""
    if not isinstance(url, str):
        return False
    try:
        p = urllib.parse.urlsplit(url.strip())
    except Exception:
        return False
    # Relative (no netloc): resolves against base -> allow only if base is a valid http(s) origin.
    if not p.netloc:
        if p.scheme and p.scheme.lower() not in ("http", "https", ""):
            return False
        b = origin_of(base)
        return b is not None and b[0] in ("http", "https")
    # Absolute or scheme-relative: require matching origin.
    b = origin_of(base)
    if b is None:
        return False
    if not p.netloc:
        return False
    u = origin_of(url)
    if u is None:
        return False
    return u == b


def safe_next(nxt, base):
    """Return nxt if it is same-origin as base, else None."""
    if not nxt:
        return None
    if not same_origin(nxt, base):
        return None
    return nxt
