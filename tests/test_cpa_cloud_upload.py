"""Unit tests for online CLIProxyAPI auth-file upload helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from grok_register.core import (
    normalize_cpa_cloud_api_base,
    upload_cpa_auth_dir_to_cloud,
    upload_cpa_auth_file_to_cloud,
)


def test_normalize_cpa_cloud_api_base_variants():
    assert (
        normalize_cpa_cloud_api_base("https://cpa.example.com:8317")
        == "https://cpa.example.com:8317/v0/management"
    )
    assert (
        normalize_cpa_cloud_api_base("https://cpa.example.com:8317/v0/management/")
        == "https://cpa.example.com:8317/v0/management"
    )
    assert (
        normalize_cpa_cloud_api_base(
            "https://cpa.example.com:8317/v0/management/auth-files"
        )
        == "https://cpa.example.com:8317/v0/management"
    )
    assert (
        normalize_cpa_cloud_api_base("cpa.example.com:8317")
        == "http://cpa.example.com:8317/v0/management"
    )
    assert normalize_cpa_cloud_api_base("") == ""


def test_upload_skipped_when_disabled():
    res = upload_cpa_auth_file_to_cloud(
        "/tmp/x.json",
        cfg={"cpa_cloud_upload_enabled": False},
    )
    assert res.get("skipped") is True
    assert res.get("reason") == "disabled"


def test_upload_multipart_success(tmp_path: Path):
    auth = tmp_path / "xai-user@example.com.json"
    auth.write_text(
        json.dumps({"type": "xai", "email": "user@example.com", "refresh_token": "r"}),
        encoding="utf-8",
    )
    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {"status": "ok"}
    mock_res.text = '{"status":"ok"}'

    cfg = {
        "cpa_cloud_upload_enabled": True,
        "cpa_cloud_api_base": "https://cpa.example.com:8317",
        "cpa_cloud_management_key": "secret-key",
        "cpa_cloud_upload_timeout": 10,
        "cpa_cloud_upload_retries": 1,
    }
    with patch("grok_register.core.std_requests.post", return_value=mock_res) as post:
        res = upload_cpa_auth_file_to_cloud(str(auth), cfg=cfg)
    assert res["ok"] is True
    assert res["name"] == "xai-user@example.com.json"
    assert post.called
    args, kwargs = post.call_args
    assert args[0] == "https://cpa.example.com:8317/v0/management/auth-files"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-key"
    assert kwargs["headers"]["X-Management-Key"] == "secret-key"
    assert "file" in kwargs["files"]


def test_upload_no_retry_on_403(tmp_path: Path):
    auth = tmp_path / "xai-a.json"
    auth.write_text("{}", encoding="utf-8")
    mock_res = MagicMock()
    mock_res.status_code = 403
    mock_res.json.return_value = {"error": "remote management disabled"}
    mock_res.text = '{"error":"remote management disabled"}'

    cfg = {
        "cpa_cloud_upload_enabled": True,
        "cpa_cloud_api_base": "http://127.0.0.1:8317",
        "cpa_cloud_management_key": "k",
        "cpa_cloud_upload_retries": 3,
    }
    with patch("grok_register.core.std_requests.post", return_value=mock_res) as post:
        res = upload_cpa_auth_file_to_cloud(str(auth), cfg=cfg)
    assert res["ok"] is False
    assert res["status_code"] == 403
    assert post.call_count == 1


def test_match_cpa_auth_files_fuzzy():
    from grok_register.core import match_cpa_auth_files

    files = [
        {"name": "xai-alice@foo.com.json", "email": "alice@foo.com", "provider": "xai"},
        {"name": "xai-bob@bar.com.json", "email": "bob@bar.com", "provider": "xai"},
        {"name": "claude-user.json", "email": "c@x.com", "provider": "claude"},
    ]
    # substring domain
    m = match_cpa_auth_files(files, ["@foo.com"])
    assert len(m) == 1 and m[0]["email"] == "alice@foo.com"
    # glob
    m = match_cpa_auth_files(files, ["xai-*"])
    assert len(m) == 2
    # regex
    m = match_cpa_auth_files(files, ["re:bob@"])
    assert len(m) == 1
    # multi OR
    m = match_cpa_auth_files(files, ["alice", "claude"])
    assert len(m) == 2


def test_delete_all_dry_run_lists_everything():
    from grok_register.core import delete_all_cpa_auth_files_on_cloud

    fake_files = [
        {"name": "a.json", "email": "a@x.com"},
        {"name": "b.json", "email": "b@x.com"},
    ]
    with patch(
        "grok_register.core.list_cpa_auth_files_on_cloud",
        return_value={"ok": True, "files": fake_files},
    ):
        summary = delete_all_cpa_auth_files_on_cloud(
            cfg={"cpa_cloud_api_base": "http://h", "cpa_cloud_management_key": "k"},
            dry_run=True,
        )
    assert summary["ok"] is True
    assert summary["dry_run"] is True
    assert summary["delete_all"] is True
    assert summary["match_count"] == 2


def test_delete_all_calls_all_true_param():
    from grok_register.core import delete_all_cpa_auth_files_on_cloud

    fake_files = [{"name": "a.json"}]
    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {"status": "ok", "deleted": 1}
    mock_res.text = '{"status":"ok","deleted":1}'

    with patch(
        "grok_register.core.list_cpa_auth_files_on_cloud",
        return_value={"ok": True, "files": fake_files},
    ), patch("grok_register.core.std_requests.delete", return_value=mock_res) as dele:
        summary = delete_all_cpa_auth_files_on_cloud(
            cfg={
                "cpa_cloud_api_base": "http://host:8317",
                "cpa_cloud_management_key": "secret",
            },
            dry_run=False,
        )
    assert summary["ok"] is True
    assert summary.get("delete_all") is True
    assert dele.called
    _args, kwargs = dele.call_args
    assert kwargs.get("params") == {"all": "true"}


def test_batch_upload_force(tmp_path: Path):
    for name in ("xai-a@x.com.json", "xai-b@x.com.json"):
        (tmp_path / name).write_text('{"type":"xai"}', encoding="utf-8")

    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {"status": "ok"}
    mock_res.text = '{"status":"ok"}'

    cfg = {
        "cpa_cloud_upload_enabled": False,  # force bypasses this
        "cpa_cloud_api_base": "http://host:8317",
        "cpa_cloud_management_key": "k",
        "cpa_cloud_upload_retries": 1,
        "cpa_cloud_upload_workers": 4,
    }
    mock_sess = MagicMock()
    mock_sess.post.return_value = mock_res
    with patch("grok_register.core.std_requests.Session", return_value=mock_sess):
        summary = upload_cpa_auth_dir_to_cloud(
            cpa_dir=str(tmp_path), cfg=cfg, force=True
        )
    assert summary["total"] == 2
    assert summary["ok_count"] == 2
    assert summary["fail_count"] == 0
    assert summary["ok"] is True
    assert summary.get("workers") == 2  # capped by file count
    assert mock_sess.post.call_count == 2


def test_batch_upload_workers_one_uses_session(tmp_path: Path):
    (tmp_path / "xai-only@x.com.json").write_text('{"type":"xai"}', encoding="utf-8")
    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.json.return_value = {"status": "ok"}
    mock_res.text = '{"status":"ok"}'
    mock_sess = MagicMock()
    mock_sess.post.return_value = mock_res
    cfg = {
        "cpa_cloud_api_base": "http://host:8317",
        "cpa_cloud_management_key": "k",
        "cpa_cloud_upload_retries": 1,
        "cpa_cloud_upload_workers": 1,
    }
    with patch("grok_register.core.std_requests.Session", return_value=mock_sess):
        summary = upload_cpa_auth_dir_to_cloud(
            cpa_dir=str(tmp_path), cfg=cfg, force=True, workers=1
        )
    assert summary["ok_count"] == 1
    assert summary["workers"] == 1
    assert mock_sess.post.call_count == 1
