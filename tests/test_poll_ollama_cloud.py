import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import poll_ollama_cloud as poc


def _resp(obj):
    b = json.dumps(obj).encode("utf-8")
    m = MagicMock()
    m.read.return_value = b
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


class TestPollOllamaCloud(unittest.TestCase):
    def test_ollama_style(self):
        payload = {"models": [{"name": "gpt-oss:20b", "capabilities": ["completion"]}]}
        with patch("poll_ollama_cloud.urllib.request.urlopen", return_value=_resp(payload)):
            models = poc.fetch_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["id"], "gpt-oss:20b")
        self.assertTrue(models[0]["free"])

    def test_openai_style(self):
        payload = {"data": [{"id": "llama3:8b"}, {"id": "qwen:7b"}]}
        with patch("poll_ollama_cloud.urllib.request.urlopen", return_value=_resp(payload)):
            models = poc.fetch_models()
        self.assertEqual([m["id"] for m in models], ["llama3:8b", "qwen:7b"])

    def test_openai_style_with_metadata_merge(self):
        payload = {
            "data": [{"id": "beam"}],
            "models": [{"slug": "beam", "capabilities": ["completion"], "context_window": 1024}],
        }
        with patch("poll_ollama_cloud.urllib.request.urlopen", return_value=_resp(payload)):
            models = poc.fetch_models()
        self.assertEqual(models[0]["id"], "beam")
        self.assertEqual(models[0]["capabilities"], ["completion"])
        self.assertEqual(models[0]["context_window"], 1024)

    def test_free_filter_pricing(self):
        payload = {"data": [
            {"id": "free-model", "pricing": {"prompt": 0, "completion": 0}},
            {"id": "paid-model", "pricing": {"prompt": 0.001, "completion": 0.002}},
            {"id": "price-field", "price": 0},
            {"id": "paid-price", "price": 5},
        ]}
        with patch("poll_ollama_cloud.urllib.request.urlopen", return_value=_resp(payload)):
            models = poc.fetch_models()
        ids = [m["id"] for m in models]
        self.assertIn("free-model", ids)
        self.assertIn("price-field", ids)
        self.assertNotIn("paid-model", ids)
        self.assertNotIn("paid-price", ids)

    def test_empty_result(self):
        payload = {"models": []}
        with patch("poll_ollama_cloud.urllib.request.urlopen", return_value=_resp(payload)):
            # first endpoint empty -> tries next; mock returns empty for all, raw stays []
            models = poc.fetch_models()
        self.assertEqual(models, [])

    def test_retry_on_500(self):
        err = Exception("boom")
        # First call raises HTTPError 500, second succeeds
        import urllib.error
        http_err = urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b""))
        seq = [http_err, _resp({"models": [{"name": "m1"}]})]
        with patch("poll_ollama_cloud.urllib.request.urlopen", side_effect=seq):
            with patch("poll_ollama_cloud.time.sleep"):
                models = poc.fetch_models(retries=2)
        self.assertEqual([m["id"] for m in models], ["m1"])

    def test_render_table(self):
        txt = poc.render_table([{"id": "abc", "free": True, "capabilities": ["x"]}])
        self.assertIn("abc", txt)
        self.assertIn("Total: 1", txt)

    def test_write_output(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "out.json")
            out = poc.build_output([{"id": "m", "free": True}], "src")
            poc.write_output(path, out)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["count"], 1)
            self.assertTrue(data["best_effort"])
            self.assertTrue(data["unverified"])

    def test_is_free_default_true(self):
        self.assertTrue(poc.is_free({"id": "x"}))
        self.assertTrue(poc.is_free("x"))

    def test_normalize_aliases(self):
        m = poc.normalize({"name": "n1"}, source="s")
        self.assertEqual(m["id"], "n1")
        self.assertEqual(m["source"], "s")

    def test_main_dry_run(self):
        payload = {"models": [{"name": "m1"}]}
        with patch("poll_ollama_cloud.urllib.request.urlopen", return_value=_resp(payload)):
            rc = poc.main(["--dry-run", "--no-table", "--quiet", "--out", os.path.join(tempfile.gettempdir(), "nope.json")])
        self.assertEqual(rc, 0)

    def test_main_writes_file(self):
        payload = {"models": [{"name": "m1"}]}
        with tempfile.TemporaryDirectory() as d:
            outp = os.path.join(d, "o.json")
            with patch("poll_ollama_cloud.urllib.request.urlopen", return_value=_resp(payload)):
                rc = poc.main(["--out", outp, "--no-table", "--quiet"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(outp))


if __name__ == "__main__":
    unittest.main()
