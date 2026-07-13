import sys
from pathlib import Path as _Path

_SRC = _Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for TempMail.lol provider helpers (no network)."""

import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# grok_register.core imports browser deps/browser deps at module load; stub if missing
# so pure helper tests can run without Tk / DrissionPage.
for _mod_name in (
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
    "tkinter.scrolledtext",
    "DrissionPage",
    "DrissionPage.errors",
    "curl_cffi",
    "curl_cffi.requests",
):
    if _mod_name not in sys.modules:
        stub = types.ModuleType(_mod_name)
        if _mod_name == "DrissionPage":
            stub.Chromium = object
            stub.ChromiumOptions = object
        if _mod_name == "DrissionPage.errors":
            stub.PageDisconnectedError = type("PageDisconnectedError", (Exception,), {})
        if _mod_name == "curl_cffi.requests":
            stub.get = lambda *a, **k: None
            stub.post = lambda *a, **k: None
        if _mod_name == "tkinter":
            stub.StringVar = object
            stub.BooleanVar = object
            stub.END = "end"
            stub.W = "w"
            stub.EW = "ew"
            stub.NSEW = "nsew"
            stub.LEFT = "left"
            stub.RIGHT = "right"
            stub.NORMAL = "normal"
            stub.DISABLED = "disabled"
            stub.SOLID = "solid"
            stub.GROOVE = "groove"
            stub.Frame = object
            stub.Label = object
            stub.Spinbox = object
            stub.LabelFrame = object
            stub.OptionMenu = object
            stub.Checkbutton = object
            stub.Entry = object
            stub.Button = object
            stub.Tk = object
        sys.modules[_mod_name] = stub

import grok_register.core as app  # noqa: E402


class TempMailLolKeyPoolTests(unittest.TestCase):
    def setUp(self):
        self._old_config = app.config.copy()
        self._env_backup = {
            k: os.environ.get(k)
            for k in (
                "TEMPMAIL_LOL_API_KEY",
                "TEMPMAIL_LOL_API_KEYS",
                "TEMPMAIL_LOL_API_BASE",
            )
        }
        for k in self._env_backup:
            os.environ.pop(k, None)
        app.config = app.DEFAULT_CONFIG.copy()
        app._tempmail_lol_key_index = 0

    def tearDown(self):
        app.config = self._old_config
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        app._tempmail_lol_key_index = 0

    def test_split_api_keys_comma_and_dedupe(self):
        keys = app._split_api_keys(" a ,b; a,  c  ")
        self.assertEqual(keys, ["a", "b", "c"])

    def test_single_key_from_config(self):
        app.config["tempmail_lol_api_key"] = "tm.single.key"
        self.assertEqual(app.get_tempmail_lol_api_keys(), ["tm.single.key"])

    def test_multi_keys_prefer_over_single(self):
        app.config["tempmail_lol_api_key"] = "single"
        app.config["tempmail_lol_api_keys"] = "k1,k2"
        self.assertEqual(app.get_tempmail_lol_api_keys(), ["k1", "k2"])

    def test_env_multi_keys(self):
        os.environ["TEMPMAIL_LOL_API_KEYS"] = "e1,e2"
        self.assertEqual(app.get_tempmail_lol_api_keys(), ["e1", "e2"])

    def test_env_single_key(self):
        os.environ["TEMPMAIL_LOL_API_KEY"] = "env-single"
        self.assertEqual(app.get_tempmail_lol_api_keys(), ["env-single"])

    def test_round_robin(self):
        app.config["tempmail_lol_api_keys"] = "a,b,c"
        got = [app.next_tempmail_lol_api_key() for _ in range(5)]
        self.assertEqual(got, ["a", "b", "c", "a", "b"])

    def test_free_tier_empty_key(self):
        self.assertEqual(app.get_tempmail_lol_api_keys(), [])
        self.assertEqual(app.next_tempmail_lol_api_key(), "")

    def test_provider_alias(self):
        app.config["email_provider"] = "tempmail.lol"
        self.assertEqual(app.get_email_provider(), "tempmail_lol")
        app.config["email_provider"] = "tempmail_lol"
        self.assertEqual(app.get_email_provider(), "tempmail_lol")

    def test_api_base_default_and_override(self):
        self.assertEqual(app.get_tempmail_lol_api_base(), "https://api.tempmail.lol/v2")
        app.config["tempmail_lol_api_base"] = "https://example.com/v2/"
        self.assertEqual(app.get_tempmail_lol_api_base(), "https://example.com/v2")

    def test_build_headers_with_bearer(self):
        headers = app.tempmail_lol_build_headers(api_key="tm.abc", content_type=True)
        self.assertEqual(headers["Authorization"], "Bearer tm.abc")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_create_inbox_parses_response(self):
        class FakeResp:
            status_code = 201
            text = '{"address":"a@b.com","token":"tok123"}'

            def json(self):
                return {"address": "a@b.com", "token": "tok123"}

        with mock.patch.object(app, "http_post", return_value=FakeResp()):
            address, token = app.tempmail_lol_create_inbox(api_key="k")
        self.assertEqual(address, "a@b.com")
        self.assertEqual(token, "tok123")

    def test_get_emails_expired(self):
        class FakeResp:
            status_code = 200
            text = '{"emails":null,"expired":true}'

            def json(self):
                return {"emails": None, "expired": True}

        with mock.patch.object(app, "http_get", return_value=FakeResp()):
            with self.assertRaises(Exception) as ctx:
                app.tempmail_lol_get_emails("tok")
        self.assertIn("过期", str(ctx.exception))

    def test_extract_code_from_body_path(self):
        emails = [
            {
                "from": "noreply@x.ai",
                "to": "user@example.com",
                "subject": "ABC-123 xAI",
                "body": "Your code is ABC-123",
                "html": None,
                "date": 1,
            }
        ]
        with mock.patch.object(app, "tempmail_lol_get_emails", return_value=emails):
            code = app.tempmail_lol_get_oai_code(
                "tok",
                "user@example.com",
                timeout=5,
                poll_interval=0,
            )
        self.assertEqual(code.upper(), "ABC-123")


if __name__ == "__main__":
    unittest.main()
