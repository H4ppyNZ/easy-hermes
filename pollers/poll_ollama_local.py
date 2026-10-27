"""Poller adapter for local Ollama (wraps hermes-ollama-selector)."""
from __future__ import annotations
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "hermes_ollama_selector", Path(__file__).resolve().parent.parent / "hermes-ollama-selector.py")
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

fetch_models = _MOD.fetch_models
probe_model = _MOD.probe_model
resolve_base_url = _MOD.resolve_base_url
openai_base = _MOD.openai_base
param_sort_key = getattr(_MOD, "param_sort_key", None)


def poll(*a, **kw):
    base = kw.pop("base_url", None) or resolve_base_url(kw.get("base_url_arg"))
    return fetch_models(base)