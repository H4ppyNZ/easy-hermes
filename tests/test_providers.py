"""Tests for providers.validate_key (no network)."""
import urllib.error
import email.message
from unittest.mock import patch, MagicMock

import providers


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body):
        super().__init__("u", code, "msg", email.message.Message(), None)
        self._body = body

    def read(self, n=-1):
        return self._body


def _fake_response():
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = b"{}"
    cm.__exit__.return_value = False
    return cm


def test_missing_required_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NGC_API_KEY", raising=False)
    ok, note = providers.validate_key(providers.PROVIDERS["nvidia"])
    assert ok is False
    assert "missing" in note.lower()


def test_no_key_required_ok(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ok, note = providers.validate_key(providers.PROVIDERS["openrouter"])
    assert ok is True


def test_valid_openai_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    with patch("urllib.request.urlopen", return_value=_fake_response()):
        ok, note = providers.validate_key(providers.PROVIDERS["nvidia"])
    assert ok is True
    assert "key OK" in note


def test_rejected_openai_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-bad")
    with patch("urllib.request.urlopen", side_effect=FakeHTTPError(401, b'{"detail":"invalid"}')):
        ok, note = providers.validate_key(providers.PROVIDERS["nvidia"])
    assert ok is False
    assert "401" in note


def test_valid_gemini_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
    with patch("urllib.request.urlopen", return_value=_fake_response()):
        ok, note = providers.validate_key(providers.PROVIDERS["gemini"])
    assert ok is True
    assert "key OK" in note


def test_rejected_gemini_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-bad")
    with patch("urllib.request.urlopen", side_effect=FakeHTTPError(403, b'{"error":{"message":"invalid"}}')):
        ok, note = providers.validate_key(providers.PROVIDERS["gemini"])
    assert ok is False
    assert "403" in note


def test_non_auth_http_not_key_failure(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    with patch("urllib.request.urlopen", side_effect=FakeHTTPError(404, b"{}")):
        ok, note = providers.validate_key(providers.PROVIDERS["nvidia"])
    assert ok is True
