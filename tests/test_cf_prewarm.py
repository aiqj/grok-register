#!/usr/bin/env python
# -*- coding: utf-8 -*-
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

import grok_register.browser.cf_prewarm as cf  # noqa: E402


class CfPrewarmUnitTests(unittest.TestCase):
    def setUp(self):
        cf.clear_cf_cache()
        os.environ.pop("CF_PREWARM", None)

    def tearDown(self):
        cf.clear_cf_cache()
        os.environ.pop("CF_PREWARM", None)

    def test_normalize_expands_cf_domains(self):
        raw = [
            {
                "name": "cf_clearance",
                "value": "abc",
                "domain": ".x.ai",
                "path": "/",
                "secure": True,
            }
        ]
        out = cf._normalize_cookies(raw)
        domains = {c["domain"] for c in out if c["name"] == "cf_clearance"}
        self.assertIn(".x.ai", domains)
        self.assertIn("accounts.x.ai", domains)

    def test_cache_roundtrip(self):
        cf.set_cached_cf_cookies(
            "http://u:p@h:1",
            [{"name": "cf_clearance", "value": "tok", "domain": ".x.ai", "path": "/"}],
        )
        got = cf.get_cached_cf_cookies("http://u:p@h:1")
        self.assertTrue(any(c["name"] == "cf_clearance" for c in got))

    def test_enabled_env_off(self):
        os.environ["CF_PREWARM"] = "0"
        self.assertFalse(cf.cf_prewarm_enabled({}))

    def test_direct_default_off(self):
        # No proxy → prewarm off even if config true-ish
        self.assertFalse(cf.cf_prewarm_enabled({"cf_prewarm": True}))
        self.assertFalse(cf.cf_prewarm_enabled({"cf_prewarm": "auto"}))


if __name__ == "__main__":
    unittest.main()
