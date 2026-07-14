"""Interactive menu for grok-register.

Entry:
  python register_cli.py
  python register_cli.py --menu
"""

from __future__ import annotations

import os
import sys
from typing import Callable

MENU_VERSION = "1.3.1"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _rule(char: str = "─", width: int = 64) -> None:
    _p(char * width)


def _title(text: str) -> None:
    _p()
    _rule()
    _p(f"  {text}")
    _rule()
    _p()


def _pause() -> None:
    try:
        input("\n  按回车返回… ")
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
    _p("  上传哪些文件？")
    _p()
    _p("    1   最新一批")
    _p("    2   exports 下全部批次")
    _p("    3   指定目录")
    _p("    4   指定文件")
    _p("    0   返回")
    _p()
    c = _ask("范围", "1")
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


def _menu_upload() -> int:
    _title("上传到线上")
    _p("    1   全部（CPA + Sub2API）")
    _p("    2   仅 CPA")
    _p("    3   仅 Sub2API")
    _p("    0   返回")
    _p()
    target = _ask("目标", "1")
    if target == "0" or target not in ("1", "2", "3"):
        if target not in ("0", "1", "2", "3"):
            _p("  → 无效选项")
        return 0

    _p()
    scope = _scope_menu()
    if not scope:
        return 0

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
    bad = [c for c in codes if c]
    return bad[0] if bad else 0


def _menu_cpa_manage() -> int:
    _title("管理线上 CPA 凭证")
    _p("    1   列出凭证")
    _p("    2   模糊删除（先预览）")
    _p("    3   全部删除（先预览）")
    _p("    0   返回")
    _p()
    c = _ask("操作", "1")
    if c == "0":
        return 0
    if c == "1":
        _p()
        return _run_cli(["--cpa-list"])
    if c == "2":
        _p()
        _p("  支持：子串、glob（如 xai-*）、re:正则；多个用空格表示或")
        patterns = _ask("匹配", "")
        if not patterns:
            return 0
        parts = patterns.split()
        _p()
        _p("  — 预览 —")
        code = _run_cli(["--cpa-delete", *parts])
        if not _ask_yes("确认删除", False):
            return code
        _p()
        return _run_cli(["--cpa-delete", *parts, "--yes"])
    if c == "3":
        _p()
        _p("  — 预览 —")
        code = _run_cli(["--cpa-delete-all"])
        if not _ask_yes("确认清空线上全部 CPA 凭证", False):
            return code
        if _ask("再输入 DELETE 确认", "") != "DELETE":
            _p("  → 已取消")
            return 0
        _p()
        return _run_cli(["--cpa-delete-all", "--yes"])
    _p("  → 无效选项")
    return 0


def _menu_sub2api_manage() -> int:
    _title("管理线上 Sub2API 账号")
    _p("    1   列出账号（platform=grok）")
    _p("    2   模糊删除（先预览）")
    _p("    3   删除最新批（先预览）")
    _p("    4   删除全部 grok（先预览）")
    _p("    5   替换最新批（先删匹配再上传）")
    _p("    0   返回")
    _p()
    c = _ask("操作", "1")
    if c == "0":
        return 0
    if c == "1":
        _p()
        return _run_cli(["--sub2api-list"])
    if c == "2":
        _p()
        _p("  支持：子串、glob、re:正则；匹配 name / email / id")
        patterns = _ask("匹配", "")
        if not patterns:
            return 0
        parts = patterns.split()
        _p()
        _p("  — 预览 —")
        code = _run_cli(["--sub2api-delete", *parts])
        if not _ask_yes("确认删除", False):
            return code
        _p()
        return _run_cli(["--sub2api-delete", *parts, "--yes"])
    if c == "3":
        _p()
        _p("  按本地最新批 exports/<批次>/sub2api 的 email/name")
        _p("  删除线上匹配账号（只删不传）")
        _p()
        _p("  — 预览 —")
        code = _run_cli(["--sub2api-delete-latest"])
        if not _ask_yes("确认删除线上匹配的最新批账号", False):
            return code
        _p()
        return _run_cli(["--sub2api-delete-latest", "--yes"])
    if c == "4":
        _p()
        _p("  — 预览 —")
        code = _run_cli(["--sub2api-delete-all"])
        if not _ask_yes("确认删除线上全部 platform=grok 账号", False):
            return code
        if _ask("再输入 DELETE 确认", "") != "DELETE":
            _p("  → 已取消")
            return 0
        _p()
        return _run_cli(["--sub2api-delete-all", "--yes"])
    if c == "5":
        _p()
        _p("  按本地最新批 email/name 删除线上匹配账号，再上传最新批")
        _p("  （Sub2API 导入只能新建，不能覆盖 credentials）")
        _p()
        _p("  — 预览 —")
        code = _run_cli(["--sub2api-replace-latest"])
        if not _ask_yes("确认删除匹配并重新上传", False):
            return code
        _p()
        return _run_cli(["--sub2api-replace-latest", "--yes"])
    _p("  → 无效选项")
    return 0


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

  文档
    docs/export-cpa-and-sub2api.md
    docs/grok-403-investigation.md   # 对话 403 排查记录
"""
    )
    return 0


# Main menu: fewer top-level items, nested where needed
_ITEMS: list[tuple[str, str, Callable[[], int]]] = [
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
            last = int(action() or 0)
        except (EOFError, KeyboardInterrupt):
            _p()
            _p("  → 已中断")
            last = 130
        except Exception as exc:
            _p()
            _p(f"  → 异常: {exc}")
            last = 1
        _pause()
