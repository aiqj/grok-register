"""Unit tests for Sub2API online data-import helpers (v0.1.153 contract)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from grok_register.core import (
    collect_sub2api_export_files,
    normalize_sub2api_cloud_api_base,
    normalize_sub2api_data_document,
    upload_sub2api_data_file_to_cloud,
)


def test_normalize_sub2api_cloud_api_base():
    assert (
        normalize_sub2api_cloud_api_base("https://s2.example.com:8080")
        == "https://s2.example.com:8080"
    )
    assert (
        normalize_sub2api_cloud_api_base("https://s2.example.com/api/v1/admin")
        == "https://s2.example.com"
    )
    assert normalize_sub2api_cloud_api_base("s2.example.com") == "http://s2.example.com"


def test_normalize_sub2api_data_document_wraps_and_fixes_platform():
    doc = {
        "type": "sub2api-data",
        "version": 1,
        "accounts": [
            {
                "name": "a@x.com",
                "platform": "xai",
                "type": "oauth",
                "credentials": {"access_token": "t", "refresh_token": "r"},
            }
        ],
    }
    out, err = normalize_sub2api_data_document(doc)
    assert err is None
    assert out["proxies"] == []
    assert out["accounts"][0]["platform"] == "grok"
    assert out["type"] == "sub2api-data"
    assert out["version"] == 1


def test_upload_sub2api_posts_accounts_data_envelope(tmp_path: Path):
    f = tmp_path / "sub2api-accounts.json"
    f.write_text(
        json.dumps(
            {
                "type": "sub2api-data",
                "version": 1,
                "proxies": [],
                "accounts": [
                    {
                        "name": "u@x.com",
                        "platform": "grok",
                        "type": "oauth",
                        "concurrency": 1,
                        "priority": 50,
                        "credentials": {
                            "access_token": "a",
                            "refresh_token": "r",
                            "base_url": "https://api.x.ai/v1",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {
        "code": 0,
        "message": "success",
        "data": {
            "account_created": 1,
            "account_failed": 0,
            "proxy_created": 0,
            "errors": [],
        },
    }
    mock_res.text = '{"code":0}'

    cfg = {
        "sub2api_cloud_upload_enabled": True,
        "sub2api_cloud_api_base": "https://s2.example.com",
        "sub2api_cloud_admin_key": "admin-key",
        "sub2api_cloud_skip_default_group_bind": False,
        "sub2api_cloud_retries": 1,
    }
    with patch("grok_register.core.std_requests.post", return_value=mock_res) as post:
        res = upload_sub2api_data_file_to_cloud(str(f), cfg=cfg)
    assert res["ok"] is True
    assert res["account_created"] == 1
    args, kwargs = post.call_args
    assert args[0] == "https://s2.example.com/api/v1/admin/accounts/data"
    assert kwargs["headers"]["x-api-key"] == "admin-key"
    assert "Idempotency-Key" in kwargs["headers"]
    body = kwargs["json"]
    assert body["skip_default_group_bind"] is False
    assert body["data"]["type"] == "sub2api-data"
    assert body["data"]["proxies"] == []
    assert len(body["data"]["accounts"]) == 1


def test_collect_prefers_combined(tmp_path: Path):
    d = tmp_path / "sub2api"
    d.mkdir()
    (d / "sub2api-accounts.json").write_text("{}", encoding="utf-8")
    (d / "sub2api-xai-a.json").write_text("{}", encoding="utf-8")
    paths = collect_sub2api_export_files(sub2api_dir=str(tmp_path), prefer_combined=True)
    assert any(p.endswith("sub2api-accounts.json") for p in paths)
    # combined preferred → may only return combined from that dir
    assert len(paths) >= 1
