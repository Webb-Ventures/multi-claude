import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("multi_claude_config", ROOT / "multi_claude_config.py")
cfg = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cfg
spec.loader.exec_module(cfg)
TOKEN = "unit-test-credential-not-real"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"

    def write(self, data):
        self.path.write_text(json.dumps(data))
        return self.path

    def test_gateway_and_redaction(self):
        for url, port in (("https://gateway.invalid/base", 443), ("http://localhost:8317", 8317),
                          ("http://[::1]", 80), ("http://127.0.0.2", 80), ("http://node.ts.net", 80)):
            gateway = cfg.Gateway(url, TOKEN)
            self.assertEqual((gateway.url, gateway.port), (url + "/", port))
            self.assertEqual(gateway.parsed.scheme, url.split(":")[0])
            self.assertNotIn(TOKEN, repr(gateway))
            with self.assertRaises(dataclasses.FrozenInstanceError):
                gateway.url = "https://other.invalid/"

    def test_reject_urls_without_disclosing_credentials(self):
        for url in (f"https://user:{TOKEN}@gateway.invalid", f"https://gateway.invalid/?{TOKEN}",
                    "http://public.invalid", "http://ts.net", "http://node.ts.net.evil.invalid",
                    "https://host/#", "https://host/?", "https://host/\n", "https://host:99999",
                    "https://host:0", "https://host:", "https://host\\evil", "file:///tmp/a", None):
            with self.subTest(url=url), self.assertRaises(ValueError) as error:
                cfg.Gateway(url, TOKEN)
            self.assertNotIn(TOKEN, str(error.exception))

    def test_loading_overrides_and_default_precedence(self):
        self.write({"gateway_urls": ["https://first.invalid", "https://second.invalid"], "gateway_token": TOKEN})
        self.assertEqual(len(cfg.load_gateways(self.path)), 2)
        with patch.object(Path, "is_file", return_value=True):
            self.assertEqual(cfg.default_config_path(), ROOT / "config.local.json")
        with patch.object(Path, "is_file", return_value=False):
            self.assertEqual(cfg.default_config_path(), Path.home() / ".config/multi-claude/config.json")
        os.environ.update(MULTI_CLAUDE_CONFIG=str(self.path), MULTI_CLAUDE_GATEWAY_URL="http://localhost:8317", MULTI_CLAUDE_GATEWAY_TOKEN="env-test-credential")
        self.assertEqual(cfg.default_config_path(), self.path)
        self.assertEqual(cfg.load_gateways()[0], cfg.Gateway("http://localhost:8317/", "env-test-credential"))
        self.path.unlink()
        self.assertEqual(len(cfg.load_gateways()), 1)  # Environment-only configuration.

    def test_malformed_and_placeholder_config(self):
        for data in ([], {}, {"gateway_token": TOKEN, "gateway_urls": "https://host"},
                     {"gateway_token": TOKEN, "gateway_urls": []},
                     *({"gateway_urls": ["http://localhost:8317"], "gateway_token": t}
                       for t in (None, "", "REPLACE_WITH_YOUR_GATEWAY_TOKEN", "placeholder", "a\nb"))):
            with self.subTest(data=data), self.assertRaises(ValueError):
                cfg.load_config(self.write(data))
        self.path.write_text('{"gateway_token": "' + TOKEN)
        with self.assertRaises(ValueError) as error:
            cfg.load_config(self.path)
        self.assertNotIn(TOKEN, str(error.exception))
        self.path.unlink()
        with self.assertRaises(ValueError):
            cfg.load_gateways(self.path)

    def test_tcp_fallback_and_fail_closed(self):
        gateways = [cfg.Gateway("https://first.invalid", TOKEN), cfg.Gateway("https://second.invalid", TOKEN)]
        with patch.object(cfg.socket, "create_connection", side_effect=[OSError(TOKEN), MagicMock()]) as probe:
            self.assertIs(cfg.select_gateway(gateways), gateways[1])
            self.assertEqual(probe.call_args_list, [call(("first.invalid", 443), timeout=3), call(("second.invalid", 443), timeout=3)])
        with patch.object(cfg.socket, "create_connection", side_effect=OSError(TOKEN)):
            for candidates in (gateways, []):
                with self.assertRaisesRegex(ValueError, "No direct fallback") as error:
                    cfg.select_gateway(candidates)
                self.assertNotIn(TOKEN, str(error.exception))
