import json
import os
from unittest.mock import patch, MagicMock

import poll_nvidia


def _mock_resp(data, headers=None, status=200):
    r = MagicMock()
    r.read.return_value = json.dumps(data).encode()
    r.headers = headers or {}
    r.status = status
    r.getcode.return_value = status
    r.__enter__ = lambda self: self
    r.__exit__ = lambda *a: None
    return r


def test_missing_creds_exits_2(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NGC_API_KEY", raising=False)
    try:
        poll_nvidia.fetch_models()
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("expected SystemExit")


def test_missing_creds_cli(monkeypatch, capsys):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NGC_API_KEY", raising=False)
    try:
        poll_nvidia.main([])
    except SystemExit as e:
        assert e.code == 2


def test_normalize_and_filter(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test")
    payload = {"data": [
        {"id": "meta/llama-3.1-8b-instruct", "context_length": 131072},
        {"id": "nvidia/nv-embedqa-e5-v5", "context_length": 512},
    ]}
    resp = _mock_resp(payload, headers={"x-ratelimit-limit-requests": "40"})
    with patch("poll_nvidia.urlopen", return_value=resp):
        models = poll_nvidia.fetch_models()
    assert len(models) == 2
    assert models[0]["id"] == "meta/llama-3.1-8b-instruct"
    assert models[0]["context_window"] == 131072
    assert models[0]["display_name"] == "meta/llama-3.1-8b-instruct"
    assert models[0]["rate_limits"]["limit"] == "40"
    # non-chat kept
    assert any("Non-chat" in m["notes"] or "embed" in m["id"] for m in models)


def test_empty_listing(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test")
    resp = _mock_resp({"data": []})
    with patch("poll_nvidia.urlopen", return_value=resp):
        models = poll_nvidia.fetch_models()
    assert models == []


def test_pagination(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test")
    r1 = _mock_resp({"data": [{"id": "m1"}], "next": "/v1/models?after=m1"})
    r2 = _mock_resp({"data": [{"id": "m2"}]})
    with patch("poll_nvidia.urlopen", side_effect=[r1, r2]):
        models = poll_nvidia.fetch_models(base_url="https://example.com/v1")
    assert [m["id"] for m in models] == ["m1", "m2"]


def test_429_retry(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test")
    monkeypatch.setattr(poll_nvidia.time, "sleep", lambda x: None)
    r429 = _mock_resp({}, headers={"Retry-After": "0"}, status=429)
    r_ok = _mock_resp({"data": [{"id": "m1"}]})
    with patch("poll_nvidia.urlopen", side_effect=[r429, r_ok]):
        models = poll_nvidia.fetch_models(retries=2)
    assert [m["id"] for m in models] == ["m1"]


def test_json_cli(monkeypatch, capsys):
    monkeypatch.setenv("NVIDIA_API_KEY", "test")
    resp = _mock_resp({"data": [{"id": "m1"}]})
    with patch("poll_nvidia.urlopen", return_value=resp):
        try:
            poll_nvidia.main(["--json"])
        except SystemExit as e:
            assert e.code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert data[0]["id"] == "m1"


def test_ngc_env(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("NGC_API_KEY", "ngc-key")
    assert poll_nvidia.resolve_api_key()[0] == "ngc-key"


def test_context_window_nested(monkeypatch):
    raw = {"id": "x", "metadata": {"context_length": 4096}}
    nm = poll_nvidia.normalize_model(raw)
    assert nm["context_window"] == 4096
