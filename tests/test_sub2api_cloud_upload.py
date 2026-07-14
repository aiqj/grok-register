"""Unit tests for Sub2API online data-import helpers (v0.1.153 contract)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import base64
import time

from grok_register.core import (
    analyze_sub2api_oauth_account,
    collect_sub2api_export_files,
    delete_sub2api_account_on_cloud,
    delete_sub2api_accounts_on_cloud,
    delete_sub2api_latest_batch_on_cloud,
    filter_sub2api_document_by_token_health,
    list_sub2api_accounts_on_cloud,
    match_sub2api_accounts,
    normalize_sub2api_cloud_api_base,
    normalize_sub2api_data_document,
    upload_sub2api_data_file_to_cloud,
)


def _jwt(exp: int) -> str:
    def b64(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp, 'sub': 'u1'})}.sig"


def test_analyze_sub2api_oauth_account_expired():
    now = int(time.time())
    acc = {
        "name": "a@x.com",
        "credentials": {
            "access_token": _jwt(now - 100),
            "refresh_token": "rt",
            "email": "a@x.com",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        },
    }
    rep = analyze_sub2api_oauth_account(acc, now=now, skew_sec=0)
    assert rep["access_expired"] is True
    assert rep["remint_recommended"] is True
    assert "access_token_expired" in rep["risks"]
    assert "will_refresh_on_use" in rep["risks"]


def test_analyze_sub2api_oauth_account_healthy():
    now = int(time.time())
    acc = {
        "name": "a@x.com",
        "credentials": {
            "access_token": _jwt(now + 10_000),
            "refresh_token": "rt",
            "email": "a@x.com",
        },
    }
    rep = analyze_sub2api_oauth_account(acc, now=now, skew_sec=120, soon_sec=3600)
    assert rep["healthy"] is True
    assert rep["remint_recommended"] is False


def test_filter_skips_unhealthy_accounts():
    now = int(time.time())
    doc = {
        "type": "sub2api-data",
        "version": 1,
        "proxies": [],
        "accounts": [
            {
                "name": "dead@x.com",
                "credentials": {
                    "access_token": _jwt(now - 50),
                    "refresh_token": "rt",
                    "email": "dead@x.com",
                },
            },
            {
                "name": "live@x.com",
                "credentials": {
                    "access_token": _jwt(now + 20_000),
                    "refresh_token": "rt",
                    "email": "live@x.com",
                },
            },
        ],
    }
    cfg = {
        "sub2api_upload_check_tokens": True,
        "sub2api_upload_skip_unhealthy": True,
        "sub2api_token_skew_sec": 0,
        "sub2api_token_soon_sec": 3600,
    }
    # inject now via analyzing with patched time is hard; use skew so expired is clear
    out, report = filter_sub2api_document_by_token_health(doc, cfg=cfg, now=now)
    assert report["skipped_count"] == 1
    assert report["kept_count"] == 1
    assert out["accounts"][0]["name"] == "live@x.com"


def test_upload_skips_when_all_unhealthy(tmp_path: Path):
    now = int(time.time())
    f = tmp_path / "sub2api-accounts.json"
    f.write_text(
        json.dumps(
            {
                "type": "sub2api-data",
                "version": 1,
                "proxies": [],
                "accounts": [
                    {
                        "name": "dead@x.com",
                        "platform": "grok",
                        "type": "oauth",
                        "credentials": {
                            "access_token": _jwt(now - 100),
                            "refresh_token": "rt",
                            "email": "dead@x.com",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = {
        "sub2api_cloud_upload_enabled": True,
        "sub2api_cloud_api_base": "https://s2.example.com",
        "sub2api_cloud_admin_key": "admin-key",
        "sub2api_upload_check_tokens": True,
        "sub2api_upload_skip_unhealthy": True,
        "sub2api_token_skew_sec": 0,
        "sub2api_cloud_retries": 1,
    }
    with patch("grok_register.core.std_requests.post") as post:
        res = upload_sub2api_data_file_to_cloud(str(f), cfg=cfg)
    assert res.get("skipped") is True
    assert res.get("error") == "all_accounts_unhealthy"
    assert post.call_count == 0


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


def test_match_sub2api_accounts_by_email_substring():
    accounts = [
        {
            "id": 1,
            "name": "alice@x.com",
            "platform": "grok",
            "type": "oauth",
            "credentials": {"email": "alice@x.com", "base_url": "https://cli-chat-proxy.grok.com/v1"},
        },
        {
            "id": 2,
            "name": "bob",
            "platform": "grok",
            "type": "oauth",
            "extra": {"email": "bob@y.com"},
        },
    ]
    hit = match_sub2api_accounts(accounts, ["@x.com"])
    assert len(hit) == 1
    assert hit[0]["id"] == 1
    hit2 = match_sub2api_accounts(accounts, ["bob@y.com"])
    assert len(hit2) == 1 and hit2[0]["id"] == 2


def test_list_sub2api_accounts_paginates():
    page1 = MagicMock()
    page1.status_code = 200
    page1.json.return_value = {
        "code": 0,
        "data": {
            "items": [
                {"id": 1, "name": "a@x.com", "platform": "grok", "type": "oauth"},
                {"id": 2, "name": "b@x.com", "platform": "grok", "type": "oauth"},
            ],
            "total": 3,
            "page": 1,
            "page_size": 2,
            "pages": 2,
        },
    }
    page1.text = "{}"
    page2 = MagicMock()
    page2.status_code = 200
    page2.json.return_value = {
        "code": 0,
        "data": {
            "items": [
                {"id": 3, "name": "c@x.com", "platform": "grok", "type": "oauth"},
            ],
            "total": 3,
            "page": 2,
            "page_size": 2,
            "pages": 2,
        },
    }
    page2.text = "{}"
    cfg = {
        "sub2api_cloud_api_base": "https://s2.example.com",
        "sub2api_cloud_admin_key": "k",
    }
    with patch(
        "grok_register.core.std_requests.get", side_effect=[page1, page2]
    ) as get:
        res = list_sub2api_accounts_on_cloud(cfg=cfg, page_size=2)
    assert res["ok"] is True
    assert len(res["accounts"]) == 3
    assert get.call_count == 2
    assert get.call_args_list[0][1]["params"]["platform"] == "grok"


def test_delete_sub2api_account_by_id():
    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {"code": 0, "message": "success", "data": {"message": "ok"}}
    mock_res.text = "{}"
    cfg = {
        "sub2api_cloud_api_base": "https://s2.example.com",
        "sub2api_cloud_admin_key": "k",
    }
    with patch("grok_register.core.std_requests.delete", return_value=mock_res) as delete:
        res = delete_sub2api_account_on_cloud(42, cfg=cfg)
    assert res["ok"] is True
    assert res["id"] == 42
    assert delete.call_args[0][0] == "https://s2.example.com/api/v1/admin/accounts/42"


def test_delete_sub2api_latest_batch_uses_local_tokens(tmp_path: Path):
    batch = tmp_path / "20260714_120000"
    sub = batch / "sub2api"
    sub.mkdir(parents=True)
    (sub / "sub2api-accounts.json").write_text(
        json.dumps(
            {
                "type": "sub2api-data",
                "version": 1,
                "proxies": [],
                "accounts": [
                    {
                        "name": "a@x.com",
                        "platform": "grok",
                        "type": "oauth",
                        "credentials": {"email": "a@x.com", "access_token": "t"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    listed = {
        "ok": True,
        "accounts": [
            {
                "id": 7,
                "name": "a@x.com",
                "platform": "grok",
                "type": "oauth",
                "credentials": {"email": "a@x.com"},
            }
        ],
        "total": 1,
    }
    with patch("grok_register.core.list_sub2api_accounts_on_cloud", return_value=listed):
        res = delete_sub2api_latest_batch_on_cloud(
            sub2api_dir=str(batch),
            cfg={
                "sub2api_cloud_api_base": "https://s2.example.com",
                "sub2api_cloud_admin_key": "k",
            },
            dry_run=True,
        )
    assert res["ok"] is True
    assert res["dry_run"] is True
    assert res["match_count"] == 1
    assert "a@x.com" in (res.get("local_tokens") or [])


def test_delete_sub2api_accounts_dry_run_and_execute():
    listed = {
        "ok": True,
        "accounts": [
            {
                "id": 10,
                "name": "u@x.com",
                "platform": "grok",
                "type": "oauth",
                "credentials": {"email": "u@x.com"},
            }
        ],
        "total": 1,
        "api_base": "https://s2.example.com",
    }
    cfg = {
        "sub2api_cloud_api_base": "https://s2.example.com",
        "sub2api_cloud_admin_key": "k",
    }
    with patch("grok_register.core.list_sub2api_accounts_on_cloud", return_value=listed):
        dry = delete_sub2api_accounts_on_cloud(
            patterns=["@x.com"], cfg=cfg, dry_run=True
        )
        assert dry["dry_run"] is True
        assert dry["match_count"] == 1
        assert dry["deleted"] == []

    mock_del = MagicMock()
    mock_del.status_code = 200
    mock_del.json.return_value = {"code": 0, "data": {}}
    mock_del.text = "{}"
    with patch("grok_register.core.list_sub2api_accounts_on_cloud", return_value=listed):
        with patch("grok_register.core.std_requests.delete", return_value=mock_del):
            real = delete_sub2api_accounts_on_cloud(
                patterns=["@x.com"], cfg=cfg, dry_run=False
            )
    assert real["ok"] is True
    assert real["ok_count"] == 1
    assert real["deleted"][0]["id"] == 10
