"""CLI batch runner for Grok registration — multi-thread register + async CPA mint pipeline.

Architecture:
  Register workers (R)  →  batch accounts.txt (+ optional global mirror) + mint_queue
  Mint workers (M)      →  exports/<YYYYMMDD_HHMMSS>/cpa + sub2api + optional online CPA upload

Browser lifecycle:
  - One Chromium per register worker, reused via TabPool.clear_session
  - Full recycle every N accounts or on error
  - CPA mint prefers the warm register browser (before recycle) to avoid CF hard-block
  - Standalone mint workers only when warm path disabled / no page
  - Each CLI run creates exports/YYYYMMDD_HHMMSS/ (unless --no-batch)
"""
from __future__ import annotations

import argparse
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from grok_register.paths import PACKAGE_DIR, PROJECT_ROOT

from grok_register import core as reg  # noqa: E402


# Browser options live in grok_register.core.create_browser_options (proxy/headless/
# turnstilePatch/Linux flags). Only patch when that factory fails hard.
_orig_create_browser_options = reg.create_browser_options


def _patched_create_browser_options():
    try:
        return _orig_create_browser_options()
    except Exception:
        from DrissionPage import ChromiumOptions

        from grok_register.proxy.pool import find_browser_path, linux_server_chromium_flags, resolve_headless

        opts = ChromiumOptions()
        opts.auto_port()
        opts.set_timeouts(base=1)
        for flag in getattr(reg, "CHROMIUM_SLIM_FLAGS", ()) or ():
            try:
                opts.set_argument(flag)
            except Exception:
                pass
        for flag in linux_server_chromium_flags():
            try:
                opts.set_argument(flag)
            except Exception:
                pass
        if resolve_headless(getattr(reg, "config", {}) or {}):
            try:
                opts.headless(True)
            except Exception:
                opts.set_argument("--headless=new")
        bp = find_browser_path()
        if bp:
            try:
                opts.set_browser_path(bp)
            except Exception:
                pass
        ext_path = str(PACKAGE_DIR / "browser" / "extensions" / "turnstilePatch")
        if os.path.isdir(ext_path):
            try:
                opts.add_extension(ext_path)
            except Exception:
                pass
        return opts


reg.create_browser_options = _patched_create_browser_options


# ── 线程安全日志 ──

_log_queue: queue.Queue = queue.Queue()


def _log_writer():
    while True:
        msg = _log_queue.get()
        if msg is None:
            break
        print(msg, flush=True)


def log(worker_id: int | str, msg: str) -> None:
    _log_queue.put(f"[{time.strftime('%H:%M:%S')}] [W{worker_id}] {msg}")


# ── 统计 ──

_stats_lock = threading.Lock()
_STATS_ZERO = {
    "reg_success": 0,
    "reg_fail": 0,
    "mint_success": 0,
    "mint_fail": 0,
    "mint_skip": 0,
    "sub2api_success": 0,
    "sub2api_fail": 0,
    "sub2api_skip": 0,
    "sso_build_ok": 0,
    "browser_device_ok": 0,
}
_stats = dict(_STATS_ZERO)


def _inc(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


def _reset_stats() -> None:
    with _stats_lock:
        _stats.clear()
        _stats.update(_STATS_ZERO)


def _print_run_summary(cfg: dict | None = None) -> dict:
    """Print a clean multi-line end-of-run summary; return a stats snapshot."""
    with _stats_lock:
        s = dict(_stats)
    cfg = cfg or {}
    batch = str(cfg.get("export_batch_dir") or "").strip()
    cpa_on = bool(cfg.get("cpa_export_enabled", True))
    s2_on = bool(cfg.get("sub2api_export_enabled", True))

    def _disp_w(text: str) -> int:
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)

    def _pad(text: str, width: int) -> str:
        return text + " " * max(0, width - _disp_w(text))

    def _row(label: str, ok: int, fail: int, skip: int | None = None) -> str:
        parts = [
            f"  {_pad(label, 8)}",
            f"成功 {ok}",
            f"失败 {fail}",
        ]
        if skip is not None:
            parts.append(f"跳过 {skip}")
        # column gaps
        return f"{parts[0]}  {parts[1]:<10}  {parts[2]:<10}" + (
            f"  {parts[3]}" if skip is not None else ""
        )

    lines = [
        "",
        "─" * 48,
        "  本批完成",
        "─" * 48,
        _row("注册", s.get("reg_success", 0), s.get("reg_fail", 0)),
    ]
    if cpa_on:
        lines.append(
            _row(
                "CPA",
                s.get("mint_success", 0),
                s.get("mint_fail", 0),
                s.get("mint_skip", 0),
            )
        )
    else:
        lines.append(f"  {_pad('CPA', 8)}  （未启用）")
    if s2_on:
        lines.append(
            _row(
                "Sub2API",
                s.get("sub2api_success", 0),
                s.get("sub2api_fail", 0),
                s.get("sub2api_skip", 0),
            )
        )
    else:
        lines.append(f"  {_pad('Sub2API', 8)}  （未启用）")

    # Mint path breakdown when any CPA success used a known path
    sso_n = int(s.get("sso_build_ok", 0) or 0)
    br_n = int(s.get("browser_device_ok", 0) or 0)
    if sso_n or br_n:
        lines.append("─" * 48)
        lines.append(
            f"  {_pad('mint路径', 8)}  SSO→Build {sso_n}    浏览器设备码 {br_n}"
        )

    if batch:
        lines.append("─" * 48)
        lines.append(f"  {_pad('批次目录', 8)}  {batch}")
    lines.append("─" * 48)
    lines.append("")
    print("\n".join(lines), flush=True)
    return s


# forever 任务索引
_next_idx_lock = threading.Lock()
_next_idx = [1]

# mint 队列结束哨兵
_MINT_STOP = object()


def resolve_mint_workers(
    *,
    cli_value: int,
    threads: int,
    config: dict,
    inline_mint: bool,
) -> int:
    """Resolve mint worker count.

    Priority: --inline-mint > CLI --mint-workers (>=0) > config cpa_mint_workers > auto.
    auto (-1): 0 when warm-browser mint preferred; else min(threads, 4).
    0: mint on register threads (no dedicated mint pool).
    """
    if inline_mint:
        return 0
    if cli_value >= 0:
        return max(0, min(int(cli_value), 10))
    cfg_v = config.get("cpa_mint_workers", -1)
    try:
        cfg_v = int(cfg_v)
    except Exception:
        cfg_v = -1
    if cfg_v >= 0:
        return max(0, min(cfg_v, 10))
    if not config.get("cpa_export_enabled", True):
        return 0
    # Warm mint runs on the register thread; spare mint workers only add noise.
    if bool(config.get("cpa_mint_prefer_warm_browser", True)):
        return 0
    return max(1, min(int(threads), 4))


def resolve_mint_queue_max(config: dict, mint_workers: int, cli_value: int | None = None) -> int:
    if cli_value is not None and cli_value >= 0:
        return int(cli_value)
    try:
        v = int(config.get("cpa_mint_queue_max", 0) or 0)
    except Exception:
        v = 0
    if v > 0:
        return v
    # default backpressure: 2 × mint workers (0 if no mint pool)
    return max(0, mint_workers * 2) if mint_workers > 0 else 0


class DummyStop:
    def __call__(self) -> bool:
        return False


def _ensure_browser(worker_id: int, force_recycle: bool = False):
    """Start browser if missing; optional full recycle."""
    if force_recycle:
        try:
            reg.stop_browser()
        except Exception:
            pass
    if reg.TabPool.get_browser() is None:
        reg.start_browser(log_callback=lambda m: log(worker_id, m))


def format_progress_label(
    idx: int,
    *,
    done_at_start: int = 0,
    batch_total: int | None = None,
    target_total: int | None = None,
) -> str:
    """Human-friendly progress for --extra / --count runs.

    --extra 10 with 3 already done used to show「第 13/13」, which looked like
    only one account. Prefer「本批第 10/10（全局 13，启动时已有 3）」.
    """
    if batch_total and batch_total > 0:
        n = max(1, idx - int(done_at_start or 0))
        n = min(n, int(batch_total))
        return (
            f"本批第 {n}/{int(batch_total)} 个"
            f"（全局序号 {idx}"
            + (f"，启动时已有 {done_at_start}" if done_at_start else "")
            + (f"，目标总量 {target_total}" if target_total else "")
            + "）"
        )
    if target_total and target_total > 0:
        return f"第 {idx}/{int(target_total)} 个账号"
    return f"第 {idx} 个账号"


def register_one(
    worker_id: int,
    idx: int,
    total: int,
    accounts_file: str,
    *,
    do_mint_inline: bool = False,
    mint_queue: queue.Queue | None = None,
    done_at_start: int = 0,
    batch_total: int | None = None,
    count_fail: bool = True,
) -> dict | None:
    """Run one browser registration via RegistrationEngine. Enqueue CPA mint.

    Returns success dict with ok=True, or failure dict with ok=False.

    Side effects after success (register thread):
      - write accounts line
      - grok2api local/remote pool
      - NSFW when ``config.enable_nsfw`` is true (uses warm browser if available)
      - CPA mint (warm browser preferred, else queue)
    """
    from grok_register import RegistrationEngine

    email = ""
    cancel = DummyStop()
    cfg = getattr(reg, "config", {}) or {}
    try:
        max_mail_retry = int(reg.get_max_mail_retry())
    except Exception:
        max_mail_retry = 3

    try:
        _ensure_browser(worker_id, force_recycle=False)
    except Exception as exc:
        log(worker_id, f"! 浏览器启动失败: {exc}")
        if count_fail:
            _inc("reg_fail")
        return {
            "ok": False,
            "error": str(exc),
            "failure_reason": "browser_start",
            "counted_fail": count_fail,
        }

    prog = format_progress_label(
        idx,
        done_at_start=done_at_start,
        batch_total=batch_total,
        target_total=total if total and total > 0 else None,
    )
    log(worker_id, f"--- {prog} | browser registration ---")

    engine = RegistrationEngine(config=cfg, reg_module=reg)
    try:
        result = engine.register_one(
            log_callback=lambda m: log(worker_id, m),
            cancel_callback=cancel,
            side_effect_profile="cli_pipeline",
            accounts_file=None,  # write accounts below (same path as before)
            run_side_effects=False,
            max_mail_retry=max_mail_retry,
        )
    except Exception as exc:
        log(worker_id, f"! 注册失败: {exc}")
        reg.mark_error(email or "", reason=str(exc)[:120])
        traceback.print_exc()
        if count_fail:
            _inc("reg_fail")
        try:
            reg.restart_browser(log_callback=lambda m: log(worker_id, m))
        except Exception:
            pass
        return {
            "ok": False,
            "error": str(exc),
            "failure_reason": "exception",
            "counted_fail": count_fail,
        }

    if not result.ok or not str(result.sso or "").strip():
        log(
            worker_id,
            f"! 注册失败: {result.failure_reason} {result.error} "
            f"(transport={result.transport_used})",
        )
        reg.mark_error(result.email or "", reason=(result.error or "")[:120])
        if count_fail:
            _inc("reg_fail")
        try:
            reg.restart_browser(log_callback=lambda m: log(worker_id, m))
        except Exception:
            pass
        fr = getattr(result.failure_reason, "value", None) or str(result.failure_reason or "")
        return {
            "ok": False,
            "error": str(result.error or ""),
            "failure_reason": fr,
            "counted_fail": count_fail,
        }

    email = result.email
    password = result.password or ""
    sso = result.sso
    profile = dict(result.profile or {})
    cookies = list(result.cookies or [])

    try:
        line = f"{email}----{password}----{sso}\n"
        Path(accounts_file).parent.mkdir(parents=True, exist_ok=True)
        with open(accounts_file, "a", encoding="utf-8") as f:
            f.write(line)
        # Optional mirror into global accounts_cli when using per-batch export dirs
        cfg_now = getattr(reg, "config", {}) or {}
        if cfg_now.get("export_batch_also_global_accounts", False):
            global_acc = (
                cfg_now.get("export_batch_global_accounts_file")
                or str(PROJECT_ROOT / "accounts" / "accounts_cli.txt")
            )
            try:
                gpath = Path(os.path.expanduser(str(global_acc)))
                if not gpath.is_absolute():
                    gpath = (PROJECT_ROOT / gpath).resolve()
                if str(gpath.resolve()) != str(Path(accounts_file).resolve()):
                    gpath.parent.mkdir(parents=True, exist_ok=True)
                    with open(gpath, "a", encoding="utf-8") as gf:
                        gf.write(line)
            except Exception as mirror_exc:
                log(worker_id, f"[!] global accounts mirror failed: {mirror_exc}")
        timing = ""
        if getattr(result, "stage_summary", None):
            timing = f" | {result.stage_summary}"
        if getattr(result, "duration_ms", 0):
            timing += f" total={result.duration_ms}ms"
        log(worker_id, f"+ 注册成功: {email} (via browser){timing}")
        reg.mark_used(email, password)

        if not cookies:
            page = reg._get_page()
            try:
                import grok_register.export.cpa_export as _cpa_exp

                cookies = _cpa_exp.export_cookies_from_page(page) if page is not None else []
            except Exception:
                cookies = []
        if cookies:
            log(worker_id, f"[*] cookie {len(cookies)} 条供 mint 注入")

        page = reg._get_page()
        if page and reg.PERF_FLAGS.get("cookie_snapshot", True):
            try:
                reg.save_cookies_snapshot(page, "success", email)
            except Exception:
                pass
        try:
            reg.add_token_to_grok2api_pools(
                sso, email=email, log_callback=lambda m: log(worker_id, m)
            )
        except Exception as exc:
            log(worker_id, f"[Debug] grok2api: {exc}")

        # Honor config.enable_nsfw (previously skipped by cli_pipeline / run_side_effects=False).
        # Run before CPA mint while the register browser is still warm when possible.
        if cfg.get("enable_nsfw", True):
            try:
                page_for_nsfw = reg._get_page()
                nsfw_ok, nsfw_msg = reg.enable_nsfw_for_token(
                    sso,
                    page=page_for_nsfw,
                    log_callback=lambda m: log(worker_id, m),
                )
                if nsfw_ok:
                    log(worker_id, f"[+] NSFW 开启成功: {nsfw_msg}")
                else:
                    log(
                        worker_id,
                        f"[!] NSFW 未开启（账号已保存，可稍后手动开）: {nsfw_msg}",
                    )
            except Exception as exc:
                log(worker_id, f"[!] NSFW 异常（账号已保存）: {exc}")

        job = {
            "email": email,
            "password": password,
            "sso": sso,
            "profile": profile,
            "idx": idx,
            "cookies": cookies,
            "transport_used": result.transport_used or "browser",
        }

        # CPA mint BEFORE recycling register browser so we can reuse warm CF/session.
        # Prefer warm-browser mint on this thread when page is available.
        prefer_warm = bool(cfg.get("cpa_mint_prefer_warm_browser", True))
        mint_now = False
        if cfg.get("cpa_export_enabled", True):
            if prefer_warm and page is not None:
                mint_now = True
            elif do_mint_inline:
                mint_now = True

        if mint_now:
            log(worker_id, "[cpa] mint on register thread (warm browser preferred)")
            _run_mint_job(
                f"R{worker_id}",
                job,
                cfg,
                page=page if prefer_warm else None,
            )
        elif mint_queue is not None and cfg.get("cpa_export_enabled", True):
            qmax = int(getattr(mint_queue, "_reg_qmax", 0) or 0)
            while qmax > 0 and mint_queue.qsize() >= qmax:
                log(worker_id, f"[cpa] mint 队列背压 qsize={mint_queue.qsize()}≥{qmax}，等待...")
                time.sleep(1.0)
            mint_queue.put(job)
            log(
                worker_id,
                f"[cpa] enqueued mint for {email} (queue≈{mint_queue.qsize()}; "
                "standalone browser — more CF risk than warm mint)",
            )
        elif cfg.get("cpa_export_enabled", True):
            log(worker_id, "[cpa] mint skipped (no queue / page / inline)")

        # Release / recycle register browser AFTER mint
        try:
            reg.prepare_browser_for_next_account(log_callback=lambda m: log(worker_id, m))
        except Exception:
            try:
                reg.stop_browser()
            except Exception:
                pass

        _inc("reg_success")
        job["ok"] = True
        return job
    except Exception as exc:
        log(worker_id, f"! 注册后处理失败: {exc}")
        reg.mark_error(email or "", reason=str(exc)[:120])
        traceback.print_exc()
        if count_fail:
            _inc("reg_fail")
        try:
            reg.restart_browser(log_callback=lambda m: log(worker_id, m))
        except Exception:
            pass
        return {
            "ok": False,
            "error": str(exc),
            "failure_reason": "post_process",
            "counted_fail": count_fail,
        }


def _run_mint_job(
    worker_id: int | str,
    job: dict[str, Any],
    config: dict,
    *,
    page: Any | None = None,
) -> dict:
    """CPA mint + Sub2API export. Prefer warm register page when provided."""
    email = job.get("email") or ""
    password = job.get("password") or ""
    if not email or not password:
        _inc("mint_fail")
        return {"ok": False, "error": "missing email/password", "email": email}
    if not config.get("cpa_export_enabled", True):
        _inc("mint_skip")
        _inc("sub2api_skip")
        log(worker_id, f"[cpa] export disabled, skip {email}")
        return {"ok": False, "skipped": True, "email": email}
    try:
        import grok_register.export.cpa_export as cpa_export

        result = cpa_export.export_cpa_xai_for_account(
            email,
            password,
            page=page,
            cookies=job.get("cookies"),
            sso=job.get("sso") or "",
            config=config,
            log_callback=lambda m: log(worker_id, m),
        )
        if result.get("ok"):
            log(worker_id, f"+ CPA auth: {result.get('path')}")
            sub = result.get("sub2api") or {}
            if sub.get("ok"):
                log(
                    worker_id,
                    f"+ Sub2API: {sub.get('path')} (combined={sub.get('combined_path')})",
                )
                _inc("sub2api_success")
            elif not config.get("sub2api_export_enabled", True):
                _inc("sub2api_skip")
            elif result.get("sub2api_error") or (sub and not sub.get("ok") and not sub.get("skipped")):
                log(
                    worker_id,
                    f"! Sub2API 导出失败: {result.get('sub2api_error') or sub.get('error') or sub}",
                )
                _inc("sub2api_fail")
            elif sub.get("skipped"):
                _inc("sub2api_skip")
                log(worker_id, f"[sub2api] skipped: {sub.get('reason') or 'disabled'}")
            else:
                # CPA ok but no sub2api payload (unexpected when export enabled)
                if config.get("sub2api_export_enabled", True):
                    log(worker_id, f"[sub2api] status={sub or 'missing'}")
                    _inc("sub2api_fail")
                else:
                    _inc("sub2api_skip")
            # mint path (SSO→Build vs browser device)
            src = str(result.get("mint_source") or "").strip().lower()
            if src.startswith("sso"):
                _inc("sso_build_ok")
            elif "browser" in src or src == "browser_device":
                _inc("browser_device_ok")
            cloud = result.get("cloud_cpa_upload") or {}
            if cloud.get("ok"):
                log(worker_id, f"+ 线上 CPA: {cloud.get('name')}")
            elif cloud and not cloud.get("skipped") and config.get("cpa_cloud_upload_enabled"):
                log(worker_id, f"! 线上 CPA 导入失败: {cloud.get('error') or cloud}")
            cloud_s2 = result.get("cloud_sub2api_upload") or {}
            if cloud_s2.get("ok"):
                log(
                    worker_id,
                    f"+ 线上 Sub2API: created={cloud_s2.get('account_created')} "
                    f"failed={cloud_s2.get('account_failed')}",
                )
            elif (
                cloud_s2
                and not cloud_s2.get("skipped")
                and config.get("sub2api_cloud_upload_enabled")
            ):
                log(
                    worker_id,
                    f"! 线上 Sub2API 导入失败: {cloud_s2.get('error') or cloud_s2}",
                )
            _inc("mint_success")
        elif result.get("skipped"):
            _inc("mint_skip")
            _inc("sub2api_skip")
            log(worker_id, f"[cpa] skipped: {result.get('reason')}")
        else:
            _inc("mint_fail")
            _inc("sub2api_skip")
            log(worker_id, f"! CPA auth 未成功: {result.get('error') or result}")
        return result
    except Exception as exc:
        _inc("mint_fail")
        _inc("sub2api_skip")
        log(worker_id, f"! CPA export 异常: {exc}")
        traceback.print_exc()
        return {"ok": False, "error": str(exc), "email": email}


def _register_worker(
    worker_id: int,
    task_queue: queue.Queue,
    total: int,
    accounts_file: str,
    mint_queue: queue.Queue | None,
    forever: bool,
    do_mint_inline: bool,
    start_delay: float = 0.0,
    done_at_start: int = 0,
    batch_total: int | None = None,
):
    # Stagger worker start so multi-thread CF burst is milder
    if start_delay > 0:
        time.sleep(start_delay)
        log(worker_id, f"[*] worker start delay {start_delay:.1f}s")

    cf_streak = 0
    while True:
        try:
            idx = task_queue.get_nowait()
        except queue.Empty:
            if not forever:
                break
            with _next_idx_lock:
                nxt = _next_idx[0]
                _next_idx[0] = nxt + 5
            for i in range(nxt, nxt + 5):
                task_queue.put(i)
            continue

        retry = 0
        last_err = ""
        last_fr = ""
        accounted_fail = False  # count each account slot once in summary
        while retry < 2:
            try:
                result = register_one(
                    worker_id,
                    idx,
                    total,
                    accounts_file,
                    do_mint_inline=do_mint_inline,
                    mint_queue=mint_queue,
                    done_at_start=done_at_start,
                    batch_total=batch_total,
                    count_fail=retry == 0,  # only first attempt counts as fail
                )
                if result and result.get("ok"):
                    cf_streak = 0
                    try:
                        from grok_register.proxy.pool import get_thread_proxy, report_proxy_success

                        report_proxy_success(get_thread_proxy(config=getattr(reg, "config", {}) or {}))
                    except Exception:
                        pass
                    break
                last_err = str((result or {}).get("error") or "")
                last_fr = str((result or {}).get("failure_reason") or "")
                if result and result.get("counted_fail"):
                    accounted_fail = True
                # Mark current proxy bad on CF / tunnel failures so pool can cool it down
                blob_fail = f"{last_fr} {last_err}".lower()
                if any(
                    m in blob_fail
                    for m in (
                        "cloudflare",
                        "cf_challenge",
                        "proxy",
                        "tunnel",
                        "timed out",
                        "timeout",
                        "connection",
                        "blocked",
                        "network",
                        "错误页",
                    )
                ):
                    try:
                        from grok_register.proxy.pool import (
                            get_thread_proxy,
                            report_proxy_failure,
                            rotate_thread_proxy,
                        )

                        cur = get_thread_proxy(config=getattr(reg, "config", {}) or {})
                        report_proxy_failure(
                            cur,
                            last_err or last_fr,
                            config=getattr(reg, "config", {}) or {},
                        )
                        rotate_thread_proxy(getattr(reg, "config", {}) or {})
                    except Exception:
                        pass
                retry += 1
                if retry < 2:
                    log(worker_id, f"[retry] 账号 {idx} 失败，重试 {retry}/1")
                    try:
                        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                    except Exception:
                        pass
                    time.sleep(3.0 + worker_id)
            except Exception as exc:
                last_err = str(exc)
                last_fr = "exception"
                if not accounted_fail:
                    _inc("reg_fail")
                    accounted_fail = True
                retry += 1
                if retry < 2:
                    log(worker_id, f"[retry] 账号 {idx} 异常，重试 {retry}/1")
                    traceback.print_exc()
                    try:
                        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                    except Exception:
                        pass
                    time.sleep(3.0 + worker_id)

        # Track Cloudflare streaks — pause batch instead of burning all slots
        blob = f"{last_fr} {last_err}".lower()
        if any(
            m in blob
            for m in (
                "cloudflare",
                "attention required",
                "cf_challenge",
                "blocked",
                "unable to access",
            )
        ):
            cf_streak += 1
        elif retry >= 2:
            cf_streak = 0
        if cf_streak >= 3:
            log(
                worker_id,
                f"[!] 连续 {cf_streak} 次 Cloudflare 拦截，停止本线程剩余任务以免空烧。"
                " 建议: --threads 1 --headed（直连），或可用的 PROXY_POOL",
            )
            # Drain remaining tasks for this worker only by exiting loop
            # (leave tasks for other workers if any)
            try:
                while True:
                    task_queue.get_nowait()
            except queue.Empty:
                pass
            break

    # worker exit: free browser
    try:
        reg.stop_browser()
    except Exception:
        pass
    log(worker_id, "register worker exit")


def _mint_worker(worker_id: str, mint_queue: queue.Queue, config: dict):
    while True:
        job = mint_queue.get()
        try:
            if job is _MINT_STOP:
                break
            if not isinstance(job, dict):
                continue
            _run_mint_job(worker_id, job, config)
        finally:
            mint_queue.task_done()
    try:
        from grok_register.export.cpa_xai.browser_confirm import shutdown_mint_browsers

        shutdown_mint_browsers()
    except Exception:
        pass
    log(worker_id, "mint worker exit")


def parse_account_line(line: str) -> tuple[str, str, str] | None:
    """Parse ``email----password----sso`` lines robustly.

    Passwords from ``token_urlsafe`` may end with ``-``, so naive
    ``line.split('----')`` corrupts the JWT into ``-eyJ...`` and breaks SSO cookie
    inject (login page loop on remint).
    """
    line = (line or "").strip()
    if not line or line.startswith("#") or "----" not in line:
        return None
    # Anchor on JWT (always starts with eyJ)
    m = re.search(
        r"----(eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*)\s*$",
        line,
    )
    if m:
        sso = m.group(1)
        left = line[: m.start()]
        if "----" not in left:
            return None
        email, password = left.split("----", 1)
        email, password = email.strip(), password.strip()
        if email and password and sso:
            return email, password, sso
    parts = [p for p in line.split("----") if p != ""]
    if len(parts) < 3:
        return None
    email = parts[0].strip()
    sso = parts[-1].strip().lstrip("-")
    password = "----".join(parts[1:-1]).strip()
    if sso.startswith("eyJ") is False and parts[-1].strip().startswith("-eyJ"):
        sso = parts[-1].strip().lstrip("-")
    if email and password and sso:
        return email, password, sso
    return None


def remint_missing_from_accounts(accounts_file: str, config: dict) -> int:
    """Backfill CPA and/or Sub2API for accounts that already have SSO.

    Two tiers (no browser for Sub2API-only gaps):

    1. **Missing CPA** (``xai-<email>.json``): full OIDC mint (headed standalone).
       On success, Sub2API is also written if ``sub2api_export_enabled``.
    2. **Has CPA but missing Sub2API** (``sub2api-xai-<email>.json``): pure file
       conversion from CPA — no browser, no re-mint.

    Always uses a **headed standalone** browser for CPA remint: cold headless is
    almost always Cloudflare-blocked on accounts.x.ai (no warm register tab).
    """
    try:
        from grok_register.export.cpa_export import resolve_export_dirs

        _root, auth_dir, sub_dir = resolve_export_dirs(config)
    except Exception:
        auth_dir = Path(config.get("cpa_auth_dir") or "./exports/cpa")
        if not auth_dir.is_absolute():
            auth_dir = Path(os.path.dirname(os.path.abspath(__file__))) / auth_dir
        auth_dir = auth_dir.resolve()
        sub_dir = Path(config.get("sub2api_export_dir") or "./exports/sub2api")
        if not sub_dir.is_absolute():
            sub_dir = Path(os.path.dirname(os.path.abspath(__file__))) / sub_dir
        sub_dir = sub_dir.resolve()
    auth_dir.mkdir(parents=True, exist_ok=True)
    sub_dir.mkdir(parents=True, exist_ok=True)
    if not os.path.isfile(accounts_file):
        print(f"[!] accounts file missing: {accounts_file}", flush=True)
        return 1

    want_sub2api = bool(config.get("sub2api_export_enabled", True))
    # Need CPA mint (browser)
    missing_cpa: list[tuple[str, str, str]] = []
    # Have CPA, only need Sub2API convert
    missing_sub2api_only: list[str] = []

    with open(accounts_file, encoding="utf-8") as f:
        for line in f:
            parsed = parse_account_line(line)
            if not parsed:
                continue
            email, password, sso = parsed
            cpa_path = auth_dir / f"xai-{email}.json"
            sub_path = sub_dir / f"sub2api-xai-{email}.json"
            if not cpa_path.is_file():
                missing_cpa.append((email, password, sso))
                continue
            if want_sub2api and not sub_path.is_file():
                missing_sub2api_only.append(email)

    # --- Phase A: Sub2API-only backfill from existing CPA (no browser) ---
    sub_ok = 0
    sub_fail = 0
    if missing_sub2api_only:
        print(
            f"[*] 补 Sub2API {len(missing_sub2api_only)} 个（已有 CPA、缺 sub2api-xai-*.json）\n"
            f"    CPA 目录: {auth_dir}\n"
            f"    Sub2API:  {sub_dir}\n"
            f"    方式=本地转换（不打开浏览器、不重新 mint）",
            flush=True,
        )
        try:
            import grok_register.export.cpa_to_sub2api as cpa_to_sub2api
        except Exception as exc:
            print(f"[!] 无法导入 cpa_to_sub2api: {exc}", flush=True)
            sub_fail = len(missing_sub2api_only)
        else:
            for email in missing_sub2api_only:
                cpa_path = auth_dir / f"xai-{email}.json"
                try:
                    out_path, _doc = cpa_to_sub2api.convert_cpa_file(
                        cpa_path, out_dir=sub_dir
                    )
                    print(f"  + Sub2API {email} → {out_path.name}", flush=True)
                    sub_ok += 1
                except Exception as exc:
                    print(f"  ! Sub2API 转换失败 {email}: {exc}", flush=True)
                    sub_fail += 1
            # rebuild combined package
            try:
                combined_raw = str(config.get("sub2api_combined_file") or "").strip()
                if combined_raw:
                    combined = Path(combined_raw).expanduser()
                    if not combined.is_absolute():
                        combined = (PROJECT_ROOT / combined).resolve()
                else:
                    combined = sub_dir / "sub2api-accounts.json"
                cpa_to_sub2api.rebuild_combined(auth_dir, combined)
                print(f"  + combined combined → {combined}", flush=True)
            except Exception as exc:
                print(f"  ! rebuild combined failed: {exc}", flush=True)

    if not missing_cpa:
        if missing_sub2api_only:
            print(
                f"=== 补导出完成: Sub2API 成功 {sub_ok}, 失败 {sub_fail}; "
                f"CPA 均已存在，无需浏览器 remint ===",
                flush=True,
            )
            return 0 if sub_fail == 0 else (2 if sub_ok > 0 else 1)
        print(
            f"[*] 无需补做：accounts 中账号均已有 CPA"
            + (f" 与 Sub2API" if want_sub2api else "")
            + f"\n    CPA={auth_dir}"
            + (f"\n    Sub2API={sub_dir}" if want_sub2api else ""),
            flush=True,
        )
        return 0

    # --- Phase B: full CPA remint (browser) for accounts without CPA ---
    # Force headed remint regardless of config.cpa_headless (default true).
    cfg = dict(config or {})
    cfg["cpa_headless"] = False
    cfg["headless"] = False
    cfg["cpa_force_standalone"] = True
    # Fresh browser per account: avoid previous SSO residual + clearer CF state.
    cfg["cpa_mint_browser_reuse"] = False
    cfg["cpa_mint_browser_recycle_every"] = 1
    cfg["cpa_mint_cookie_inject"] = True
    os.environ["HEADLESS"] = "0"
    try:
        import grok_register.core as _reg

        if getattr(_reg, "config", None) is not None:
            _reg.config["headless"] = False
            _reg.config["cpa_headless"] = False
    except Exception:
        pass

    missing = missing_cpa
    print(
        f"[*] 补 mint CPA {len(missing)} 个账号（无 xai-*.json）→ {auth_dir}\n"
        f"    成功后会按配置写 Sub2API（sub2api_export_enabled="
        f"{cfg.get('sub2api_export_enabled', True)}）\n"
        f"    模式=headed standalone（强制有界面；config.cpa_headless 已忽略）\n"
        f"    间隔≈4s；失败账号会再试一轮",
        flush=True,
    )
    log_thread = threading.Thread(target=_log_writer, daemon=True)
    log_thread.start()
    ok = 0
    fail = 0
    failed_jobs: list[tuple[str, str, str]] = []

    def _job(email: str, password: str, sso: str, idx: int) -> dict:
        return {
            "email": email,
            "password": password,
            "sso": sso,
            "cookies": [
                {
                    "name": n,
                    "value": sso,
                    "domain": d,
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
                for n in ("sso", "sso-rw")
                for d in (
                    ".x.ai",
                    "accounts.x.ai",
                    ".accounts.x.ai",
                    "auth.x.ai",
                    ".auth.x.ai",
                    "grok.com",
                    ".grok.com",
                )
            ],
            "idx": idx,
        }

    try:
        for i, (email, password, sso) in enumerate(missing, 1):
            log("RM", f"--- remint {i}/{len(missing)}: {email} ---")
            result = _run_mint_job("RM", _job(email, password, sso, i), cfg, page=None)
            if result.get("ok"):
                ok += 1
            else:
                fail += 1
                failed_jobs.append((email, password, sso))
            # OAuth device code rate-limits if we hammer; space remints out
            time.sleep(4.0)

        # Second pass for transient SSL / rate_limit / login-fill failures
        if failed_jobs:
            log("RM", f"--- remint 重试 {len(failed_jobs)} 个失败账号 ---")
            time.sleep(6.0)
            still_fail: list[tuple[str, str, str]] = []
            for j, (email, password, sso) in enumerate(failed_jobs, 1):
                # Skip if a concurrent path already wrote the file
                if (auth_dir / f"xai-{email}.json").is_file():
                    log("RM", f"skip retry (already has json): {email}")
                    ok += 1
                    fail = max(0, fail - 1)
                    continue
                log("RM", f"--- remint retry {j}/{len(failed_jobs)}: {email} ---")
                result = _run_mint_job(
                    "RM", _job(email, password, sso, 1000 + j), cfg, page=None
                )
                if result.get("ok"):
                    ok += 1
                    fail = max(0, fail - 1)
                else:
                    still_fail.append((email, password, sso))
                time.sleep(5.0)
            failed_jobs = still_fail
    finally:
        try:
            from grok_register.export.cpa_xai.browser_confirm import shutdown_mint_browsers

            shutdown_mint_browsers()
        except Exception:
            pass
        _log_queue.put(None)
        log_thread.join(timeout=2)

    print(
        f"=== remint 完成: CPA mint 成功 {ok}, 失败 {fail}"
        + (
            f"; 另 Sub2API 回填 成功 {sub_ok}, 失败 {sub_fail}"
            if (sub_ok or sub_fail)
            else ""
        )
        + " ===",
        flush=True,
    )
    if failed_jobs:
        print(
            "[*] 仍失败: " + ", ".join(e for e, _, _ in failed_jobs),
            flush=True,
        )
    if fail == 0 and sub_fail == 0:
        return 0
    if ok > 0 or sub_ok > 0:
        return 2  # partial
    return 1


def main() -> int:
    # Interactive menu: bare command or --menu / -i / --interactive
    _menu_flags = {"--menu", "-i", "--interactive"}
    _argv_rest = sys.argv[1:]
    if not _argv_rest or (
        set(_argv_rest) <= _menu_flags
        or (_argv_rest[0] in _menu_flags and len(_argv_rest) == 1)
    ):
        from grok_register.menu import run_interactive_menu

        return run_interactive_menu()

    parser = argparse.ArgumentParser(description="CLI runner for grok_register.core (pipelined).")
    parser.add_argument(
        "--menu",
        "-i",
        "--interactive",
        action="store_true",
        help="打开交互式菜单（无参数时默认也进入菜单）",
    )
    parser.add_argument("--count", type=int, default=1, help="账号总数目标（0=不限；含已有）")
    parser.add_argument(
        "--extra",
        type=int,
        default=0,
        help="在已有 accounts 基础上再新注册 N 个",
    )
    parser.add_argument("--threads", type=int, default=1, help="注册并发线程数（1-10）")
    parser.add_argument(
        "--mint-workers",
        type=int,
        default=-1,
        help="CPA mint 并发：-1=用 config/auto；0=内联；1-10=固定。覆盖 config.cpa_mint_workers",
    )
    parser.add_argument(
        "--mint-queue-max",
        type=int,
        default=-1,
        help="mint 队列背压上限：-1=用 config/auto(2×workers)；0=不限制",
    )
    parser.add_argument(
        "--accounts-file",
        default=None,
        help="账号输出文件；默认 batch 模式写到本批目录 accounts.txt，否则 accounts/accounts_cli.txt",
    )
    parser.add_argument(
        "--batch-name",
        default="",
        help="本批导出子目录后缀名（默认 YYYYMMDD_HHMMSS[_name]）",
    )
    parser.add_argument(
        "--no-batch",
        action="store_true",
        help="关闭按批分子目录：继续写 exports/cpa 扁平结构（旧行为）",
    )
    parser.add_argument(
        "--batch-dir",
        default="",
        help="直接指定已有批次目录（续跑/remint 到同一批）；不新建",
    )
    parser.add_argument("--fast", action="store_true", default=True, help="快速模式（默认开）：压缩 sleep、关截图")
    parser.add_argument("--no-fast", action="store_true", help="关闭快速模式")
    parser.add_argument("--no-browser-reuse", action="store_true", help="每号强制 quit 浏览器")
    parser.add_argument("--browser-recycle-every", type=int, default=25, help="复用 N 次后完整回收")
    parser.add_argument("--cookie-snapshot", action="store_true", help="注册成功写 cookie 快照（默认关，fast）")
    parser.add_argument("--inline-mint", action="store_true", help="强制注册线程内联 mint（调试用）")
    parser.add_argument(
        "--headless",
        action="store_true",
        default=None,
        help="后台/无交互浏览器（默认模式见 --headless-mode）",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="有界面模式（关闭无头；覆盖默认 / HEADLESS）",
    )
    parser.add_argument(
        "--headless-mode",
        default="",
        choices=["", "auto", "offscreen", "pure"],
        help="后台实现: auto=桌面离屏真窗口(推荐过CF); offscreen=强制离屏; pure=--headless=new",
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="可选单代理；不填则直连。覆盖 config.proxy / 环境变量 PROXY",
    )
    parser.add_argument(
        "--proxy-pool",
        default="",
        help="可选代理池（逗号/分号分隔轮询）；不填则直连。覆盖 config.proxy_pool / PROXY_POOL",
    )
    parser.add_argument(
        "--force-multi",
        action="store_true",
        help="直连时仍允许多线程（默认直连强制 1 线程，降低 CF 连拦）",
    )
    parser.add_argument(
        "--remint-missing",
        action="store_true",
        help=(
            "不新注册：从 accounts 补缺。"
            "① 无 CPA → 浏览器 remint；"
            "② 有 CPA 无 Sub2API → 本地转换（不开浏览器）"
        ),
    )
    parser.add_argument(
        "--upload-cpa-cloud",
        action="store_true",
        help="将本地 CPA JSON 上传到线上 CLIProxyAPI（Management API auth-files）",
    )
    parser.add_argument(
        "--cpa-upload-dir",
        default="",
        help="上传扫描目录：某批 20260712_153045/ 或其 cpa/ 或 exports（配合 --cpa-upload-all）",
    )
    parser.add_argument(
        "--cpa-upload-files",
        nargs="*",
        default=None,
        help="只上传指定的 xai-*.json（可多个路径或 glob）",
    )
    parser.add_argument(
        "--cpa-upload-all",
        action="store_true",
        help="递归上传目录下全部 xai-*.json（默认扫 exports/ 下所有 batch）",
    )
    parser.add_argument(
        "--cpa-upload-latest",
        action="store_true",
        help="只上传最新一批注册目录（exports/YYYYMMDD_HHMMSS/cpa）；可单独使用，隐含 --upload-cpa-cloud",
    )
    parser.add_argument(
        "--cpa-upload-workers",
        type=int,
        default=0,
        help="批量上传 CPA 并行线程数（0=用 config cpa_cloud_upload_workers，默认 8）",
    )
    parser.add_argument(
        "--upload-sub2api-cloud",
        action="store_true",
        help="将本地 Sub2API JSON 上传到线上 Sub2API（POST /api/v1/admin/accounts/data）",
    )
    parser.add_argument(
        "--sub2api-upload-dir",
        default="",
        help="Sub2API 上传扫描目录：某批 时间戳/ 或其 sub2api/ 或 exports",
    )
    parser.add_argument(
        "--sub2api-upload-files",
        nargs="*",
        default=None,
        help="只上传指定的 sub2api-*.json / sub2api-accounts.json",
    )
    parser.add_argument(
        "--sub2api-upload-all",
        action="store_true",
        help="递归上传 exports 下全部 Sub2API 导出（优先各批 sub2api-accounts.json）",
    )
    parser.add_argument(
        "--sub2api-upload-latest",
        action="store_true",
        help="只上传最新一批的 Sub2API 导出；可单独使用，隐含 --upload-sub2api-cloud",
    )
    parser.add_argument(
        "--sub2api-list",
        action="store_true",
        help="列出线上 Sub2API 账号（Admin GET /accounts；默认 platform=grok）",
    )
    parser.add_argument(
        "--sub2api-delete",
        nargs="+",
        default=None,
        metavar="PATTERN",
        help="模糊删除线上 Sub2API 账号：子串 / glob / re:正则；匹配 name/email/id；先 dry-run，加 --yes 才删除",
    )
    parser.add_argument(
        "--sub2api-delete-all",
        action="store_true",
        help="删除线上全部匹配 platform 的账号（默认 grok；无服务端 bulk，逐条 DELETE /accounts/:id）；须 --yes",
    )
    parser.add_argument(
        "--sub2api-platform",
        default="grok",
        metavar="PLATFORM",
        help="Sub2API 列表/删除的 platform 过滤（默认 grok；* 表示全部）",
    )
    parser.add_argument(
        "--sub2api-delete-latest",
        action="store_true",
        help="按本地最新批 email/name 删除线上匹配账号（只删不传；须 --yes）",
    )
    parser.add_argument(
        "--sub2api-replace-latest",
        action="store_true",
        help="先按本地最新批 email/name 删除线上匹配账号，再上传最新批（导入仅 create；须 --yes）",
    )
    parser.add_argument(
        "--sub2api-replace-all-platform",
        action="store_true",
        help="与 --sub2api-replace-latest 联用：删除该 platform 下全部线上账号后再上传（危险）",
    )
    parser.add_argument(
        "--sub2api-upload-allow-unhealthy",
        action="store_true",
        help="上传 Sub2API 时不跳过 access 已过期/缺 refresh 的账号（默认跳过并提示 remint）",
    )
    parser.add_argument(
        "--cpa-list",
        action="store_true",
        help="列出线上 CPA 已导入的 auth 凭证（Management API）",
    )
    parser.add_argument(
        "--cpa-delete",
        nargs="+",
        default=None,
        metavar="PATTERN",
        help="模糊删除线上凭证：子串 / glob(xai-*) / re:正则；先 dry-run，加 --yes 才真正删除",
    )
    parser.add_argument(
        "--cpa-delete-all",
        action="store_true",
        help="快速清空线上全部 auth 凭证（DELETE ?all=true）；必须加 --yes 才执行，否则仅预览",
    )
    parser.add_argument(
        "--cpa-delete-dry-run",
        action="store_true",
        help="仅预览 --cpa-delete / --cpa-delete-all 匹配结果（默认在未加 --yes 时就是 dry-run）",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="确认执行危险操作（如 --cpa-delete / --cpa-delete-all 真正删除）",
    )
    args = parser.parse_args()
    if getattr(args, "menu", False):
        from grok_register.menu import run_interactive_menu

        return run_interactive_menu()

    reg.load_config()
    cfg0 = getattr(reg, "config", {}) or {}

    # Proxy / headless CLI + env → config, then refresh pool cache
    if args.proxy_pool:
        reg.config["proxy_pool"] = args.proxy_pool.strip()
        cfg0["proxy_pool"] = reg.config["proxy_pool"]
        # Prefer pool when CLI passes it
        os.environ["PROXY_POOL"] = reg.config["proxy_pool"]
    if args.proxy:
        reg.config["proxy"] = args.proxy.strip()
        cfg0["proxy"] = reg.config["proxy"]
        os.environ["PROXY"] = reg.config["proxy"]
    if args.headed:
        reg.config["headless"] = False
        reg.config["cpa_headless"] = False  # standalone mint must also be headed
        cfg0["headless"] = False
        cfg0["cpa_headless"] = False
        os.environ["HEADLESS"] = "0"
    elif args.headless:
        reg.config["headless"] = True
        cfg0["headless"] = True
        os.environ["HEADLESS"] = "1"
    if getattr(args, "headless_mode", None):
        mode = str(args.headless_mode or "").strip()
        if mode:
            reg.config["headless_mode"] = mode
            cfg0["headless_mode"] = mode
            os.environ["HEADLESS_MODE"] = mode
    # else: leave config/env; resolve_headless() decides (desktop headed, Linux server headless)

    try:
        from grok_register.proxy.pool import print_startup_report, refresh_proxy_cache

        refresh_proxy_cache(reg.config)
        ext = str(PACKAGE_DIR / "browser" / "extensions" / "turnstilePatch")
        print_startup_report(reg.config, extension_path=ext)
    except Exception as exc:
        print(f"[!] proxy/headless init: {exc}", flush=True)

    # List / fuzzy-delete / delete-all / replace online Sub2API accounts
    if (
        args.sub2api_list
        or args.sub2api_delete
        or args.sub2api_delete_all
        or args.sub2api_delete_latest
        or args.sub2api_replace_latest
    ):
        cfg = getattr(reg, "config", {}) or {}
        if getattr(args, "sub2api_upload_allow_unhealthy", False):
            cfg = dict(cfg)
            cfg["sub2api_upload_skip_unhealthy"] = False
            reg.config["sub2api_upload_skip_unhealthy"] = False
        log_fn = lambda m: print(m, flush=True)
        platform = str(getattr(args, "sub2api_platform", None) or "grok").strip() or "grok"

        def _resolve_latest_sub2api_batch():
            latest = None
            try:
                from grok_register.export.cpa_export import find_latest_export_batch

                latest = find_latest_export_batch(cfg, require_cpa_files=False)
            except Exception as exc:
                print(f"[!] 查找最新批次失败: {exc}", flush=True)
                return None, 1
            if latest is None:
                parent = Path(
                    cfg.get("export_batch_parent")
                    or cfg.get("export_root")
                    or "./exports"
                )
                if not parent.is_absolute():
                    parent = (PROJECT_ROOT / parent).resolve()
                if parent.is_dir():
                    cands = sorted(
                        [
                            p
                            for p in parent.iterdir()
                            if p.is_dir() and (p / "sub2api").is_dir()
                        ],
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    latest = cands[0] if cands else None
            if latest is None:
                print("[!] 未找到含 sub2api/ 的最新批次目录", flush=True)
                return None, 1
            return latest, 0

        if args.sub2api_delete_latest:
            latest_batch, err_code = _resolve_latest_sub2api_batch()
            if err_code:
                return err_code
            dry_run = (not args.yes) or bool(getattr(args, "cpa_delete_dry_run", False))
            print(
                f"[*] Sub2API 删除最新批: batch={latest_batch.name} "
                f"platform={platform} {'dry-run' if dry_run else 'EXECUTE'}",
                flush=True,
            )
            if dry_run:
                print(
                    "[*] 预览：按本地最新批 email/name 删除线上匹配账号。"
                    "确认删除请追加: --yes",
                    flush=True,
                )
            summary = reg.delete_sub2api_latest_batch_on_cloud(
                sub2api_dir=str(latest_batch),
                cfg=cfg,
                log_callback=log_fn,
                dry_run=dry_run,
                platform=platform,
            )
            if not summary.get("ok") and summary.get("error"):
                print(f"[!] 删除最新批失败: {summary.get('error')}", flush=True)
                return 1
            n = int(summary.get("match_count") or 0)
            if dry_run:
                print(
                    f"=== dry-run 删除最新批: match={n} "
                    f"local_files={len(summary.get('paths') or [])} "
                    f"tokens={len(summary.get('local_tokens') or [])} "
                    f"batch={latest_batch.name} ===",
                    flush=True,
                )
                print(
                    "[*] 确认删除: python register_cli.py --sub2api-delete-latest --yes",
                    flush=True,
                )
                return 0
            ok_n = int(summary.get("ok_count") or 0)
            fail_n = int(summary.get("fail_count") or 0)
            print(
                f"=== 删除最新批完成: ok={ok_n} fail={fail_n} matched={n} "
                f"batch={latest_batch.name} ===",
                flush=True,
            )
            if ok_n == 0 and fail_n > 0:
                return 1
            if fail_n > 0:
                return 2
            return 0

        if args.sub2api_replace_latest:
            latest_batch, err_code = _resolve_latest_sub2api_batch()
            if err_code:
                return err_code
            dry_run = (not args.yes) or bool(getattr(args, "cpa_delete_dry_run", False))
            delete_scope = (
                "all" if args.sub2api_replace_all_platform else "matched"
            )
            print(
                f"[*] Sub2API replace latest: batch={latest_batch.name} "
                f"platform={platform} scope={delete_scope} "
                f"{'dry-run' if dry_run else 'EXECUTE'}",
                flush=True,
            )
            if dry_run:
                print(
                    "[*] 预览：将删除匹配账号并上传。确认执行请追加: --yes",
                    flush=True,
                )
            summary = reg.replace_sub2api_upload_on_cloud(
                sub2api_dir=str(latest_batch),
                cfg=cfg,
                log_callback=log_fn,
                dry_run=dry_run,
                platform=platform,
                delete_scope=delete_scope,
            )
            if not summary.get("ok") and summary.get("error"):
                print(f"[!] replace 失败: {summary.get('error')}", flush=True)
                return 1
            if dry_run:
                print(
                    f"=== dry-run replace: delete_match={summary.get('match_count', 0)} "
                    f"local_files={len(summary.get('paths') or [])} "
                    f"tokens={len(summary.get('local_tokens') or [])} ===",
                    flush=True,
                )
                print(
                    "[*] 确认执行: python register_cli.py --sub2api-replace-latest --yes",
                    flush=True,
                )
                return 0
            up = summary.get("upload") or {}
            print(
                f"=== Sub2API replace 完成: deleted={summary.get('deleted_count', 0)} "
                f"delete_fail={summary.get('delete_fail_count', 0)} "
                f"created={up.get('account_created', 0)} "
                f"upload_fail_acc={up.get('account_failed', 0)} ===",
                flush=True,
            )
            if not summary.get("ok"):
                return 2
            return 0

        if args.sub2api_list and not args.sub2api_delete and not args.sub2api_delete_all:
            listed = reg.list_sub2api_accounts_on_cloud(
                cfg=cfg, log_callback=log_fn, platform=platform
            )
            if not listed.get("ok"):
                print(f"[!] 列出失败: {listed.get('error')}", flush=True)
                return 1
            accounts = listed.get("accounts") or []
            print(
                f"=== 线上 Sub2API 账号: {len(accounts)} 个 "
                f"(platform={platform}, total={listed.get('total', len(accounts))}) ===",
                flush=True,
            )
            for e in accounts:
                email = (
                    e.get("_email")
                    or (e.get("credentials") or {}).get("email")
                    or (e.get("extra") or {}).get("email")
                    or "-"
                )
                print(
                    f"  id={e.get('id')}  name={e.get('name') or '-'}  "
                    f"email={email}  "
                    f"platform={e.get('platform') or '-'}  type={e.get('type') or '-'}  "
                    f"status={e.get('status') or '-'}",
                    flush=True,
                )
            return 0

        if args.sub2api_delete_all and args.sub2api_delete:
            print(
                "[!] 同时指定了 --sub2api-delete-all 与 --sub2api-delete：以全删为准",
                flush=True,
            )

        dry_run = (not args.yes) or bool(getattr(args, "cpa_delete_dry_run", False))
        if dry_run and not args.yes:
            print(
                "[*] 删除预览（dry-run）。确认删除请追加: --yes",
                flush=True,
            )
        if args.sub2api_delete_all and not dry_run:
            print(
                f"[!] 即将删除线上 Sub2API platform={platform} 下全部账号（逐条 DELETE）…",
                flush=True,
            )

        if args.sub2api_delete_all:
            summary = reg.delete_all_sub2api_accounts_on_cloud(
                cfg=cfg,
                log_callback=log_fn,
                dry_run=dry_run,
                platform=platform,
            )
        else:
            summary = reg.delete_sub2api_accounts_on_cloud(
                patterns=args.sub2api_delete,
                cfg=cfg,
                log_callback=log_fn,
                dry_run=dry_run,
                platform=platform,
            )
        if not summary.get("ok") and summary.get("error"):
            print(f"[!] 删除/匹配失败: {summary.get('error')}", flush=True)
            return 1
        n = int(summary.get("match_count") or len(summary.get("matched") or []))
        if dry_run:
            mode = "全删" if args.sub2api_delete_all else f"patterns={args.sub2api_delete}"
            print(
                f"=== dry-run: 匹配 {n} 个（线上共 {summary.get('total_online', '?')}）"
                f" {mode} platform={platform} ===",
                flush=True,
            )
            if args.sub2api_delete_all:
                print(
                    "[*] 确认删除: python register_cli.py --sub2api-delete-all --yes",
                    flush=True,
                )
            return 0
        ok_n = int(summary.get("ok_count") or 0)
        fail_n = int(summary.get("fail_count") or 0)
        label = "全删" if args.sub2api_delete_all else "模糊删除"
        print(
            f"=== 线上 Sub2API {label}完成: ok={ok_n} fail={fail_n} matched={n} ===",
            flush=True,
        )
        if ok_n == 0 and fail_n > 0:
            return 1
        if fail_n > 0:
            return 2
        return 0

    # List / fuzzy-delete / delete-all online CPA auth files (Management API)
    if args.cpa_list or args.cpa_delete or args.cpa_delete_all:
        cfg = getattr(reg, "config", {}) or {}
        log_fn = lambda m: print(m, flush=True)
        if args.cpa_list and not args.cpa_delete and not args.cpa_delete_all:
            listed = reg.list_cpa_auth_files_on_cloud(cfg=cfg, log_callback=log_fn)
            if not listed.get("ok"):
                print(f"[!] 列出失败: {listed.get('error')}", flush=True)
                return 1
            files = listed.get("files") or []
            print(f"=== 线上 CPA 凭证: {len(files)} 个 ===", flush=True)
            for e in files:
                print(
                    f"  {e.get('name') or e.get('id') or '?'}  "
                    f"email={e.get('email') or '-'}  "
                    f"provider={e.get('provider') or '-'}  "
                    f"status={e.get('status') or '-'}",
                    flush=True,
                )
            return 0

        if args.cpa_delete_all and args.cpa_delete:
            print(
                "[!] 同时指定了 --cpa-delete-all 与 --cpa-delete：以全删为准",
                flush=True,
            )

        # delete path (fuzzy or all)
        dry_run = (not args.yes) or bool(args.cpa_delete_dry_run)
        if dry_run and not args.yes:
            print(
                "[*] 删除预览（dry-run）。确认删除请追加: --yes",
                flush=True,
            )
        if args.cpa_delete_all and not dry_run:
            print(
                "[!] 即将清空线上 CPA 全部 auth 凭证（DELETE ?all=true）…",
                flush=True,
            )

        if args.cpa_delete_all:
            summary = reg.delete_all_cpa_auth_files_on_cloud(
                cfg=cfg,
                log_callback=log_fn,
                dry_run=dry_run,
            )
        else:
            summary = reg.delete_cpa_auth_files_on_cloud(
                patterns=args.cpa_delete,
                cfg=cfg,
                log_callback=log_fn,
                dry_run=dry_run,
            )
        if not summary.get("ok") and summary.get("error"):
            print(f"[!] 删除/匹配失败: {summary.get('error')}", flush=True)
            return 1
        n = int(summary.get("match_count") or len(summary.get("matched") or []))
        if dry_run:
            mode = "全删" if args.cpa_delete_all else f"patterns={args.cpa_delete}"
            print(
                f"=== dry-run: 匹配 {n} 个（线上共 {summary.get('total_online', '?')}）"
                f" {mode} ===",
                flush=True,
            )
            if args.cpa_delete_all:
                print(
                    "[*] 确认清空全部请执行: python register_cli.py --cpa-delete-all --yes",
                    flush=True,
                )
            return 0 if n >= 0 else 1
        ok_n = int(summary.get("ok_count") or 0)
        fail_n = int(summary.get("fail_count") or 0)
        if args.cpa_delete_all and summary.get("deleted_count") is not None:
            ok_n = int(summary.get("ok_count") or summary.get("deleted_count") or 0)
        label = "全删" if args.cpa_delete_all else "模糊删除"
        print(
            f"=== 线上 CPA {label}完成: ok={ok_n} fail={fail_n} matched={n} ===",
            flush=True,
        )
        if ok_n == 0 and fail_n > 0:
            return 1
        if fail_n > 0:
            return 2
        return 0

    # Batch-push local CPA JSON files into online CLIProxyAPI (Management API)
    # --cpa-upload-latest alone is enough (implies upload of newest batch only)
    if args.upload_cpa_cloud or args.cpa_upload_latest:
        cfg = getattr(reg, "config", {}) or {}
        upload_dir = (args.cpa_upload_dir or "").strip() or None
        upload_files = args.cpa_upload_files
        recursive = bool(args.cpa_upload_all)
        latest_batch = None

        if args.cpa_upload_latest:
            if upload_dir or upload_files or recursive:
                print(
                    "[!] --cpa-upload-latest 与 --cpa-upload-dir/--cpa-upload-files/--cpa-upload-all "
                    "同时指定时，以 latest 为准",
                    flush=True,
                )
            try:
                from grok_register.export.cpa_export import find_latest_export_batch

                latest_batch = find_latest_export_batch(cfg)
            except Exception as exc:
                print(f"[!] 查找最新批次失败: {exc}", flush=True)
                return 1
            if latest_batch is None:
                parent = (
                    cfg.get("export_batch_parent")
                    or cfg.get("export_root")
                    or "./exports"
                )
                print(
                    f"[!] 未找到可上传的最新批次（在 {parent} 下无含 xai-*.json 的 batch 目录）",
                    flush=True,
                )
                return 1
            upload_dir = str(latest_batch)
            upload_files = None
            recursive = False
            n_json = 0
            try:
                cpa_sub = latest_batch / "cpa"
                scan = cpa_sub if cpa_sub.is_dir() else latest_batch
                n_json = len(list(scan.glob("xai-*.json")))
            except Exception:
                pass
            print(
                f"[*] 最新批次: {latest_batch.name}\n"
                f"    path={latest_batch}\n"
                f"    cpa 文件约 {n_json} 个",
                flush=True,
            )
        elif recursive and not upload_dir and not upload_files:
            # Default: if no files/dir and --cpa-upload-all, scan exports parent
            upload_dir = (
                cfg.get("export_batch_parent")
                or cfg.get("export_root")
                or "./exports"
            )

        workers = int(getattr(args, "cpa_upload_workers", 0) or 0)
        if workers > 0:
            cfg = dict(cfg)
            cfg["cpa_cloud_upload_workers"] = workers
        workers_show = workers or int(cfg.get("cpa_cloud_upload_workers") or 8)
        print(
            f"[*] 上传本地 CPA → 线上 CLIProxyAPI "
            f"api={cfg.get('cpa_cloud_api_base') or os.environ.get('CPA_CLOUD_API_BASE') or '(未配置)'} "
            f"dir={upload_dir or ('(files only)' if upload_files else (cfg.get('cpa_auth_dir') or './exports'))} "
            f"files={len(upload_files) if upload_files else 0} "
            f"recursive={recursive} latest={bool(args.cpa_upload_latest)} "
            f"workers={workers_show}",
            flush=True,
        )
        summary = reg.upload_cpa_auth_dir_to_cloud(
            cpa_dir=upload_dir,
            cfg=cfg,
            log_callback=lambda m: print(m, flush=True),
            force=True,
            files=upload_files,
            recursive=recursive,
            workers=workers if workers > 0 else None,
        )
        ok_n = int(summary.get("ok_count") or 0)
        fail_n = int(summary.get("fail_count") or 0)
        elapsed = summary.get("elapsed_sec")
        elapsed_s = f" elapsed={elapsed}s" if elapsed is not None else ""
        w_used = summary.get("workers")
        w_s = f" workers={w_used}" if w_used is not None else ""
        print(
            f"=== 线上 CPA 上传完成: ok={ok_n} fail={fail_n} total={summary.get('total', 0)}"
            + elapsed_s
            + w_s
            + (f" batch={latest_batch.name}" if latest_batch is not None else "")
            + " ===",
            flush=True,
        )
        if summary.get("error") == "dir_not_found":
            return 1
        if ok_n == 0 and fail_n > 0:
            return 1
        if fail_n > 0:
            return 2  # partial
        return 0 if ok_n > 0 else 0

    # Batch-push local Sub2API JSON into online Sub2API (admin accounts/data)
    if args.upload_sub2api_cloud or args.sub2api_upload_latest:
        cfg = getattr(reg, "config", {}) or {}
        if getattr(args, "sub2api_upload_allow_unhealthy", False):
            cfg = dict(cfg)
            cfg["sub2api_upload_skip_unhealthy"] = False
            reg.config["sub2api_upload_skip_unhealthy"] = False
        upload_dir = (args.sub2api_upload_dir or "").strip() or None
        upload_files = args.sub2api_upload_files
        recursive = bool(args.sub2api_upload_all)
        latest_batch = None

        if args.sub2api_upload_latest:
            try:
                from grok_register.export.cpa_export import find_latest_export_batch

                latest_batch = find_latest_export_batch(cfg, require_cpa_files=False)
                # prefer batch that has sub2api files; if none, try with require_cpa false still
                if latest_batch is None:
                    # fall back: any batch dir under exports with sub2api/
                    parent = Path(
                        cfg.get("export_batch_parent")
                        or cfg.get("export_root")
                        or "./exports"
                    )
                    if not parent.is_absolute():
                        parent = (PROJECT_ROOT / parent).resolve()
                    if parent.is_dir():
                        cands = sorted(
                            [p for p in parent.iterdir() if p.is_dir() and (p / "sub2api").is_dir()],
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        )
                        latest_batch = cands[0] if cands else None
            except Exception as exc:
                print(f"[!] 查找最新批次失败: {exc}", flush=True)
                return 1
            if latest_batch is None:
                print("[!] 未找到含 sub2api/ 的最新批次目录", flush=True)
                return 1
            upload_dir = str(latest_batch)
            upload_files = None
            recursive = False
            print(
                f"[*] Sub2API 最新批次: {latest_batch.name}\n"
                f"    path={latest_batch}",
                flush=True,
            )
        elif recursive and not upload_dir and not upload_files:
            upload_dir = (
                cfg.get("export_batch_parent")
                or cfg.get("export_root")
                or "./exports"
            )

        print(
            f"[*] 上传本地 Sub2API → 线上 "
            f"api={cfg.get('sub2api_cloud_api_base') or os.environ.get('SUB2API_BASE_URL') or '(未配置)'} "
            f"dir={upload_dir or ('(files only)' if upload_files else (cfg.get('sub2api_export_dir') or './exports'))} "
            f"files={len(upload_files) if upload_files else 0} "
            f"recursive={recursive} latest={bool(args.sub2api_upload_latest)}",
            flush=True,
        )
        summary = reg.upload_sub2api_dir_to_cloud(
            sub2api_dir=upload_dir,
            cfg=cfg,
            log_callback=lambda m: print(m, flush=True),
            force=True,
            files=upload_files,
            recursive=recursive,
        )
        ok_n = int(summary.get("ok_count") or 0)
        fail_n = int(summary.get("fail_count") or 0)
        print(
            f"=== 线上 Sub2API 上传完成: files_ok={ok_n} files_fail={fail_n} "
            f"total={summary.get('total', 0)} "
            f"accounts_created={summary.get('account_created', 0)} "
            f"accounts_failed={summary.get('account_failed', 0)}"
            + (f" batch={latest_batch.name}" if latest_batch is not None else "")
            + " ===",
            flush=True,
        )
        if ok_n == 0 and fail_n > 0:
            return 1
        if fail_n > 0:
            return 2
        return 0 if ok_n > 0 else 0

    # Per-run batch export directory (accounts + cpa + sub2api)
    use_batch = (not args.no_batch) and bool(
        (getattr(reg, "config", {}) or {}).get("export_batch_enabled", True)
    )
    if args.batch_dir:
        use_batch = True
    if use_batch and not args.remint_missing:
        try:
            from grok_register.export.cpa_export import apply_batch_export_layout

            cfg = getattr(reg, "config", {}) or {}
            if args.batch_dir:
                bdir = Path(os.path.expanduser(args.batch_dir))
                if not bdir.is_absolute():
                    bdir = (PROJECT_ROOT / bdir).resolve()
                else:
                    bdir = bdir.resolve()
                bdir.mkdir(parents=True, exist_ok=True)
                (bdir / "cpa").mkdir(exist_ok=True)
                (bdir / "sub2api").mkdir(exist_ok=True)
                meta = {
                    "batch_id": bdir.name,
                    "batch_dir": str(bdir),
                    "cpa_dir": str(bdir / "cpa"),
                    "sub2api_dir": str(bdir / "sub2api"),
                    "accounts_file": str(bdir / "accounts.txt"),
                }
                cfg["export_root"] = str(bdir)
                cfg["cpa_auth_dir"] = str(bdir / "cpa")
                cfg["sub2api_export_dir"] = str(bdir / "sub2api")
                cfg["sub2api_combined_file"] = str(bdir / "sub2api" / "sub2api-accounts.json")
                cfg["export_batch_id"] = bdir.name
                cfg["export_batch_dir"] = str(bdir)
                cfg["export_batch_accounts_file"] = str(bdir / "accounts.txt")
                if cfg.get("export_batch_also_global_accounts") is None:
                    cfg["export_batch_also_global_accounts"] = True
            else:
                meta = apply_batch_export_layout(
                    cfg,
                    batch_name=(args.batch_name or "").strip() or None,
                )
            reg.config = cfg
            cfg0 = cfg
            if args.accounts_file is None:
                args.accounts_file = meta["accounts_file"]
            print(
                f"[*] 本批导出目录: {meta.get('batch_dir')}\n"
                f"    accounts → {meta.get('accounts_file')}\n"
                f"    cpa      → {meta.get('cpa_dir')}\n"
                f"    sub2api  → {meta.get('sub2api_dir')}",
                flush=True,
            )
        except Exception as batch_exc:
            print(f"[!] 创建 batch 目录失败，回退扁平 exports: {batch_exc}", flush=True)
            use_batch = False

    if args.accounts_file is None:
        args.accounts_file = str(PROJECT_ROOT / "accounts" / "accounts_cli.txt")

    # Recover CPA for registered accounts that missed mint (e.g. tunnel 503)
    if args.remint_missing:
        # remint writes into current config cpa_auth_dir (batch if --batch-dir set)
        if args.batch_dir:
            try:
                from grok_register.export.cpa_export import apply_batch_export_layout  # noqa: F401

                bdir = Path(os.path.expanduser(args.batch_dir))
                if not bdir.is_absolute():
                    bdir = (PROJECT_ROOT / bdir).resolve()
                cfg = getattr(reg, "config", {}) or {}
                cfg["export_root"] = str(bdir)
                cfg["cpa_auth_dir"] = str(bdir / "cpa")
                cfg["sub2api_export_dir"] = str(bdir / "sub2api")
                cfg["sub2api_combined_file"] = str(bdir / "sub2api" / "sub2api-accounts.json")
                reg.config = cfg
            except Exception as exc:
                print(f"[!] remint batch-dir: {exc}", flush=True)
        acc = args.accounts_file or str(PROJECT_ROOT / "accounts" / "accounts_cli.txt")
        return remint_missing_from_accounts(acc, getattr(reg, "config", {}) or {})

    # Global CF cookie pre-warm (only when there are usable proxies / direct)
    try:
        from grok_register.browser.cf_prewarm import prewarm_cf_for_config
        from grok_register.proxy.pool import proxy_pool_stats

        stats = proxy_pool_stats(reg.config)
        # Skip prewarm noise when pool configured but every gateway failed probe
        if stats.get("total", 0) > 0 and stats.get("available", 0) == 0:
            print(
                "[*] 跳过 CF 预缓存：代理池 0 可用。请先解决 CONNECT 400/超时，"
                "或临时去掉 --proxy-pool 用 --headed 直连",
                flush=True,
            )
        else:
            prewarm_cf_for_config(reg.config, log=lambda m: print(m, flush=True))
    except Exception as exc:
        print(f"[!] CF prewarm: {exc}", flush=True)

    threads = max(1, min(args.threads, 10))
    # Direct (no proxy): multi-thread almost always CF-blocks the whole batch.
    try:
        from grok_register.proxy.pool import get_proxy_list

        has_proxy = bool(get_proxy_list(reg.config))
    except Exception:
        has_proxy = False
    if threads > 1 and not has_proxy and not args.force_multi:
        print(
            f"[*] 直连模式自动将线程 {threads}→1（同一出口 IP 多开极易 Cloudflare）。\n"
            f"    需要多线程请配 --proxy-pool 或显式 --force-multi",
            flush=True,
        )
        threads = 1
    fast = bool(args.fast) and not bool(args.no_fast)

    mint_workers = resolve_mint_workers(
        cli_value=args.mint_workers,
        threads=threads,
        config=cfg0,
        inline_mint=bool(args.inline_mint),
    )
    do_mint_inline = mint_workers == 0
    mint_qmax = resolve_mint_queue_max(
        cfg0,
        mint_workers,
        cli_value=(None if args.mint_queue_max < 0 else args.mint_queue_max),
    )

    # perf knobs
    reg.configure_perf(
        fast=fast,
        sleep_scale=0.15 if fast else 1.0,
        skip_debug_io=fast,
        cookie_snapshot=bool(args.cookie_snapshot) or not fast,
        async_side_effects=True,
        browser_reuse=not args.no_browser_reuse,
        browser_recycle_every=max(1, int(args.browser_recycle_every)),
    )

    _reset_stats()

    # 断点续跑
    done_count = 0
    if os.path.exists(args.accounts_file):
        with open(args.accounts_file) as f:
            done_count = sum(1 for line in f if line.strip())

    # batch_total: how many this run will attempt (for progress display)
    batch_total: int | None = None
    if args.extra and args.extra > 0:
        target_total = done_count + args.extra
        remaining = args.extra
        batch_total = args.extra
        print(
            f"[*] 配置加载完成，本批新注册 {args.extra} 个"
            f"（启动时已有 {done_count} → 跑完后目标总量 {target_total}），"
            f"注册线程={threads} mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
        print(
            f"[*] 进度显示：本批第 n/{args.extra}（全局序号从 {done_count + 1} 起）",
            flush=True,
        )
        args.count = target_total
    elif args.count == 0:
        remaining = None
        batch_total = None
        print(
            f"[*] 配置加载完成，不限数量，注册线程={threads} mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
    else:
        remaining = max(0, args.count - done_count)
        batch_total = remaining if remaining > 0 else 0
        print(
            f"[*] 配置加载完成，目标总量 {args.count}（已有 {done_count}，本批还需 {remaining}），"
            f"注册线程={threads} mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
        if remaining > 0 and done_count > 0:
            print(
                f"[*] 进度显示：本批第 n/{remaining}（全局序号从 {done_count + 1} 起）",
                flush=True,
            )
    print(f"[*] accounts_file = {args.accounts_file}", flush=True)
    print("[*] registration=browser (Chromium DOM)", flush=True)
    if done_count > 0 and not (args.extra and args.extra > 0):
        print(f"[*] 断点续跑：accounts 已有 {done_count} 行", flush=True)
    if remaining is not None and remaining <= 0:
        print("[*] 所有账号已完成，无需继续（可用 --extra N 再注册）", flush=True)
        return 0

    # Background mailbox pre-create (hides TempMail/API latency during DOM wait)
    try:
        pool_size = int((getattr(reg, "config", {}) or {}).get("mail_pool_size", 3) or 0)
    except Exception:
        pool_size = 3
    pool_size = max(0, min(pool_size, 10))
    if pool_size > 0:
        try:
            from grok_register.mail.pool import start_mail_pool

            start_mail_pool(
                reg._get_email_and_token_direct,
                size=pool_size,
                log=lambda m: print(m, flush=True),
            )
        except Exception as exc:
            print(f"[!] mail pool: {exc}", flush=True)
    else:
        print("[*] 邮箱预创建池关闭 (mail_pool_size=0)", flush=True)

    log_thread = threading.Thread(target=_log_writer, daemon=True)
    log_thread.start()

    try:
        reg.TabPool.init(reg.create_browser_options, log_callback=lambda m: log(0, m))
    except Exception as exc:
        print(f"[!] 浏览器初始化失败: {exc}", flush=True)
        try:
            from grok_register.mail.pool import stop_mail_pool

            stop_mail_pool()
        except Exception:
            pass
        return 1

    task_queue: queue.Queue = queue.Queue()
    mint_queue: queue.Queue | None = queue.Queue() if not do_mint_inline else None
    if mint_queue is not None:
        mint_queue._reg_qmax = mint_qmax  # type: ignore[attr-defined]
    global _next_idx
    _next_idx[0] = done_count + 1
    if remaining is not None:
        for i in range(done_count + 1, args.count + 1):
            task_queue.put(i)
    else:
        for i in range(done_count + 1, done_count + threads * 5 + 1):
            task_queue.put(i)
        _next_idx[0] = done_count + threads * 5 + 1

    forever = remaining is None
    cfg = getattr(reg, "config", {}) or {}

    # mint workers first (so queue consumers ready)
    mint_threads: list[threading.Thread] = []
    if mint_queue is not None and mint_workers > 0:
        for i in range(1, mint_workers + 1):
            wid = f"M{i}"
            t = threading.Thread(
                target=_mint_worker,
                args=(wid, mint_queue, cfg),
                daemon=True,
                name=f"mint-{i}",
            )
            t.start()
            mint_threads.append(t)

    # Stagger multi-thread starts (config thread_start_interval, default 0.8s)
    try:
        start_iv = float(cfg.get("thread_start_interval", 0.8) or 0.8)
    except Exception:
        start_iv = 0.8
    start_iv = max(0.0, min(start_iv, 30.0))
    if threads > 1:
        print(
            f"[*] 注册线程错峰启动 interval={start_iv}s "
            f"(多线程无头极易触发 Cloudflare；本机推荐 --threads 1 --headed)",
            flush=True,
        )

    reg_threads: list[threading.Thread] = []
    for wid in range(1, threads + 1):
        delay = (wid - 1) * start_iv
        t = threading.Thread(
            target=_register_worker,
            args=(
                wid,
                task_queue,
                args.count,
                args.accounts_file,
                mint_queue,
                forever,
                do_mint_inline,
                delay,
                done_count,
                batch_total,
            ),
            daemon=True,
            name=f"reg-{wid}",
        )
        t.start()
        reg_threads.append(t)

    try:
        for t in reg_threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[!] 用户中断", flush=True)

    # drain mint queue
    if mint_queue is not None:
        log(0, f"[cpa] 等待 mint 队列清空（qsize≈{mint_queue.qsize()}）...")
        mint_queue.join()
        for _ in mint_threads:
            mint_queue.put(_MINT_STOP)
        for t in mint_threads:
            t.join(timeout=600)

    try:
        reg.shutdown_browser()
    except Exception:
        pass

    try:
        from grok_register.mail.pool import stop_mail_pool

        stop_mail_pool()
    except Exception:
        pass

    # stop side-effect pool
    try:
        pool = getattr(reg, "_side_effect_pool", None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass

    _log_queue.put(None)
    log_thread.join(timeout=2)

    cfg_end = getattr(reg, "config", {}) or {}
    s = _print_run_summary(cfg_end)
    return 0 if s.get("reg_success", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
