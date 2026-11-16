"""Tests for the Nous credential expiry guard in selector.runner.

The nightly selector loads the Nous access token straight out of
``<HERMES_HOME>/auth.json``. While that token is expired, every probe result is
stale: ``providers.validate_key`` does not reject a 429, so a benched/expired
token used to be probed anyway and the run reported only cached pass/fail lines.

These tests exercise ``_nous_cred_state`` directly (no auth.json, no network).
"""

import datetime as dt
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load_runner():
    """Load selector/runner.py as a module without executing main()."""
    path = os.path.join(REPO, "selector", "runner.py")
    spec = importlib.util.spec_from_file_location("selector_runner_under_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not build an import spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = _load_runner()
_nous_cred_state = runner._nous_cred_state
NOW = dt.datetime(2026, 11, 16, 12, 0, 0, tzinfo=dt.timezone.utc)


def test_live_token_is_ok():
    cred = {"access_token": "x", "expires_at": "2026-11-16T13:00:00+00:00"}
    state, note = _nous_cred_state(cred, now=NOW)
    assert state == "ok", note


def test_expired_token_is_reported():
    cred = {"access_token": "x", "expires_at": "2026-11-16T11:00:00+00:00"}
    state, note = _nous_cred_state(cred, now=NOW)
    assert state == "expired"
    assert "11:00" in note


def test_naive_timestamp_is_treated_as_utc():
    cred = {"expires_at": "2026-11-16T11:00:00"}  # no tz offset
    assert _nous_cred_state(cred, now=NOW)[0] == "expired"


def test_missing_expires_at_does_not_block_the_provider():
    assert _nous_cred_state({}, now=NOW)[0] == "ok"
    assert _nous_cred_state({"expires_at": None}, now=NOW)[0] == "ok"


def test_unparseable_expires_at_is_usable_not_fatal():
    state, note = _nous_cred_state({"expires_at": "not-a-date"}, now=NOW)
    assert state == "ok"
    assert "unparseable" in note


def test_rate_limit_cooldown_alone_does_not_skip_probing():
    """A 429 cooldown must NOT skip probes: the cache still holds live data."""
    cred = {
        "expires_at": "2026-11-16T13:00:00+00:00",
        "failure_reason": "rate_limit",
        "last_error_reset_at": "2026-11-16T14:00:00+00:00",
    }
    state, note = _nous_cred_state(cred, now=NOW)
    assert state == "ok", note
