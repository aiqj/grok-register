#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Proxy pool health / cooldown unit tests (no real network)."""

from __future__ import annotations

import sys
from pathlib import Path as _Path

_SRC = _Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


import os
import sys
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import grok_register.proxy.pool as pp  # noqa: E402


class ProxyHealthTests(unittest.TestCase):
    def setUp(self):
        for k in ("PROXY", "PROXY_POOL", "PROXY_PROBE", "PROXY_COOLDOWN_SEC", "PROXY_FAIL_THRESHOLD"):
            os.environ.pop(k, None)
        pp.clear_thread_proxy()
        pp.refresh_proxy_cache({})
        with pp._lock:
            pp._states.clear()
            pp._cached_list = None
            pp._index = 0

    def tearDown(self):
        self.setUp()

    def test_next_proxy_skips_cooldown(self):
        pool = ["http://a:1", "http://b:2", "http://c:3"]
        pp.refresh_proxy_cache({"proxy_pool": ",".join(pool)})
        # Cool down a
        with pp._lock:
            st = pp._states["http://a:1"]
            st.cooldown_until = time.time() + 600
        seen = {pp.next_proxy() for _ in range(6)}
        self.assertNotIn("http://a:1", seen)
        self.assertTrue(seen <= {"http://b:2", "http://c:3"})

    def test_report_failure_triggers_cooldown(self):
        pp.refresh_proxy_cache({"proxy_pool": "http://dead:9", "proxy_fail_threshold": 2, "proxy_cooldown_sec": 120})
        pp.report_proxy_failure("http://dead:9", "timeout", config={"proxy_fail_threshold": 2, "proxy_cooldown_sec": 120})
        stats = pp.proxy_pool_stats()
        self.assertEqual(stats["available"], 1)  # only 1 fail, threshold 2
        pp.report_proxy_failure("http://dead:9", "timeout", config={"proxy_fail_threshold": 2, "proxy_cooldown_sec": 120})
        stats = pp.proxy_pool_stats()
        self.assertEqual(stats["available"], 0)
        self.assertEqual(stats["cooling"], 1)

    def test_report_success_clears_failures(self):
        pp.refresh_proxy_cache({"proxy_pool": "http://ok:1"})
        pp.report_proxy_failure("http://ok:1", "x", config={"proxy_fail_threshold": 5})
        pp.report_proxy_success("http://ok:1", exit_ip="1.2.3.4")
        with pp._lock:
            st = pp._states["http://ok:1"]
            self.assertEqual(st.failures, 0)
            self.assertEqual(st.last_exit_ip, "1.2.3.4")
            self.assertTrue(st.probe_ok)

    @patch.object(pp, "probe_proxy")
    def test_probe_pool_marks_dead(self, mock_probe):
        def side(p, timeout=5.0, probe_urls=None):
            if "good" in p:
                return True, "可用 - HTTP 200，出口 9.9.9.9", "9.9.9.9"
            return False, "不可用 - CONNECT 503", ""

        mock_probe.side_effect = side
        logs: list[str] = []
        stats = pp.probe_proxy_pool(
            {"proxy_pool": "http://good:1,http://bad:2", "proxy_cooldown_sec": 60, "proxy_fail_threshold": 1},
            log=logs.append,
        )
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["available"], 1)
        self.assertTrue(any("代理池可用: 1/2" in x for x in logs))


if __name__ == "__main__":
    unittest.main()
