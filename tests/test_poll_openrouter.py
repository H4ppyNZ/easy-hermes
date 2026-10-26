import io
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import poll_openrouter as po


def _fake_response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


SAMPLE_FREE = {
    "id": "foo/bar:free",
    "name": "Foo Bar (free)",
    "context_length": 12345,
    "pricing": {"prompt": "0", "completion": "0"},
    "top_provider": {"context_length": 12345},
    "per_request_limits": None,
}

SAMPLE_PAID = {
    "id": "paid/model",
    "name": "Paid Model",
    "context_length": 8192,
    "pricing": {"prompt": "0.001", "completion": "0.002"},
}

SAMPLE_FREE_NO_SUFFIX = {
    "id": "google/lyria-3-clip-preview",
    "name": "Lyria Clip",
    "context_length": 1024,
    "pricing": {"prompt": 0, "completion": 0.0},
}

SAMPLE_FREE_SUFFIX_PRICING_MISSING = {
    "id": "x/y:free",
    "name": "Y",
    "context_length": 4096,
    # no pricing
}


def test_is_free_filter():
    assert po.is_free_model(SAMPLE_FREE) is True
    assert po.is_free_model(SAMPLE_PAID) is False
    assert po.is_free_model(SAMPLE_FREE_NO_SUFFIX) is True
    assert po.is_free_model(SAMPLE_FREE_SUFFIX_PRICING_MISSING) is True
    # pricing one side nonzero -> not free unless suffix
    half = {"id": "a/b", "pricing": {"prompt": "0", "completion": "0.001"}}
    assert po.is_free_model(half) is False


def test_normalize_model():
    norm = po.normalize_model(SAMPLE_FREE)
    assert norm == {
        "id": "foo/bar:free",
        "display_name": "Foo Bar (free)",
        "context_window": 12345,
        "created": None,
        "rate_limits": None,
        "notes": "Free model per OpenRouter listing; rate limits are not provided "
                 "by the listing endpoint (per_request_limits=null).",
    }
    # top_provider fallback for context
    m = {"id": "z", "name": "Z", "pricing": {"prompt": "0", "completion": "0"},
         "top_provider": {"context_length": 999}}
    assert po.normalize_model(m)["context_window"] == 999


def test_fetch_all_models_single_page():
    payload = {"data": [SAMPLE_FREE, SAMPLE_PAID, SAMPLE_FREE_NO_SUFFIX],
               "total_count": 3, "links": {"next": None}}
    with patch("poll_openrouter.urllib.request.urlopen",
               return_value=_fake_response(payload)):
        with patch("poll_openrouter.time.sleep"):
            models = po.fetch_all_models()
    assert len(models) == 3
    assert models[0]["id"] == "foo/bar:free"


def test_fetch_all_models_pagination():
    page1 = {"data": [SAMPLE_FREE], "links": {"next": "https://openrouter.ai/api/v1/models?offset=1"}}
    page2 = {"data": [SAMPLE_PAID], "links": {"next": None}}
    responses = [_fake_response(page1), _fake_response(page2)]
    with patch("poll_openrouter.urllib.request.urlopen", side_effect=responses):
        with patch("poll_openrouter.time.sleep"):
            models = po.fetch_all_models()
    assert [m["id"] for m in models] == ["foo/bar:free", "paid/model"]


def test_poll_filters_and_normalizes():
    payload = {"data": [SAMPLE_FREE, SAMPLE_PAID, SAMPLE_FREE_NO_SUFFIX],
               "links": {"next": None}}
    with patch("poll_openrouter.urllib.request.urlopen",
               return_value=_fake_response(payload)):
        with patch("poll_openrouter.time.sleep"):
            out = po.poll()
    assert len(out) == 2
    ids = {m["id"] for m in out}
    assert ids == {"foo/bar:free", "google/lyria-3-clip-preview"}
    for m in out:
        assert set(m.keys()) == {"id", "display_name", "context_window", "created", "rate_limits", "notes"}
        assert m["rate_limits"] is None


def test_http_429_retry_then_success():
    err = __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
        "http://x", 429, "Too Many", {}, None)
    # make read work
    err.read = lambda: b"rate limited"
    responses = [err, _fake_response({"data": [], "links": {"next": None}})]
    with patch("poll_openrouter.urllib.request.urlopen", side_effect=responses):
        with patch("poll_openrouter.time.sleep") as sl:
            data, errstr = po.http_get_json("http://example.com", backoff=(2, 5))
    assert errstr is None
    assert data == {"data": [], "links": {"next": None}}
    # slept once for retry
    assert sl.call_count == 1


def test_empty_results():
    payload = {"data": [], "links": {"next": None}}
    with patch("poll_openrouter.urllib.request.urlopen",
               return_value=_fake_response(payload)):
        with patch("poll_openrouter.time.sleep"):
            out = po.poll()
    assert out == []


def test_auth_header_when_key_present(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        return _fake_response({"data": [], "links": {"next": None}})

    with patch("poll_openrouter.urllib.request.urlopen", side_effect=fake_urlopen):
        with patch("poll_openrouter.time.sleep"):
            po.fetch_all_models()
    assert captured["auth"] == "Bearer sk-test"


def test_no_crash_on_fetch_error():
    import urllib.error
    with patch("poll_openrouter.urllib.request.urlopen",
               side_effect=urllib.error.URLError("boom")):
        with patch("poll_openrouter.time.sleep"):
            out = po.poll()
    assert out == []


def test_main_json_writes_file(tmp_path):
    payload = {"data": [SAMPLE_FREE], "links": {"next": None}}
    with patch("poll_openrouter.urllib.request.urlopen",
               return_value=_fake_response(payload)):
        with patch("poll_openrouter.time.sleep"):
            rc = po.main(["--json", "--out", str(tmp_path)])
    assert rc == 0
    out_file = tmp_path / "openrouter.json"
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["count"] == 1
