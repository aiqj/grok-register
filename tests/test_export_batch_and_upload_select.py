"""Per-batch export layout + selective CPA cloud upload path collection."""

from __future__ import annotations

from pathlib import Path

from grok_register.core import collect_cpa_auth_files
from grok_register.export.cpa_export import (
    apply_batch_export_layout,
    find_latest_export_batch,
    make_batch_id,
    resolve_export_dirs,
)


def test_make_batch_id_with_name():
    bid = make_batch_id("demo run!")
    # YYYYMMDD_HHMMSS[_name] — no batch_ prefix
    assert not bid.startswith("batch_")
    assert len(bid) >= len("20260712_153045")
    assert bid.endswith("demo_run") or "demo" in bid


def test_apply_batch_export_layout(tmp_path: Path):
    cfg: dict = {
        "export_batch_parent": str(tmp_path),
        "export_batch_also_global_accounts": True,
    }
    meta = apply_batch_export_layout(cfg, batch_name="t1")
    batch_dir = Path(meta["batch_dir"])
    assert batch_dir.is_dir()
    assert (batch_dir / "cpa").is_dir()
    assert (batch_dir / "sub2api").is_dir()
    assert (batch_dir / "accounts.txt").is_file()
    assert (batch_dir / "meta.json").is_file()
    assert cfg["cpa_auth_dir"] == str(batch_dir / "cpa")
    assert cfg["sub2api_export_dir"] == str(batch_dir / "sub2api")
    root, cpa, sub = resolve_export_dirs(cfg)
    assert root == batch_dir
    assert cpa == batch_dir / "cpa"
    assert sub == batch_dir / "sub2api"


def test_collect_files_explicit_and_recursive(tmp_path: Path):
    b1 = tmp_path / "batch_a" / "cpa"
    b2 = tmp_path / "batch_b" / "cpa"
    b1.mkdir(parents=True)
    b2.mkdir(parents=True)
    f1 = b1 / "xai-a@x.com.json"
    f2 = b2 / "xai-b@x.com.json"
    f1.write_text("{}", encoding="utf-8")
    f2.write_text("{}", encoding="utf-8")
    (b1 / "meta.json").write_text("{}", encoding="utf-8")  # should skip if scanned as json

    only = collect_cpa_auth_files(files=[str(f1)], cfg={})
    assert only == [str(f1.resolve())]

    batch_only = collect_cpa_auth_files(cpa_dir=str(tmp_path / "batch_a"), cfg={})
    assert str(f1.resolve()) in batch_only
    assert str(f2.resolve()) not in batch_only

    all_files = collect_cpa_auth_files(cpa_dir=str(tmp_path), recursive=True, cfg={})
    assert set(all_files) == {str(f1.resolve()), str(f2.resolve())}


def test_find_latest_export_batch(tmp_path: Path, monkeypatch):
    import time

    older = tmp_path / "20260712_100000"
    newer = tmp_path / "20260712_150000"
    (older / "cpa").mkdir(parents=True)
    (newer / "cpa").mkdir(parents=True)
    (older / "cpa" / "xai-old@x.com.json").write_text("{}", encoding="utf-8")
    f_new = newer / "cpa" / "xai-new@x.com.json"
    f_new.write_text("{}", encoding="utf-8")
    # ensure newer file mtime wins
    now = time.time()
    os_utime = __import__("os").utime
    os_utime(older / "cpa" / "xai-old@x.com.json", (now - 100, now - 100))
    os_utime(f_new, (now, now))

    # skip flat legacy dirs
    (tmp_path / "cpa").mkdir()
    (tmp_path / "cpa" / "xai-flat@x.com.json").write_text("{}", encoding="utf-8")

    latest = find_latest_export_batch({"export_batch_parent": str(tmp_path)})
    assert latest is not None
    assert latest.name == "20260712_150000"
