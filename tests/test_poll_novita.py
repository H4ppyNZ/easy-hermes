import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import poll_novita as pn


def _resp(obj):
    b = json.dumps(obj).encode("utf-8")
    m = MagicMock()
    m.read.return_value = b
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


class TestPollNovita(unittest.TestCase):
    def test_openai_style_free_filter(self):
        payload = {"data": [
            {"id": "free-model", "input_token_price_per_m": 0, "output_token_price_per_m": 0},
            {"id": "paid-model", "input_token_price_per_m": 3000, "output_token_price_per_m": 12000},
        ]}
        with patch("poll_novita.urllib.request.urlopen", return_value=_resp(payload)):
            models = pn.fetch_models()
        self.assertEqual([m["id"] for m in models], ["free-model"])

    def test_openai_style(self):
        payload = {"data": [{"id": "llama3:8b"}, {"id": "qwen:7b"}]}
        with patch("poll_novita.urllib.request.urlopen", return_value=_resp(payload)):
            models = pn.fetch_models(free_only=False)
        self.assertEqual([m["id"] for m in models], ["llama3:8b", "qwen:7b"])

    def test_metadata_merge(self):
        payload = {
            "data": [{"id": "beam"}],
            "models": [{"slug": "beam", "capabilities": ["completion"], "context_size": 1024}],
        }
        with patch("poll_novita.urllib.request.urlopen", return_value=_resp(payload)):
            models = pn.fetch_models(free_only=False)
        self.assertEqual(models[0]["id"], "beam")
        self.assertEqual(models[0]["capabilities"], ["completion"])
        self.assertEqual(models[0]["context_size"], 1024)

    def test_free_filter_pricing_generic(self):
        payload = {"data": [
            {"id": "free-model", "pricing": {"prompt": 0, "completion": 0}},
            {"id": "paid-model", "pricing": {"prompt": 0.001, "completion": 0.002}},
            {"id": "price-field", "price": 0},
            {"id": "paid-price", "price": 5},
        ]}
        with patch("poll_novita.urllib.request.urlopen", return_value=_resp(payload)):
            models = pn.fetch_models()
        ids = [m["id"] for m in models]
        self.assertIn("free-model", ids)
        self.assertIn("price-field", ids)
        self.assertNotIn("paid-model", ids)
        self.assertNotIn("paid-price", ids)

    def test_free_filter_novita_both_prices(self):
        payload = {"data": [
            {"id": "free-both", "input_token_price_per_m": 0, "output_token_price_per_m": 0},
            {"id": "paid-out", "input_token_price_per_m": 0, "output_token_price_per_m": 5},
        ]}
        with patch("poll_novita.urllib.request.urlopen", return_value=_resp(payload)):
            models = pn.fetch_models()
        ids = [m["id"] for m in models]
        self.assertIn("free-both", ids)
        self.assertNotIn("paid-out", ids)

    def test_empty_result(self):
        payload = {"data": []}
        with patch("poll_novita.urllib.request.urlopen", return_value=_resp(payload)):
            models = pn.fetch_models()
        self.assertEqual(models, [])

    def test_retry_on_500(self):
        import urllib.error
        http_err = urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b""))
        seq = [http_err, _resp({"data": [{"id": "m1", "input_token_price_per_m": 0, "output_token_price_per_m": 0}]})]
        with patch("poll_novita.urllib.request.urlopen", side_effect=seq):
            with patch("poll_novita.time.sleep"):
                models = pn.fetch_models(retries=2)
        self.assertEqual([m["id"] for m in models], ["m1"])

    def test_render_table(self):
        txt = pn.render_table([{"id": "abc", "free": True, "capabilities": ["x"]}])
        self.assertIn("abc", txt)
        self.assertIn("Total: 1", txt)

    def test_write_output(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "out.json")
            out = pn.build_output([{"id": "m", "free": True}], "src")
            pn.write_output(path, out)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["count"], 1)
            self.assertTrue(data["best_effort"])
            self.assertTrue(data["unverified"])

    def test_is_free_default_true(self):
        self.assertTrue(pn.is_free({"id": "x"}))
        self.assertTrue(pn.is_free("x"))

    def test_normalize_aliases(self):
        m = pn.normalize({"name": "n1"}, source="s")
        self.assertEqual(m["id"], "n1")
        self.assertEqual(m["source"], "s")

    def test_load_api_key(self):
        self.assertEqual(pn.load_api_key({"NOVITA_API_KEY": "secret"}), "secret")
        self.assertIsNone(pn.load_api_key({}))

    def test_main_dry_run(self):
        payload = {"data": [{"id": "m1", "input_token_price_per_m": 0, "output_token_price_per_m": 0}]}
        with patch("poll_novita.urllib.request.urlopen", return_value=_resp(payload)):
            rc = pn.main(["--dry-run", "--no-table", "--quiet", "--out", os.path.join(tempfile.gettempdir(), "nope.json")])
        self.assertEqual(rc, 0)

    def test_main_writes_file(self):
        payload = {"data": [{"id": "m1", "input_token_price_per_m": 0, "output_token_price_per_m": 0}]}
        with tempfile.TemporaryDirectory() as d:
            outp = os.path.join(d, "o.json")
            with patch("poll_novita.urllib.request.urlopen", return_value=_resp(payload)):
                rc = pn.main(["--out", outp, "--no-table", "--quiet"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(outp))


if __name__ == "__main__":
    unittest.main()
