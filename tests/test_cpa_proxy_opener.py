#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CPA urllib opener must not inherit macOS system proxies when direct."""

from __future__ import annotations

import sys
from pathlib import Path as _Path

_SRC = _Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TestCpaProxyOpener(unittest.TestCase):
    def test_direct_disables_system_proxy(self) -> None:
        from grok_register.export.cpa_xai.proxyutil import (
            _DirectProxyHandler,
            build_opener,
            clear_runtime_proxy,
            set_runtime_proxy,
        )

        clear_runtime_proxy()
        set_runtime_proxy("")
        try:
            # System default would inject 127.0.0.1:1082 on this machine
            default = urllib.request.build_opener()
            default_proxies = None
            for h in default.handlers:
                if isinstance(h, urllib.request.ProxyHandler):
                    default_proxies = h.proxies
                    break

            opener = build_opener("")
            direct = None
            for h in opener.handlers:
                if isinstance(h, _DirectProxyHandler):
                    direct = h
                    break
                if isinstance(h, urllib.request.ProxyHandler):
                    direct = h
            self.assertIsNotNone(direct, "direct mode must install a ProxyHandler")
            self.assertIsInstance(direct, _DirectProxyHandler)
            # Must not forward system 1082
            self.assertNotIn("1082", str(getattr(direct, "proxies", {})))
            if default_proxies and "1082" in str(default_proxies):
                self.assertNotEqual(str(direct.proxies), str(default_proxies))
        finally:
            clear_runtime_proxy()

    def test_explicit_proxy_used(self) -> None:
        from grok_register.export.cpa_xai.proxyutil import build_opener, clear_runtime_proxy

        clear_runtime_proxy()
        opener = build_opener("http://127.0.0.1:9999")
        found = None
        for h in opener.handlers:
            if isinstance(h, urllib.request.ProxyHandler):
                found = h.proxies
                break
        self.assertIsNotNone(found)
        self.assertIn("9999", found.get("https") or found.get("http") or "")

    def test_oauth_opener_direct(self) -> None:
        from grok_register.export.cpa_xai.oauth_device import _opener
        from grok_register.export.cpa_xai.proxyutil import _DirectProxyHandler, clear_runtime_proxy, set_runtime_proxy

        clear_runtime_proxy()
        set_runtime_proxy("")
        try:
            op = _opener("")
            self.assertTrue(
                any(isinstance(h, _DirectProxyHandler) for h in op.handlers)
            )
        finally:
            clear_runtime_proxy()


if __name__ == "__main__":
    unittest.main()
