#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for proxy_pool (PROXY / PROXY_POOL + headless + startup checks)."""

from __future__ import annotations

import sys
from pathlib import Path as _Path

_SRC = _Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import grok_register.proxy.pool as pp  # noqa: E402


class ProxyPoolTests(unittest.TestCase):
    def setUp(self):
        self._env_backup = {}
        for k in (
            "PROXY",
            "PROXY_POOL",
            "HEADLESS",
            "BROWSER_HEADLESS",
            "https_proxy",
            "HTTPS_PROXY",
            "http_proxy",
            "HTTP_PROXY",
            "USE_SYSTEM_PROXY",
            "PROXY_ROTATE_EVERY_ACCOUNT",
            "DISPLAY",
            "WAYLAND_DISPLAY",
        ):
            self._env_backup[k] = os.environ.get(k)
            os.environ.pop(k, None)
        pp.clear_thread_proxy()
        pp.refresh_proxy_cache({})

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        pp.clear_thread_proxy()
        pp.refresh_proxy_cache({})

    def test_parse_comma_semicolon(self):
        lst = pp.parse_proxy_list("http://a:1, http://b:2;http://c:3")
        self.assertEqual(lst, ["http://a:1", "http://b:2", "http://c:3"])

    def test_parse_bare_host_port(self):
        lst = pp.parse_proxy_list("127.0.0.1:7890,10.0.0.1:8080")
        self.assertEqual(lst[0], "http://127.0.0.1:7890")
        self.assertEqual(lst[1], "http://10.0.0.1:8080")

    def test_env_pool_beats_single(self):
        os.environ["PROXY_POOL"] = "http://p1:1,http://p2:2"
        os.environ["PROXY"] = "http://single:9"
        lst = pp.resolve_proxy_list({"proxy": "http://cfg:8", "proxy_pool": "http://cfgpool:7"})
        self.assertEqual(lst, ["http://p1:1", "http://p2:2"])

    def test_env_proxy_beats_config(self):
        os.environ["PROXY"] = "http://env:1"
        lst = pp.resolve_proxy_list({"proxy": "http://cfg:2"})
        self.assertEqual(lst, ["http://env:1"])

    def test_config_pool(self):
        lst = pp.resolve_proxy_list({"proxy_pool": "http://a:1;http://b:2", "proxy": "http://x:9"})
        self.assertEqual(lst, ["http://a:1", "http://b:2"])

    def test_round_robin(self):
        pp.refresh_proxy_cache({"proxy_pool": "http://a:1,http://b:2,http://c:3"})
        seen = [pp.next_proxy() for _ in range(6)]
        self.assertEqual(
            seen, ["http://a:1", "http://b:2", "http://c:3", "http://a:1", "http://b:2", "http://c:3"]
        )

    def test_thread_sticky_then_rotate(self):
        pp.refresh_proxy_cache({"proxy_pool": "http://a:1,http://b:2"})
        p1 = pp.get_thread_proxy()
        p1b = pp.get_thread_proxy()
        self.assertEqual(p1, p1b)
        p2 = pp.rotate_thread_proxy()
        self.assertNotEqual(p1, p2)

    def test_proxy_for_chromium_strips_auth(self):
        # user:pass is forwarded via local auth proxy (127.0.0.1:ephemeral)
        out = pp.proxy_for_chromium("http://user:pass@1.2.3.4:8080")
        self.assertTrue(str(out).startswith("http://127.0.0.1:"), out)

    def test_proxy_for_chromium_socks5(self):
        out = pp.proxy_for_chromium("socks5://1.2.3.4:1080")
        self.assertIn("1.2.3.4:1080", out)
        self.assertTrue(out.startswith("socks5"), out)
        out2 = pp.proxy_for_chromium("socks5h://1.2.3.4:1080")
        self.assertIn("1.2.3.4:1080", out2)

    def test_proxy_has_userinfo(self):
        self.assertTrue(pp.proxy_has_userinfo("http://user:pass@h:1"))
        self.assertFalse(pp.proxy_has_userinfo("http://h:1"))

    def test_proxy_log_label_redacts(self):
        label = pp.proxy_log_label("http://user:secret@1.2.3.4:8080")
        self.assertIn("user:***@", label)
        self.assertNotIn("secret", label)

    def test_headless_default_desktop(self):
        # Without env/config: Linux no DISPLAY → True; else False (darwin/win)
        import sys

        got = pp.resolve_headless({})
        if sys.platform.startswith("linux"):
            display = (os.environ.get("DISPLAY") or "").strip()
            wayland = (os.environ.get("WAYLAND_DISPLAY") or "").strip()
            if not display and not wayland:
                self.assertTrue(got)
            # with display, either is ok depending on env leftover
        else:
            self.assertFalse(got)

    def test_headless_env(self):
        os.environ["HEADLESS"] = "1"
        self.assertTrue(pp.resolve_headless({}))
        os.environ["HEADLESS"] = "0"
        self.assertFalse(pp.resolve_headless({"headless": True}))

    def test_headless_config(self):
        self.assertTrue(pp.resolve_headless({"headless": True}))
        self.assertFalse(pp.resolve_headless({"headless": False}))

    def test_describe_proxy_mode(self):
        pp.refresh_proxy_cache({})
        self.assertIn("direct", pp.describe_proxy_mode({}))
        pp.refresh_proxy_cache({"proxy": "http://127.0.0.1:7890"})
        desc_single = pp.describe_proxy_mode({"proxy": "http://127.0.0.1:7890"})
        self.assertTrue(
            ("single" in desc_single) or ("proxy=" in desc_single) or ("127.0.0.1" in desc_single),
            desc_single,
        )
        pp.refresh_proxy_cache({"proxy_pool": "http://a:1,http://b:2"})
        desc = pp.describe_proxy_mode({"proxy_pool": "http://a:1,http://b:2"})
        self.assertIn("pool", desc)

    def test_proxy_optional_empty_is_direct(self):
        os.environ["https_proxy"] = "http://127.0.0.1:7890"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
        lst = pp.resolve_proxy_list({"proxy": "", "proxy_pool": ""})
        self.assertEqual(lst, [])
        pp.refresh_proxy_cache({"proxy": "", "proxy_pool": ""})
        self.assertEqual(pp.next_proxy(), "")
        self.assertEqual(pp.get_thread_proxy(), "")

    def test_use_system_proxy_opt_in(self):
        os.environ["https_proxy"] = "http://127.0.0.1:7890"
        lst = pp.resolve_proxy_list({"use_system_proxy": True})
        self.assertEqual(lst, ["http://127.0.0.1:7890"])
        os.environ["USE_SYSTEM_PROXY"] = "1"
        lst2 = pp.resolve_proxy_list({})
        self.assertEqual(lst2, ["http://127.0.0.1:7890"])

    def test_proxy_rotate_every_account_default_off(self):
        self.assertFalse(pp.proxy_rotate_every_account({}))
        self.assertTrue(pp.proxy_rotate_every_account({"proxy_rotate_every_account": True}))
        os.environ["PROXY_ROTATE_EVERY_ACCOUNT"] = "1"
        self.assertTrue(pp.proxy_rotate_every_account({"proxy_rotate_every_account": False}))

    def test_startup_warnings_auth_proxy(self):
        pp.refresh_proxy_cache({"proxy": "http://u:p@1.2.3.4:8080", "email_provider": "tempmail_lol"})
        warns = pp.collect_startup_warnings(
            {"proxy": "http://u:p@1.2.3.4:8080", "email_provider": "tempmail_lol", "headless": False},
            extension_path=None,
        )
        self.assertTrue(any("账号密码" in w or "user:pass" in w for w in warns))

    def test_startup_warnings_headless_extension(self):
        ext = os.path.join(ROOT, "turnstilePatch")
        warns = pp.collect_startup_warnings(
            {"headless": True, "email_provider": "tempmail_lol"},
            extension_path=ext if os.path.isdir(ext) else ROOT,
        )
        self.assertTrue(any("headless" in w.lower() or "turnstile" in w.lower() for w in warns))

    def test_proxyutil_reexports(self):
        from grok_register.export.cpa_xai import proxyutil as pu

        self.assertEqual(
            pu.proxy_for_chromium("http://u:p@h:1"),
            pp.proxy_for_chromium("http://u:p@h:1"),
        )
        self.assertEqual(pu.resolve_proxy(""), "")
        pu.set_runtime_proxy("http://x:1")
        self.assertEqual(pu.resolve_proxy(None), "http://x:1")
        pu.clear_runtime_proxy()



if __name__ == "__main__":
    unittest.main()
