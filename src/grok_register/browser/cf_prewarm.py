"""Cloudflare cookie pre-warm for xAI signup (inspired by headless+proxy CF cache).

Flow (same idea as production OpenAI stacks):
  1. After proxy health probe, pick available gateways (or direct)
  2. Launch short-lived Chromium (headless preferred for cache job)
  3. Visit accounts.x.ai / auth.x.ai, collect cf_clearance / __cf_bm etc.
  4. Cache cookies keyed by proxy gateway URL
  5. Inject into each register worker browser before open signup

Notes:
  - Soft CF challenges may only partially succeed in pure headless; still helps
    when clearance is issued.
  - Proxies with user:pass may not fully apply to Chromium --proxy-server
    (same limitation as register path). Prefer IP-allowlist or local forwarder.
  - Disable with CF_PREWARM=0 / config cf_prewarm=false.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

LogFn = Callable[[str], None]

# Per-proxy cookie cache: proxy_url | "direct" -> list of cookie dicts
_lock = threading.Lock()
_cache: dict[str, list[dict[str, Any]]] = {}
_meta: dict[str, dict[str, Any]] = {}

CF_COOKIE_NAMES = {
    "cf_clearance",
    "__cf_bm",
    "cf_chl_rc_i",
    "cf_chl_2",
    "cf_chl_prog",
    "_cfuvid",
}

# Keep short — multi-gateway sequential Chrome is slow.
DEFAULT_PREWARM_URLS = (
    "https://accounts.x.ai/sign-up?redirect=grok-com",
)


def _env_truthy(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    v = str(raw).strip().lower()
    if v == "":
        return None
    if v in ("1", "true", "yes", "on", "y"):
        return True
    if v in ("0", "false", "no", "off", "n"):
        return False
    return None


def cf_prewarm_enabled(config: dict[str, Any] | None = None) -> bool:
    """Prewarm only helps when using real proxies.

    Direct/desktop multi-thread: headless prewarm cookies + headed browsers often
    makes Cloudflare *worse* (Attention Required). Default off without proxy.
    Force on: CF_PREWARM=1
    """
    env = _env_truthy("CF_PREWARM")
    if env is not None:
        return env
    cfg = config or {}
    has_proxy = False
    try:
        from grok_register.proxy.pool import get_proxy_list

        has_proxy = bool(get_proxy_list(cfg))
    except Exception:
        has_proxy = False
    if not has_proxy:
        return False
    if "cf_prewarm" in cfg and cfg.get("cf_prewarm") is not None:
        val = cfg.get("cf_prewarm")
        if isinstance(val, str):
            s = val.strip().lower()
            if s in ("auto", ""):
                return True
            return s in ("1", "true", "yes", "on")
        return bool(val)
    return True


def cache_key(proxy: str | None) -> str:
    p = (proxy or "").strip()
    return p if p else "direct"


def get_cached_cf_cookies(proxy: str | None = None) -> list[dict[str, Any]]:
    key = cache_key(proxy)
    with _lock:
        return list(_cache.get(key) or [])


def set_cached_cf_cookies(proxy: str | None, cookies: list[dict[str, Any]]) -> None:
    key = cache_key(proxy)
    cleaned = _normalize_cookies(cookies)
    with _lock:
        _cache[key] = cleaned
        _meta[key] = {"ts": time.time(), "count": len(cleaned)}


def clear_cf_cache() -> None:
    with _lock:
        _cache.clear()
        _meta.clear()


def _normalize_cookies(cookies: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or c.get("Name") or "").strip()
        value = c.get("value") if "value" in c else c.get("Value")
        if not name or value is None:
            continue
        # Prefer CF-related; keep a few host cookies too if from x.ai
        domain = str(c.get("domain") or c.get("Domain") or ".x.ai")
        path = str(c.get("path") or c.get("Path") or "/")
        key = (name, domain)
        if key in seen:
            continue
        seen.add(key)
        item = {
            "name": name,
            "value": str(value),
            "domain": domain if domain.startswith(".") or "x.ai" in domain else ".x.ai",
            "path": path,
            "secure": bool(c.get("secure", c.get("Secure", True))),
        }
        if name in CF_COOKIE_NAMES or "x.ai" in domain or domain.endswith("x.ai"):
            out.append(item)
    # Expand CF cookies to common hosts
    extras: list[dict[str, Any]] = []
    for item in list(out):
        if item["name"] not in CF_COOKIE_NAMES and not item["name"].startswith("cf_"):
            continue
        for dom in (".x.ai", "accounts.x.ai", ".accounts.x.ai", "auth.x.ai", ".auth.x.ai"):
            k = (item["name"], dom)
            if k in seen:
                continue
            clone = dict(item)
            clone["domain"] = dom
            extras.append(clone)
            seen.add(k)
    out.extend(extras)
    return out


def _export_page_cookies(page: Any) -> list[dict[str, Any]]:
    cookies = None
    for getter in (
        lambda: page.cookies(all_domains=True, all_info=True),
        lambda: page.cookies(all_domains=True),
        lambda: page.cookies(),
    ):
        try:
            cookies = getter()
            if cookies:
                break
        except TypeError:
            continue
        except Exception:
            continue
    if not isinstance(cookies, list):
        return []
    return [c for c in cookies if isinstance(c, dict)]


def _looks_like_hard_cf_block(html: str, title: str) -> bool:
    t = f"{title} {html}".lower()
    return any(
        m in t
        for m in (
            "you have been blocked",
            "unable to access",
            "attention required",
        )
    )


def _prewarm_one(
    proxy: str,
    *,
    headless: bool,
    timeout_sec: float,
    urls: tuple[str, ...],
    log: LogFn,
) -> tuple[bool, int, str]:
    """Run one Chromium session; return (ok, cookie_count, message)."""
    from grok_register.proxy.pool import (
        find_browser_path,
        linux_server_chromium_flags,
        proxy_for_chromium,
        proxy_log_label,
    )

    label = proxy_log_label(proxy) if proxy else "direct"
    browser = None
    try:
        from DrissionPage import Chromium, ChromiumOptions

        opts = ChromiumOptions()
        opts.auto_port()
        opts.set_timeouts(base=2)
        for flag in (
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--mute-audio",
            "--window-size=1280,900",
        ):
            try:
                opts.set_argument(flag)
            except Exception:
                pass
        for flag in linux_server_chromium_flags():
            try:
                opts.set_argument(flag)
            except Exception:
                pass
        if headless:
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
        # turnstilePatch helps some challenges
        try:
            ext = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extensions", "turnstilePatch")
            if os.path.isdir(ext):
                opts.add_extension(ext)
        except Exception:
            pass
        chrome_proxy = proxy_for_chromium(proxy) if proxy else ""
        if chrome_proxy:
            try:
                opts.set_proxy(chrome_proxy)
            except Exception:
                opts.set_argument(f"--proxy-server={chrome_proxy}")
            log(
                f"[cf-prewarm] 启动 Chrome 预热 CF cookie（headless={headless} "
                f"proxy={label} via={chrome_proxy}）"
            )
        else:
            log(f"[cf-prewarm] 启动 Chrome 预热 CF cookie（headless={headless} proxy=direct）")
        browser = Chromium(opts)
        page = browser.latest_tab
        collected: list[dict[str, Any]] = []
        # Hard cap per gateway (default ~12–18s) so batch startup stays snappy
        deadline = time.time() + max(8.0, min(float(timeout_sec), 25.0))
        for url in urls:
            if time.time() > deadline:
                break
            try:
                log(f"[cf-prewarm] 访问 {url}")
                page.get(url)
                try:
                    page.wait.doc_loaded()
                except Exception:
                    pass
                time.sleep(1.0)
                # Brief poll for clearance; exit early on chrome/proxy error pages
                for _ in range(4):
                    if time.time() > deadline:
                        break
                    html = ""
                    title = ""
                    try:
                        title = str(page.title or "")
                        html = str(page.html or "")[:1500]
                    except Exception:
                        pass
                    low = f"{title} {html}".lower()
                    if any(
                        m in low
                        for m in (
                            "err_proxy",
                            "err_tunnel",
                            "err_connection",
                            "chromium authors",
                            "无法访问",
                            "proxy server",
                        )
                    ):
                        log(f"[cf-prewarm] 代理/网络错误页，跳过此网关 proxy={label}")
                        return False, 0, "proxy_error_page"
                    cks = _export_page_cookies(page)
                    collected.extend(cks)
                    names = {str(c.get("name") or "") for c in cks}
                    if "cf_clearance" in names or any(n.startswith("cf_") for n in names):
                        break
                    if _looks_like_hard_cf_block(html, title):
                        time.sleep(0.8)
                        continue
                    time.sleep(0.8)
            except Exception as exc:
                log(f"[cf-prewarm] 访问失败 {url}: {exc}")
                return False, 0, str(exc)

        cleaned = _normalize_cookies(collected)
        # Keep only if we got something useful
        useful = [c for c in cleaned if c["name"] in CF_COOKIE_NAMES or c["name"].startswith("cf_")]
        if useful:
            set_cached_cf_cookies(proxy, cleaned)
            log(f"[cf-prewarm] 完成 proxy={label} cookies={len(useful)} (normalized={len(cleaned)})")
            return True, len(useful), "ok"
        # Still store host cookies if any
        if cleaned:
            set_cached_cf_cookies(proxy, cleaned)
            log(f"[cf-prewarm] 弱缓存 proxy={label} cookies={len(cleaned)}（无 cf_clearance）")
            return True, len(cleaned), "weak"
        log(f"[cf-prewarm] 未获取到 CF cookie proxy={label}")
        return False, 0, "empty"
    except Exception as exc:
        log(f"[cf-prewarm] 失败 proxy={label}: {exc}")
        return False, 0, str(exc)
    finally:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass


def prewarm_cf_for_config(
    config: dict[str, Any] | None = None,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Pre-warm CF cookies for available proxies (or direct)."""
    out = log or (lambda m: print(m, flush=True))
    cfg = config or {}
    if not cf_prewarm_enabled(cfg):
        out("[*] CF cookie 预缓存：关闭（CF_PREWARM=0 / cf_prewarm=false）")
        return {"enabled": False, "ok": 0, "total": 0}

    from grok_register.proxy.pool import available_proxies, resolve_headless

    try:
        max_n = int(cfg.get("cf_prewarm_max_proxies", os.environ.get("CF_PREWARM_MAX", 2)) or 2)
    except Exception:
        max_n = 2
    max_n = max(1, min(max_n, 8))
    try:
        timeout = float(cfg.get("cf_prewarm_timeout_sec", os.environ.get("CF_PREWARM_TIMEOUT", 15)) or 15)
    except Exception:
        timeout = 15.0
    try:
        budget = float(cfg.get("cf_prewarm_budget_sec", os.environ.get("CF_PREWARM_BUDGET", 35)) or 35)
    except Exception:
        budget = 35.0
    budget = max(10.0, min(budget, 120.0))
    # Prewarm job usually headless even if register is headed
    env_h = _env_truthy("CF_PREWARM_HEADLESS")
    if env_h is not None:
        headless = env_h
    elif "cf_prewarm_headless" in cfg and cfg.get("cf_prewarm_headless") is not None:
        headless = bool(cfg.get("cf_prewarm_headless"))
    else:
        headless = True

    from grok_register.proxy.pool import get_proxy_list

    proxies = available_proxies(cfg)
    all_p = get_proxy_list(cfg)
    targets: list[str] = []
    if proxies:
        targets = proxies[:max_n]
    elif all_p:
        # All dead in probe: skip multi-Chrome prewarm (was 2+ minutes of no progress)
        out(
            "[*] 代理探测无可用节点 — 跳过 CF 预缓存（避免长时间开 Chrome）。"
            " 请先修好 PROXY 隧道（CONNECT 400/超时），或去掉 --proxy-pool 用 --headed 直连"
        )
        return {"enabled": True, "ok": 0, "total": 0, "cookies": 0, "skipped": "no_available_proxy"}
    else:
        targets = [""]  # true direct mode only when no proxy configured

    out(
        f"[*] 全局 CF cookie 预缓存（headless={headless}，目标 {len(targets)}，"
        f"单网关≤{int(timeout)}s，总预算≤{int(budget)}s）"
    )
    ok_n = 0
    total_cookies = 0
    t0 = time.time()
    for i, p in enumerate(targets):
        if time.time() - t0 >= budget:
            out(f"[*] CF 预缓存达到总预算 {int(budget)}s，停止后续网关")
            break
        ok, n, _msg = _prewarm_one(
            p,
            headless=headless,
            timeout_sec=timeout,
            urls=DEFAULT_PREWARM_URLS,
            log=out,
        )
        if ok:
            ok_n += 1
            total_cookies += n
        if i + 1 < len(targets):
            time.sleep(0.2)

    out(f"[*] 全局 CF cookie 预缓存完成：成功 {ok_n}/{len(targets)}，cookie 条目约 {total_cookies}")
    return {"enabled": True, "ok": ok_n, "total": len(targets), "cookies": total_cookies}


def inject_cf_cookies_to_page(
    page: Any,
    proxy: str | None = None,
    *,
    log: LogFn | None = None,
) -> int:
    """Inject cached CF cookies into a DrissionPage tab. Returns count set."""
    out = log or (lambda _m: None)
    if page is None:
        return 0
    cookies = get_cached_cf_cookies(proxy)
    # Do NOT fall back to another proxy's / direct cookies — CF clearance is
    # IP-bound; injecting direct cookies while browsing via proxy breaks signup.
    if not cookies:
        out(
            f"[*] 无预热 CF cookie（proxy={cache_key(proxy)[:48]}），将直接打开注册页"
        )
        return 0
    n = 0
    # Navigate to domain first so set.cookies sticks
    try:
        page.get("https://accounts.x.ai/")
        time.sleep(0.3)
    except Exception:
        pass
    for target in (page, getattr(page, "browser", None)):
        if target is None:
            continue
        try:
            target.set.cookies(cookies)  # type: ignore[attr-defined]
            n = len(cookies)
            out(f"[*] 已注入预热 CF cookie {n} 条（proxy={cache_key(proxy)[:40]}）")
            return n
        except Exception:
            continue
    # one-by-one
    for c in cookies:
        try:
            page.set.cookies(c)  # type: ignore[attr-defined]
            n += 1
        except Exception:
            try:
                br = getattr(page, "browser", None)
                if br is not None:
                    br.set.cookies(c)
                    n += 1
            except Exception:
                pass
    if n:
        out(f"[*] 已注入预热 CF cookie {n} 条（逐条）")
    return n
