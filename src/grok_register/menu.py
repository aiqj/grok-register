"""Interactive menu for grok-register.

Entry:
  python register_cli.py
  python register_cli.py --menu
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

MENU_VERSION = "1.4.1"

# Returned by nested menus when user chooses "返回" — main loop must NOT pause.
MENU_BACK: Any = object()


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _rule(char: str = "─", width: int = 64) -> None:
    _p(char * width)


def _title(text: str) -> None:
    """Legacy simple title (actions / help). Prefer _submenu_screen for menus."""
    _p()
    _rule()
    _p(f"  {text}")
    _rule()
    _p()


def _submenu_screen(title: str, items: list[tuple[str, str]]) -> None:
    """Render a nested menu with the same visual language as the root menu.

    Root style:
      ==== title ====
      请选择
        1. xxx
        0. 返回/退出
      ────
      编号 [1]:
    """
    _p()
    _rule("=")
    _p(f"  {title}")
    _rule("=")
    _p()
    _p("  请选择")
    _p()
    for key, label in items:
        _p(f"    {key}. {label}")
    _p()
    _rule()


def _pause() -> None:
    try:
        input("\n  按回车继续… ")
    except (EOFError, KeyboardInterrupt):
        _p()


def _ask(prompt: str, default: str | None = None) -> str:
    hint = f" [{default}]" if default not in (None, "") else ""
    try:
        raw = input(f"  {prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        _p()
        raise
    if not raw and default is not None:
        return str(default)
    return raw


def _ask_int(prompt: str, default: int, lo: int = 1, hi: int = 10000) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            n = int(raw)
        except ValueError:
            _p("  → 请输入整数")
            continue
        if lo <= n <= hi:
            return n
        _p(f"  → 范围 {lo}–{hi}")


def _ask_yes(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    raw = _ask(f"{prompt} ({d})", "").lower()
    if not raw:
        return default
    return raw in ("y", "yes", "1", "是")


def _run_cli(argv: list[str]) -> int:
    from grok_register import cli as cli_mod

    old = sys.argv[:]
    try:
        sys.argv = [old[0] if old else "register_cli.py", *argv]
        sys.argv = [
            a for a in sys.argv if a not in ("--menu", "-i", "--interactive")
        ]
        return int(cli_mod.main() or 0)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old


def _cfg() -> dict:
    try:
        from grok_register import core as reg

        reg.load_config()
        return getattr(reg, "config", {}) or {}
    except Exception:
        return {}


def _pick(cfg: dict, key: str, *env_keys: str) -> str:
    v = str(cfg.get(key) or "").strip()
    if v:
        return v
    for e in env_keys:
        v = str(os.environ.get(e) or "").strip()
        if v:
            return v
    return ""


def _host_only(base: str) -> str:
    base = (base or "").strip()
    if not base:
        return "未配置"
    return base.replace("https://", "").replace("http://", "").rstrip("/")


def _home_screen() -> None:
    cfg = _cfg()

    cpa_base = _host_only(
        _pick(cfg, "cpa_cloud_api_base", "CPA_CLOUD_API_BASE")
    )
    s2_base = _host_only(
        _pick(
            cfg,
            "sub2api_cloud_api_base",
            "SUB2API_BASE_URL",
            "SUB2API_CLOUD_API_BASE",
        )
    )
    email = str(cfg.get("email_provider") or "-")

    _p()
    _rule("=")
    _p("  Grok Register")
    _p(f"  交互菜单  v{MENU_VERSION}")
    _rule("=")
    _p()
    _p("  当前配置：")
    _p(f"    Email        {email}")
    _p(f"    CPA          {cpa_base}")
    _p(f"    Sub2API      {s2_base}")
    _p()
    _rule()
    _p()
    _p("  请选择")
    _p()
    _p("    1. 注册账号")
    _p("    2. 补缺 CPA / Sub2API")
    _p("    3. 上传到线上")
    _p("    4. 管理线上 CPA 凭证")
    _p("    5. 管理线上 Sub2API 账号")
    _p("    6. 说明")
    _p("    0. 退出")
    _p()
    _rule()


def _find_latest_cpa_batch() -> Path | None:
    cfg = _cfg()
    try:
        from grok_register.export.cpa_export import find_latest_export_batch

        return find_latest_export_batch(cfg, require_cpa_files=True)
    except Exception:
        parent = Path(
            cfg.get("export_batch_parent") or cfg.get("export_root") or "./exports"
        )
        if not parent.is_absolute():
            try:
                from grok_register.paths import PROJECT_ROOT

                parent = (PROJECT_ROOT / parent).resolve()
            except Exception:
                parent = parent.resolve()
        if not parent.is_dir():
            return None
        cands = sorted(
            [
                p
                for p in parent.iterdir()
                if p.is_dir()
                and (
                    (p / "cpa").is_dir()
                    and any((p / "cpa").glob("xai-*.json"))
                    or any(p.glob("xai-*.json"))
                )
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return cands[0] if cands else None


def _cpa_patterns_from_batch(batch: Path) -> list[str]:
    """Emails / filenames for fuzzy delete of this batch's CPA files."""
    cpa_dir = batch / "cpa" if (batch / "cpa").is_dir() else batch
    patterns: list[str] = []
    for p in sorted(cpa_dir.glob("xai-*.json")):
        stem = p.stem
        email = stem[4:] if stem.startswith("xai-") else stem
        if email:
            patterns.append(email)
        patterns.append(p.name)
    # stable unique
    return list(dict.fromkeys(patterns))


# ── actions ──────────────────────────────────────────────────────────────────


def _menu_register() -> int:
    _title("注册账号")
    count = _ask_int("本批数量", 1, 1, 500)
    threads = _ask_int("并发线程", 1, 1, 10)
    headed = _ask_yes("有界面浏览器（过 CF 更稳）", True)
    batch_name = _ask("批次名后缀，可空", "")

    argv = ["--count", str(count), "--threads", str(threads)]
    if headed:
        argv.append("--headed")
    if batch_name:
        argv.extend(["--batch-name", batch_name])

    _p()
    _p(f"  → register_cli.py {' '.join(argv)}")
    _p()
    return _run_cli(argv)


def _menu_remint() -> int:
    _title("补缺 CPA / Sub2API")
    _p("  读取 accounts，按缺什么补什么：")
    _p("    · 无 CPA（xai-*.json）      → 浏览器 remint OIDC")
    _p("    · 有 CPA、无 Sub2API 文件    → 本地转换（不打开浏览器）")
    _p("    · remint 成功时会顺带写 Sub2API（若开启导出）")
    _p()
    headed = _ask_yes("缺 CPA 时用有界面浏览器", True)
    batch_dir = _ask("写入指定批次目录，可空", "")

    argv = ["--remint-missing"]
    if headed:
        argv.append("--headed")
    if batch_dir:
        argv.extend(["--batch-dir", batch_dir])

    _p()
    _p(f"  → register_cli.py {' '.join(argv)}")
    _p()
    return _run_cli(argv)


def _scope_menu() -> str | None:
    _submenu_screen(
        "选择上传范围",
        [
            ("1", "最新一批"),
            ("2", "exports 下全部批次"),
            ("3", "指定目录"),
            ("4", "指定文件"),
            ("0", "返回"),
        ],
    )
    c = _ask("编号", "1")
    return {
        "1": "latest",
        "2": "all",
        "3": "dir",
        "4": "files",
        "0": None,
    }.get(c)


def _cpa_argv(scope: str) -> list[str] | None:
    if scope == "latest":
        return ["--cpa-upload-latest"]
    if scope == "all":
        return ["--upload-cpa-cloud", "--cpa-upload-all"]
    if scope == "dir":
        path = _ask("目录路径", "")
        return ["--upload-cpa-cloud", "--cpa-upload-dir", path] if path else None
    if scope == "files":
        path = _ask("文件路径（空格分隔多个）", "")
        return (
            ["--upload-cpa-cloud", "--cpa-upload-files", *path.split()]
            if path
            else None
        )
    return None


def _s2_argv(scope: str) -> list[str] | None:
    if scope == "latest":
        return ["--sub2api-upload-latest"]
    if scope == "all":
        return ["--upload-sub2api-cloud", "--sub2api-upload-all"]
    if scope == "dir":
        path = _ask("目录路径", "")
        return (
            ["--upload-sub2api-cloud", "--sub2api-upload-dir", path] if path else None
        )
    if scope == "files":
        path = _ask("文件路径（空格分隔多个）", "")
        return (
            ["--upload-sub2api-cloud", "--sub2api-upload-files", *path.split()]
            if path
            else None
        )
    return None


def _menu_upload() -> Any:
    """Upload hub. Returns MENU_BACK when user cancels without running."""
    _submenu_screen(
        "上传到线上",
        [
            ("1", "全部（CPA + Sub2API）"),
            ("2", "仅 CPA"),
            ("3", "仅 Sub2API"),
            ("0", "返回"),
        ],
    )
    target = _ask("编号", "1")
    if target == "0":
        return MENU_BACK
    if target not in ("1", "2", "3"):
        _p("  → 无效选项")
        return 0

    scope = _scope_menu()
    if not scope:
        return MENU_BACK

    codes: list[int] = []
    if target in ("1", "2"):
        argv = _cpa_argv(scope)
        if argv:
            _p()
            _p(f"  → CPA: {' '.join(argv)}")
            _p()
            codes.append(_run_cli(argv))
    if target in ("1", "3"):
        argv = _s2_argv(scope)
        if argv:
            _p()
            _p(f"  → Sub2API: {' '.join(argv)}")
            _p()
            codes.append(_run_cli(argv))
    if not codes:
        return MENU_BACK
    bad = [c for c in codes if c]
    return bad[0] if bad else 0


def _cpa_replace_latest() -> int:
    """Delete online CPA creds matching latest batch, then upload latest."""
    batch = _find_latest_cpa_batch()
    if batch is None:
        _p("  → 未找到含 xai-*.json 的最新批次")
        return 1
    patterns = _cpa_patterns_from_batch(batch)
    if not patterns:
        _p(f"  → 批次 {batch.name} 下没有 CPA 文件")
        return 1

    _p(f"  最新批次: {batch.name}")
    _p(f"  路径:     {batch}")
    _p(f"  本地 CPA: {len([p for p in patterns if p.endswith('.json')])} 个文件")
    _p("  将：按 email/文件名模糊删除线上匹配 → 再上传最新批")
    _p()
    _p("  — 删除预览 —")
    # unique emails only for cleaner delete argv (filenames also match)
    del_patterns = [p for p in patterns if not p.endswith(".json")]
    if not del_patterns:
        del_patterns = patterns
    code = _run_cli(["--cpa-delete", *del_patterns])
    if not _ask_yes("确认删除匹配并上传最新批", False):
        return code
    _p()
    code_del = _run_cli(["--cpa-delete", *del_patterns, "--yes"])
    _p()
    code_up = _run_cli(["--cpa-upload-latest"])
    return code_up or code_del


def _menu_cpa_manage() -> Any:
    """CPA online manage loop. 0 → MENU_BACK (no main-menu pause)."""
    last = 0
    while True:
        _submenu_screen(
            "管理线上 CPA 凭证",
            [
                ("1", "列出凭证"),
                ("2", "模糊删除（先预览）"),
                ("3", "全部删除（先预览）"),
                ("4", "上传最新批"),
                ("5", "上传全部批次"),
                ("6", "替换最新批（先删匹配再上传）"),
                ("0", "返回"),
            ],
        )
        c = _ask("编号", "1")
        if c in ("0", "q", "Q"):
            return MENU_BACK
        if c == "1":
            _p()
            last = _run_cli(["--cpa-list"])
            _pause()
            continue
        if c == "2":
            _p()
            _p("  支持：子串、glob（如 xai-*）、re:正则；多个用空格表示或")
            patterns = _ask("匹配", "")
            if not patterns:
                continue
            parts = patterns.split()
            _p()
            _p("  — 预览 —")
            code = _run_cli(["--cpa-delete", *parts])
            if not _ask_yes("确认删除", False):
                last = code
                _pause()
                continue
            _p()
            last = _run_cli(["--cpa-delete", *parts, "--yes"])
            _pause()
            continue
        if c == "3":
            _p()
            _p("  — 预览 —")
            code = _run_cli(["--cpa-delete-all"])
            if not _ask_yes("确认清空线上全部 CPA 凭证", False):
                last = code
                _pause()
                continue
            if _ask("再输入 DELETE 确认", "") != "DELETE":
                _p("  → 已取消")
                _pause()
                continue
            _p()
            last = _run_cli(["--cpa-delete-all", "--yes"])
            _pause()
            continue
        if c == "4":
            _p()
            _p("  → --cpa-upload-latest")
            _p()
            last = _run_cli(["--cpa-upload-latest"])
            _pause()
            continue
        if c == "5":
            _p()
            _p("  → --upload-cpa-cloud --cpa-upload-all")
            _p()
            last = _run_cli(["--upload-cpa-cloud", "--cpa-upload-all"])
            _pause()
            continue
        if c == "6":
            _p()
            last = _cpa_replace_latest()
            _pause()
            continue
        _p("  → 无效编号")
        _pause()


def _menu_sub2api_manage() -> Any:
    """Sub2API online manage loop. 0 → MENU_BACK."""
    last = 0
    while True:
        _submenu_screen(
            "管理线上 Sub2API 账号",
            [
                ("1", "列出账号（platform=grok）"),
                ("2", "模糊删除（先预览）"),
                ("3", "删除最新批（先预览）"),
                ("4", "删除全部 grok（先预览）"),
                ("5", "上传最新批"),
                ("6", "上传全部批次"),
                ("7", "替换最新批（先删匹配再上传）"),
                ("0", "返回"),
            ],
        )
        c = _ask("编号", "1")
        if c in ("0", "q", "Q"):
            return MENU_BACK
        if c == "1":
            _p()
            last = _run_cli(["--sub2api-list"])
            _pause()
            continue
        if c == "2":
            _p()
            _p("  支持：子串、glob、re:正则；匹配 name / email / id")
            patterns = _ask("匹配", "")
            if not patterns:
                continue
            parts = patterns.split()
            _p()
            _p("  — 预览 —")
            code = _run_cli(["--sub2api-delete", *parts])
            if not _ask_yes("确认删除", False):
                last = code
                _pause()
                continue
            _p()
            last = _run_cli(["--sub2api-delete", *parts, "--yes"])
            _pause()
            continue
        if c == "3":
            _p()
            _p("  按本地最新批 exports/<批次>/sub2api 的 email/name")
            _p("  删除线上匹配账号（只删不传）")
            _p()
            _p("  — 预览 —")
            code = _run_cli(["--sub2api-delete-latest"])
            if not _ask_yes("确认删除线上匹配的最新批账号", False):
                last = code
                _pause()
                continue
            _p()
            last = _run_cli(["--sub2api-delete-latest", "--yes"])
            _pause()
            continue
        if c == "4":
            _p()
            _p("  — 预览 —")
            code = _run_cli(["--sub2api-delete-all"])
            if not _ask_yes("确认删除线上全部 platform=grok 账号", False):
                last = code
                _pause()
                continue
            if _ask("再输入 DELETE 确认", "") != "DELETE":
                _p("  → 已取消")
                _pause()
                continue
            _p()
            last = _run_cli(["--sub2api-delete-all", "--yes"])
            _pause()
            continue
        if c == "5":
            _p()
            _p("  → --sub2api-upload-latest")
            _p()
            last = _run_cli(["--sub2api-upload-latest"])
            _pause()
            continue
        if c == "6":
            _p()
            _p("  → --upload-sub2api-cloud --sub2api-upload-all")
            _p()
            last = _run_cli(["--upload-sub2api-cloud", "--sub2api-upload-all"])
            _pause()
            continue
        if c == "7":
            _p()
            _p("  按本地最新批 email/name 删除线上匹配账号，再上传最新批")
            _p("  （Sub2API 导入只能新建，不能覆盖 credentials）")
            _p()
            _p("  — 预览 —")
            code = _run_cli(["--sub2api-replace-latest"])
            if not _ask_yes("确认删除匹配并重新上传", False):
                last = code
                _pause()
                continue
            _p()
            last = _run_cli(["--sub2api-replace-latest", "--yes"])
            _pause()
            continue
        _p("  → 无效编号")
        _pause()


def _menu_help() -> int:
    _title("说明")
    _p(
        """  流程
    注册 → 本地 exports/<批次>/{accounts,cpa,sub2api}
         → 可选上传 CPA / Sub2API

  配置（config.json）
    CPA 云    cpa_cloud_api_base
              cpa_cloud_management_key
              cpa_cloud_upload_enabled
    Sub2API云 sub2api_cloud_api_base
              sub2api_cloud_admin_key
              sub2api_cloud_upload_enabled
    mint      cpa_mint_prefer_sso_build（默认 true）

  注册后 CPA mint
    默认优先 SSO→Build（HTTP 自动设备码批准，通常无浏览器设备码页）
    失败或 cpa_mint_prefer_sso_build=false → 浏览器设备码
    日志：mint try SSO→Build / fallback browser device

  菜单
    3 上传到线上     通用上传（CPA / Sub2API）
    4 管理 CPA       列表 / 删除 / 上传最新|全部 / 替换最新批
    5 管理 Sub2API   列表 / 删除 / 上传 / 替换最新批
    子菜单输入 0     返回上级（不再多按一次回车）

  命令行（可选）
    register_cli.py --count 5 --headed
    register_cli.py --cpa-upload-latest
    register_cli.py --sub2api-upload-latest
    register_cli.py --sub2api-list
    register_cli.py --sub2api-delete @example.com --yes
    register_cli.py --sub2api-delete-latest --yes
    register_cli.py --sub2api-replace-latest --yes

  注意
    Sub2API 导入不覆盖已有账号；要更新 base_url 须先删后导。
    注册/导入成功 ≠ 一定能调 grok 对话（见 403 文档）。

  文档
    docs/export-cpa-and-sub2api.md   # mint 双路径 §3.1
    docs/grok-403-investigation.md   # 对话 403 排查记录
    docs/registration.md
"""
    )
    return 0


# Main menu: fewer top-level items, nested where needed
_ITEMS: list[tuple[str, str, Callable[[], Any]]] = [
    ("1", "注册账号", _menu_register),
    ("2", "补缺 CPA / Sub2API", _menu_remint),
    ("3", "上传到线上", _menu_upload),
    ("4", "管理线上 CPA 凭证", _menu_cpa_manage),
    ("5", "管理线上 Sub2API 账号", _menu_sub2api_manage),
    ("6", "说明", _menu_help),
    ("0", "退出", lambda: 0),
]


def run_interactive_menu() -> int:
    last = 0
    while True:
        _home_screen()
        try:
            choice = _ask("编号", "1")
        except (EOFError, KeyboardInterrupt):
            _p()
            _p("  再见")
            return last

        action = None
        for key, _name, fn in _ITEMS:
            if choice == key:
                action = fn
                break

        if action is None:
            _p("  → 无效编号")
            _pause()
            continue
        if choice == "0":
            _p()
            _p("  再见")
            return last

        try:
            result = action()
        except (EOFError, KeyboardInterrupt):
            _p()
            _p("  → 已中断")
            last = 130
            _pause()
            continue
        except Exception as exc:
            _p()
            _p(f"  → 异常: {exc}")
            last = 1
            _pause()
            continue

        # Nested menu "返回上级"：直接回主菜单，不再「按回车继续」
        if result is MENU_BACK:
            continue

        try:
            last = int(result or 0)
        except (TypeError, ValueError):
            last = 0
        _pause()
