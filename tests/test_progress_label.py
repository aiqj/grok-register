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

from grok_register.cli import format_progress_label  # noqa: E402


class ProgressLabelTests(unittest.TestCase):
    def test_extra_batch_not_global_total(self):
        # --extra 10 with 3 already done: last account should be 10/10 not 13/13
        label = format_progress_label(
            13, done_at_start=3, batch_total=10, target_total=13
        )
        self.assertIn("本批第 10/10", label)
        self.assertIn("全局序号 13", label)
        self.assertIn("启动时已有 3", label)
        self.assertNotIn("第 13/13", label)

    def test_first_of_extra_batch(self):
        label = format_progress_label(
            4, done_at_start=3, batch_total=10, target_total=13
        )
        self.assertIn("本批第 1/10", label)
        self.assertIn("全局序号 4", label)

    def test_plain_count_without_batch(self):
        label = format_progress_label(3, target_total=10)
        self.assertEqual(label, "第 3/10 个账号")


if __name__ == "__main__":
    unittest.main()
