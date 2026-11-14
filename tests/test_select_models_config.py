"""Tests for select-models config writing (no network).

NOTE: config path state lives in ``selector.paths`` (the implementation reads
it from there), so tests patch ``selector.paths.CONFIG_PATH`` — not the
``select-models.py`` facade attribute.  Patching the facade attribute alone
does NOT redirect the write and would clobber the real Hermes config.
"""
import os
import sys
import tempfile
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util
_spec = importlib.util.spec_from_file_location("select_models", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "select-models.py"))
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)

from selector import paths as sel_paths


def _passing():
    return [
        {"id": "primary/model:free", "_provider": "openrouter", "_base_url": None,
         "_release_date": "2026-01-01", "_score": 0.5, "_reasoning": 0.0},
        {"id": "fallback/model:free", "_provider": "nous", "_base_url": None,
         "_release_date": None, "_score": 0.2, "_reasoning": 0.0},
    ]


def test_write_config_writes_default_and_fallbacks(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model: {}\n", encoding="utf-8")
    monkeypatch.setattr(sel_paths, "CONFIG_PATH", cfg_path)
    sm.write_config(_passing(), yaml)
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["model"]["default"] == "primary/model:free"
    assert data["model"]["provider"] == "openrouter"
    assert any(e.get("model") == "fallback/model:free" for e in data["fallback_providers"])


def test_write_config_merges_existing_fallbacks(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "model:\n  default: old\nfallback_providers:\n  - {provider: custom, model: existing}\n",
        encoding="utf-8")
    monkeypatch.setattr(sel_paths, "CONFIG_PATH", cfg_path)
    sm.write_config(_passing(), yaml)
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    models = [e["model"] for e in data["fallback_providers"]]
    assert "existing" in models
    assert "fallback/model:free" in models


def test_write_config_empty_is_noop(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    original = "model:\n  default: keepme\n"
    cfg_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(sel_paths, "CONFIG_PATH", cfg_path)
    sm.write_config([], yaml)
    assert cfg_path.read_text(encoding="utf-8") == original
