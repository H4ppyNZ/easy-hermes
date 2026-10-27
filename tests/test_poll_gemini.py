import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pollers.poll_gemini as pg


def _http_ok(payload):
    return (200, {}, payload, None)


SAMPLE_GEN = {
    "name": "models/gemini-2.5-flash",
    "displayName": "Gemini 2.5 Flash",
    "inputTokenLimit": 1048576,
    "supportedGenerationMethods": ["generateContent"],
}
SAMPLE_EMBED = {
    "name": "models/text-embedding-004",
    "displayName": "Text Embedding",
    "inputTokenLimit": 2048,
    "supportedGenerationMethods": ["embedContent"],
}
SAMPLE_PAIDISH = {
    "name": "models/gemini-2.5-pro",
    "displayName": "Gemini 2.5 Pro",
    "inputTokenLimit": 1048576,
    "supportedGenerationMethods": ["generateContent", "countTokens"],
}


def test_is_generative_filter():
    assert pg._is_generative(SAMPLE_GEN) is True
    assert pg._is_generative(SAMPLE_EMBED) is False
    assert pg._is_generative({"name": "x"}) is True


def test_filter_models():
    out = pg.filter_models([SAMPLE_GEN, SAMPLE_EMBED, SAMPLE_PAIDISH, "bad"])
    assert [m["name"] for m in out] == ["models/gemini-2.5-flash", "models/gemini-2.5-pro"]


def test_normalize_model():
    norm = pg.normalize_model(SAMPLE_GEN)
    assert norm["id"] == "models/gemini-2.5-flash"
    assert norm["display_name"] == "Gemini 2.5 Flash"
    assert norm["context_window"] == 1048576
    assert norm["rate_limits"] is None
    assert "unverified" in norm["notes"]


def test_missing_creds_raises():
    for v in pg.AUTH_ENV_VARS:
        os.environ.pop(v, None)
    try:
        pg.fetch_models()
        raised = False
    except Exception as e:
        raised = True
        assert "Missing" in str(e) or "cred" in str(e).lower()
    assert raised


def test_fetch_single_page():
    payload = {"models": [SAMPLE_GEN, SAMPLE_EMBED]}
    with patch("pollers.poll_gemini._http_get", return_value=_http_ok(payload)):
        with patch("pollers.poll_gemini.time.sleep"):
            with patch.dict(os.environ, {"GOOGLE_API_KEY": "k"}):
                out = pg.fetch_models(free_only=True)
    assert len(out) == 1
    assert out[0]["id"] == "models/gemini-2.5-flash"


def test_fetch_pagination():
    p1 = {"models": [SAMPLE_GEN], "nextPageToken": "tok2"}
    p2 = {"models": [SAMPLE_PAIDISH]}
    responses = [_http_ok(p1), _http_ok(p2)]
    with patch("pollers.poll_gemini._http_get", side_effect=responses):
        with patch("pollers.poll_gemini.time.sleep"):
            with patch.dict(os.environ, {"GOOGLE_API_KEY": "k"}):
                out = pg.fetch_models(free_only=True)
    assert len(out) == 2


def test_fetch_429_retry():
    responses = [(429, {}, {}, None), _http_ok({"models": [SAMPLE_GEN]})]
    with patch("pollers.poll_gemini._http_get", side_effect=responses):
        with patch("pollers.poll_gemini.time.sleep"):
            with patch.dict(os.environ, {"GOOGLE_API_KEY": "k"}):
                out = pg.fetch_models(free_only=True)
    assert len(out) == 1


def test_poll_returns_list_on_error():
    with patch("pollers.poll_gemini._http_get", side_effect=RuntimeError("boom")):
        with patch("pollers.poll_gemini.time.sleep"):
            with patch.dict(os.environ, {"GOOGLE_API_KEY": "k"}):
                out = pg.poll()
    assert out == []
