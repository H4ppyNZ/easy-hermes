import json
import os
import unittest
from unittest.mock import patch, MagicMock

import pollers.poll_cloudflare as pc


def _resp(obj):
    b = json.dumps(obj).encode("utf-8")
    m = MagicMock()
    m.read.return_value = b
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


class TestPollCloudflare(unittest.TestCase):
    def test_envelope_result(self):
        payload = {"success": True, "result": [
            {"name": "@cf/meta/llama-3.1-8b-instruct", "task": "Text Generation"},
            {"name": "@cf/qwen/qwen3-30b-a3b-fp8", "task": "Text Generation"},
        ]}
        with patch("pollers.poll_cloudflare.urllib.request.urlopen", return_value=_resp(payload)):
            models = pc.fetch_models(account_id="acct123", free_only=False)
        self.assertEqual([m["id"] for m in models],
                         ["@cf/meta/llama-3.1-8b-instruct", "@cf/qwen/qwen3-30b-a3b-fp8"])

    def test_chat_filter(self):
        payload = {"result": [
            {"name": "embed-model", "task": "Embeddings"},
            {"name": "chat-model", "task": "Text Generation"},
        ]}
        with patch("pollers.poll_cloudflare.urllib.request.urlopen", return_value=_resp(payload)):
            models = pc.fetch_models(account_id="acct123", free_only=True)
        self.assertEqual([m["id"] for m in models], ["chat-model"])

    def test_no_account_returns_empty(self):
        # no account id -> no endpoint -> empty, no raise
        self.assertEqual(pc.poll(), [])

    def test_normalize_context(self):
        raw = {"name": "m", "task": "Text Generation", "context_window": 8192}
        norm = pc.normalize_model(raw)
        self.assertEqual(norm["id"], "m")
        self.assertEqual(norm["context_window"], 8192)
        self.assertTrue(norm["free"])

    def test_is_free_model(self):
        self.assertTrue(pc.is_free_model({"id": "x"}))
        self.assertTrue(pc.is_free_model("x"))


if __name__ == "__main__":
    unittest.main()
