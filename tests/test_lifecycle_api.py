#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Smoke tests for CLI lifecycle API surface on grok_register_ttk."""

from __future__ import annotations

import sys
from pathlib import Path as _Path

_SRC = _Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Stub Tk before importing ttk module (Homebrew Python may lack _tkinter)
for _name in (
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
    "tkinter.scrolledtext",
):
    if _name not in sys.modules:
        stub = types.ModuleType(_name)
        if _name == "tkinter":
            for attr in (
                "StringVar",
                "BooleanVar",
                "Frame",
                "Label",
                "Spinbox",
                "LabelFrame",
                "OptionMenu",
                "Checkbutton",
                "Entry",
                "Button",
                "Tk",
                "END",
                "W",
                "EW",
                "NSEW",
                "LEFT",
                "RIGHT",
                "NORMAL",
                "DISABLED",
                "SOLID",
                "GROOVE",
                "RAISED",
                "BOTH",
            ):
                setattr(stub, attr, object)
        sys.modules[_name] = stub

import grok_register.core as reg  # noqa: E402


class LifecycleApiTests(unittest.TestCase):
    def test_required_symbols_exist(self):
        for name in (
            "TabPool",
            "CHROMIUM_SLIM_FLAGS",
            "PERF_FLAGS",
            "configure_perf",
            "get_page",
            "set_page_context",
            "clear_page_context",
            "_get_page",
            "prepare_browser_for_next_account",
            "mark_used",
            "mark_error",
            "save_cookies_snapshot",
            "create_browser_options",
            "start_browser",
            "stop_browser",
        ):
            self.assertTrue(hasattr(reg, name), f"missing {name}")

    def test_configure_perf_updates_flags(self):
        reg.configure_perf(
            fast=True,
            sleep_scale=0.2,
            skip_debug_io=True,
            cookie_snapshot=False,
            async_side_effects=True,
            browser_reuse=False,
            browser_recycle_every=7,
        )
        self.assertTrue(reg.PERF_FLAGS["fast"])
        self.assertEqual(reg.PERF_FLAGS["browser_recycle_every"], 7)
        self.assertFalse(reg.PERF_FLAGS["browser_reuse"])
        # restore safer defaults for other tests
        reg.configure_perf(
            fast=False,
            sleep_scale=1.0,
            skip_debug_io=False,
            cookie_snapshot=True,
            async_side_effects=False,
            browser_reuse=True,
            browser_recycle_every=25,
        )

    def test_set_page_context_thread_local(self):
        class Dummy:
            pass

        b, p = Dummy(), Dummy()
        reg.set_page_context(b, p)
        self.assertIs(reg.get_page(), p)
        self.assertIs(reg._get_page(), p)
        reg.clear_page_context()

    def test_mark_used_and_error(self):
        before_u = len(reg._used_accounts)
        before_e = len(reg._error_accounts)
        reg.mark_used("u@example.com", "secret")
        reg.mark_error("u@example.com", "fail")
        self.assertEqual(len(reg._used_accounts), before_u + 1)
        self.assertEqual(len(reg._error_accounts), before_e + 1)

    def test_slim_flags_present(self):
        flags = " ".join(reg.CHROMIUM_SLIM_FLAGS)
        for need in (
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
        ):
            self.assertIn(need, flags)

    def test_banned_automation_cli_not_in_stealth(self):
        stealth = " ".join(getattr(reg, "CHROMIUM_STEALTH_FLAGS", ()) or ())
        banned = " ".join(getattr(reg, "CHROMIUM_BANNED_CLI_FLAGS", ()) or ())
        self.assertNotIn("AutomationControlled", stealth)
        self.assertIn("AutomationControlled", banned)

    def test_default_config_has_core_keys(self):
        self.assertIn("cpa_export_enabled", reg.DEFAULT_CONFIG)
        self.assertIn("mail_pool_size", reg.DEFAULT_CONFIG)
        self.assertIn("cpa_mint_prefer_warm_browser", reg.DEFAULT_CONFIG)

    def test_default_proxy_pool_and_headless_keys(self):
        self.assertIn("proxy_pool", reg.DEFAULT_CONFIG)
        self.assertIn("headless", reg.DEFAULT_CONFIG)
        self.assertEqual(reg.DEFAULT_CONFIG.get("proxy"), "")
        self.assertEqual(reg.DEFAULT_CONFIG.get("proxy_pool"), "")
        self.assertFalse(reg.DEFAULT_CONFIG.get("use_system_proxy"))
        self.assertFalse(reg.DEFAULT_CONFIG.get("proxy_rotate_every_account"))
        # Desktop-friendly default: headed (Linux server auto-headless via resolve_headless)
        self.assertFalse(reg.DEFAULT_CONFIG.get("headless"))
        self.assertTrue(reg.DEFAULT_CONFIG.get("cpa_headless"))


if __name__ == "__main__":
    unittest.main()
