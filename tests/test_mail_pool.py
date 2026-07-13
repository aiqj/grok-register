#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for background mailbox pre-create pool."""

from __future__ import annotations

import sys
from pathlib import Path as _Path

_SRC = _Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from grok_register.mail.pool import (  # noqa: E402
    MailboxPool,
    acquire_mailbox,
    get_mail_pool,
    start_mail_pool,
    stop_mail_pool,
)


class TestMailboxPool(unittest.TestCase):
    def tearDown(self) -> None:
        stop_mail_pool()

    def test_acquire_ready_mailbox(self) -> None:
        n = {"i": 0}
        lock = threading.Lock()

        def create():
            with lock:
                n["i"] += 1
                i = n["i"]
            return f"u{i}@example.com", f"tok{i}"

        pool = MailboxPool(create, size=2, log=lambda _m: None)
        pool.start()
        deadline = time.time() + 3
        while pool.stats()["ready"] < 1 and time.time() < deadline:
            time.sleep(0.05)
        email, token = pool.acquire(timeout=2)
        self.assertTrue(email.endswith("@example.com"))
        self.assertTrue(token.startswith("tok"))
        pool.stop()
        self.assertGreaterEqual(pool.stats()["created"], 1)
        self.assertEqual(pool.stats()["acquired"], 1)

    def test_start_stop_global(self) -> None:
        counter = {"n": 0}

        def create():
            counter["n"] += 1
            return f"g{counter['n']}@t.com", f"t{counter['n']}"

        p = start_mail_pool(create, size=2, log=lambda _m: None)
        self.assertIs(get_mail_pool(), p)
        deadline = time.time() + 3
        while acquire_mailbox(timeout=0.2) is None and time.time() < deadline:
            pass
        hit = acquire_mailbox(timeout=1.0)
        self.assertIsNotNone(hit)
        stop_mail_pool()
        self.assertIsNone(get_mail_pool())
        self.assertIsNone(acquire_mailbox(timeout=0.1))

    def test_create_failure_does_not_block_stop(self) -> None:
        def bad():
            raise RuntimeError("api down")

        pool = MailboxPool(bad, size=1, log=lambda _m: None)
        pool.start()
        time.sleep(0.3)
        pool.stop(wait=1.0)
        self.assertGreaterEqual(pool.stats()["fail"], 1)
        self.assertFalse(pool.stats()["alive"])

    def test_stage_summary_field_on_register_result(self) -> None:
        from grok_register.types import RegisterResult

        r = RegisterResult(ok=True, stage_summary="init=100ms email=50ms")
        self.assertIn("init=", r.stage_summary)
        self.assertEqual(r.duration_ms, 0)


class TestBrowserStageSummary(unittest.TestCase):
    def test_stage_summary_format(self) -> None:
        from grok_register.transport.browser import BrowserTransport

        class _Reg:
            pass

        t = BrowserTransport(reg_module=_Reg())
        t0 = time.time() - 0.12
        t._mark("init", t0)
        t._mark("email", time.time() - 0.05)
        s = t.stage_summary()
        self.assertIn("init=", s)
        self.assertIn("email=", s)
        self.assertIn("ms", s)


if __name__ == "__main__":
    unittest.main()
