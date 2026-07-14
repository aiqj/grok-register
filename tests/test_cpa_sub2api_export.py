#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CPA mint CF detection + Sub2API dual export wiring."""

from __future__ import annotations

import sys
from pathlib import Path as _Path

_SRC = _Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class CloudflareDetectTests(unittest.TestCase):
    def test_block_phrases(self):
        from grok_register.export.cpa_xai.browser_confirm import is_cloudflare_block

        self.assertTrue(
            is_cloudflare_block(
                "https://accounts.x.ai/",
                "Sorry, you have been blocked You are unable to access x.ai",
            )
        )
        self.assertTrue(
            is_cloudflare_block("", "Attention Required! | Cloudflare Why have I been blocked?")
        )
        self.assertFalse(is_cloudflare_block("https://accounts.x.ai/sign-up", "使用邮箱注册"))


class DualExportTests(unittest.TestCase):
    def test_sub2api_from_minimal_cpa(self):
        import grok_register.export.cpa_to_sub2api as cpa_to_sub2api

        # Minimal JWT-like access token with exp claim
        # header.payload.sig — payload {"exp": 9999999999, "email": "a@b.com"}
        import base64

        def b64(obj):
            raw = json.dumps(obj, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        access = f"{b64({'alg':'none'})}.{b64({'exp': 9999999999, 'email': 'a@b.com'})}.x"
        cpa = {
            "type": "xai",
            "email": "a@b.com",
            "access_token": access,
            "refresh_token": "refresh-token-value",
            "id_token": "",
            "expired": "9999999999",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "exports"
            cpa_dir = root / "cpa"
            sub_dir = root / "sub2api"
            cpa_dir.mkdir(parents=True)
            sub_dir.mkdir(parents=True)
            cpa_path = cpa_dir / "xai-a_b.com.json"
            cpa_path.write_text(json.dumps(cpa), encoding="utf-8")
            result = {"ok": True, "path": str(cpa_path), "email": "a@b.com"}
            cfg = {
                "export_root": str(root),
                "sub2api_export_enabled": True,
                "sub2api_export_dir": str(sub_dir),
                "sub2api_combined_file": str(sub_dir / "sub2api-accounts.json"),
                "cpa_auth_dir": str(cpa_dir),
            }
            logs: list[str] = []
            out = cpa_to_sub2api.export_after_cpa_result(
                result, config=cfg, log_callback=logs.append
            )
            self.assertTrue(out.get("ok"), out)
            single = Path(out["path"])
            combined = Path(out["combined_path"])
            self.assertTrue(single.is_file())
            self.assertTrue(combined.is_file())
            doc = json.loads(single.read_text(encoding="utf-8"))
            self.assertEqual(doc.get("type"), "sub2api-data")
            self.assertEqual(doc["accounts"][0]["platform"], "grok")
            self.assertEqual(doc["accounts"][0]["type"], "oauth")
            # Preserve free Build path (matches working Sub2API reference)
            self.assertEqual(
                doc["accounts"][0]["credentials"]["base_url"],
                "https://cli-chat-proxy.grok.com/v1",
            )
            self.assertEqual(out.get("base_url_mode"), "preserve")
            # CPA file still present (both formats)
            self.assertTrue(cpa_path.is_file())


if __name__ == "__main__":
    unittest.main()
