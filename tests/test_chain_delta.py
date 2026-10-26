"""Tests for chain_delta helpers (no network)."""
import chain_delta


def test_compute_delta_added_removed_moved():
    before = ["a", "b", "c"]
    after = ["b", "a", "d"]
    d = chain_delta.compute_delta(before, after)
    assert d["added"] == ["d"]
    assert d["removed"] == ["c"]
    assert {"id": "a", "from": 0, "to": 1} in d["moved"]
    assert {"id": "b", "from": 1, "to": 0} in d["moved"]
    assert d["unchanged"] == []


def test_format_delta_no_change():
    chain = ["x", "y"]
    out = chain_delta.format_delta(chain, chain)
    assert "no change" in out
    assert "unchanged: 2" in out


def test_chain_ids_from_config():
    cfg = {"model": {"default": "primary"}, "fallback_providers": [{"model": "fb1"}, "fb2"]}
    assert chain_delta.chain_ids_from_config(cfg) == ["primary", "fb1", "fb2"]


def test_as_ids_dicts():
    assert chain_delta._as_ids([{"id": "m1"}, {"model": "m2"}]) == ["m1", "m2"]