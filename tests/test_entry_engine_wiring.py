#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Static wiring checks: entrypoints route through RegistrationEngine."""

from __future__ import annotations

import sys
from pathlib import Path as _Path

_SRC = _Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EntryEngineWiringTests(unittest.TestCase):
    def test_root_entry_delegates(self):
        path = os.path.join(ROOT, "register_cli.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("grok_register.cli", src)
        self.assertIn("sys.path", src)

    def test_register_cli_uses_engine(self):
        path = os.path.join(ROOT, "src", "grok_register", "cli.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("RegistrationEngine", src)
        self.assertIn("engine.register_one", src)
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "register_one":
                body_src = ast.get_source_segment(src, node) or ""
                self.assertIn("RegistrationEngine", body_src)
                self.assertNotIn("fill_email_and_submit", body_src)
                break
        else:
            self.fail("register_one not found")

    def test_core_uses_engine(self):
        path = os.path.join(ROOT, "src", "grok_register", "core.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("RegistrationEngine", src)
        self.assertIn("def run_registration_cli", src)
        self.assertNotIn("class GrokRegisterGUI", src)
        self.assertNotIn("import tkinter", src)
        self.assertGreaterEqual(src.count("engine.register_one"), 1)

    def test_no_root_shims(self):
        for name in (
            "proxy_pool.py",
            "mail_pool.py",
            "cpa_export.py",
            "grok_register_ttk.py",
            "tab_pool.py",
            "cf_prewarm.py",
            "local_auth_proxy.py",
            "cpa_to_sub2api.py",
        ):
            self.assertFalse(
                os.path.isfile(os.path.join(ROOT, name)),
                f"legacy shim should be removed: {name}",
            )
        self.assertFalse(os.path.isdir(os.path.join(ROOT, "cpa_xai")))


if __name__ == "__main__":
    unittest.main()
