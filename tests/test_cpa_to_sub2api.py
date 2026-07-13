import sys
from pathlib import Path as _Path

_SRC = _Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for Sub2API export shape (Wei-Shaw/sub2api data import)."""

import base64
import json
import os
import sys
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import grok_register.export.cpa_to_sub2api as conv  # noqa: E402


def _b64url(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _fake_jwt(payload: dict) -> str:
    return f"{_b64url({'alg':'none'})}.{_b64url(payload)}.sig"


class Sub2APIExportTests(unittest.TestCase):
    def test_platform_is_grok_not_xai(self):
        exp = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
        cpa = {
            "access_token": _fake_jwt({"exp": exp, "sub": "u1", "client_id": "cid"}),
            "refresh_token": "rt",
            "id_token": _fake_jwt({"email": "a@b.com", "sub": "u1"}),
            "email": "a@b.com",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
            "expired": "2030-01-01T00:00:00Z",
        }
        acc = conv.cpa_xai_to_sub2api_account(cpa)
        self.assertEqual(acc["platform"], "grok")
        self.assertEqual(acc["type"], "oauth")
        self.assertNotEqual(acc["platform"], "xai")

    def test_expires_at_is_unix_seconds(self):
        exp = 1893456000  # 2030-01-01
        cpa = {
            "access_token": _fake_jwt({"exp": exp}),
            "refresh_token": "rt",
            "email": "a@b.com",
        }
        acc = conv.cpa_xai_to_sub2api_account(cpa)
        self.assertEqual(acc["expires_at"], exp)
        self.assertLess(acc["expires_at"], 1_000_000_000_000)
        self.assertEqual(acc["credentials"]["expires_at"], "2030-01-01T00:00:00Z")

    def test_base_url_rewritten_from_cli_proxy(self):
        cpa = {
            "access_token": _fake_jwt({"exp": 1893456000}),
            "refresh_token": "rt",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        }
        acc = conv.cpa_xai_to_sub2api_account(cpa)
        self.assertEqual(acc["credentials"]["base_url"], "https://api.x.ai/v1")

    def test_document_has_type_version(self):
        doc = conv.build_sub2api_document([])
        self.assertEqual(doc["type"], "sub2api-data")
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["proxies"], [])
        self.assertEqual(doc["accounts"], [])

    def test_credentials_match_official_fields(self):
        exp = 1893456000
        cpa = {
            "access_token": _fake_jwt(
                {
                    "exp": exp,
                    "sub": "user-1",
                    "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
                    "scope": "openid profile email offline_access",
                }
            ),
            "refresh_token": "refresh",
            "id_token": _fake_jwt({"email": "x@y.z", "sub": "user-1"}),
            "email": "x@y.z",
            "token_type": "Bearer",
            "headers": {"User-Agent": "should-not-export"},
        }
        acc = conv.cpa_xai_to_sub2api_account(cpa)
        creds = acc["credentials"]
        self.assertIn("access_token", creds)
        self.assertIn("refresh_token", creds)
        self.assertIn("id_token", creds)
        self.assertIn("expires_at", creds)
        self.assertIn("client_id", creds)
        self.assertIn("email", creds)
        self.assertIn("base_url", creds)
        self.assertNotIn("headers", creds)
        self.assertNotIn("expired", creds)
        self.assertEqual(acc["extra"]["auth_provider"], "grok")


if __name__ == "__main__":
    unittest.main()
