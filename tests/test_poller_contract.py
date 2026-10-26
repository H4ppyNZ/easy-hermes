"""Contract tests for pollers: module API surface (no network)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POLLERS = ["poll_openrouter", "poll_nvidia", "poll_gemini",
           "poll_ollama_cloud", "poll_novita", "poll_nous"]


def test_poller_contract():
    import importlib
    for name in POLLERS:
        m = importlib.import_module(name)
        assert callable(getattr(m, "poll", None)), f"{name}: poll missing"
        assert callable(getattr(m, "fetch_models", None)), f"{name}: fetch_models missing"
        assert callable(getattr(m, "normalize_model", None)), f"{name}: normalize_model missing"
        assert callable(getattr(m, "is_free_model", None)), f"{name}: is_free_model missing"
        # poll must never raise on no-network when called with no args? best-effort
        # (some require creds; allow empty/exception-free? contract says never raises)
        try:
            res = m.poll()
        except Exception as e:
            raise AssertionError(f"{name}.poll() raised: {e}")
        assert isinstance(res, list), f"{name}.poll() did not return list"


def test_provider_registry_fetch_fn():
    import providers
    for slug, cfg in providers.PROVIDERS.items():
        if slug == "ollama-local":
            continue
        assert "fetch_fn" in cfg, f"{slug}: missing fetch_fn"
        mod = __import__(cfg["module"])
        assert callable(getattr(mod, cfg["fetch_fn"], None)), f"{slug}: fetch_fn not callable"
