"""Proxy configuration: single PROXY or PROXY_POOL with health checks.

Supports both env vars and config.json keys:

  # 方式一：单代理
  PROXY=http://user:pass@host:port

  # 方式二：代理池（多个网关，逗号或分号分隔；可旋转出口 IP）
  PROXY_POOL=http://u:p@gw1:port,http://u:p@gw2:port

Features inspired by production register stacks:
  - Startup probe: mark dead gateways with cooldown (default 60s)
  - Round-robin only among currently available proxies
  - report_proxy_success / report_proxy_failure for runtime feedback
  - Rotating residential gateways stay in pool; only tunnel-dead ones cool down

Also provides headless resolution and lightweight startup checks.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

_lock = threading.Lock()
_index = 0
_cached_list: list[str] | None = None
_states: dict[str, "ProxyState"] = {}
_tls = threading.local()

# Chrome / Chromium candidates (macOS + Linux)
BROWSER_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
)

# Lightweight HTTPS probe targets (first success wins).
# Any HTTP response (incl. 403 Cloudflare) means the CONNECT tunnel works.
DEFAULT_PROBE_URLS = (
    "https://api.ipify.org?format=json",
    "https://httpbin.org/ip",
    "https://1.1.1.1/cdn-cgi/trace",
)

LogFn = Callable[[str], None]


@dataclass
class ProxyState:
    url: str
    failures: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""
    last_exit_ip: str = ""
    last_ok_at: float = 0.0
    probe_ok: bool | None = None  # None=unprobed


def parse_proxy_list(value: str | None) -> list[str]:
    """Parse comma/semicolon-separated proxies; preserve order, drop empties/dupes."""
    raw = str(value or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,;]+", raw)
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        p = part.strip().strip('"').strip("'")
        if not p or p in seen:
            continue
        if "://" not in p and re.match(r"^[^/\s]+:\d+$", p):
            p = f"http://{p}"
        seen.add(p)
        out.append(p)
    return out


def resolve_proxy_list(config: dict[str, Any] | None = None) -> list[str]:
    """Resolve active proxy list. Proxy is **optional** — empty means direct."""
    cfg = config or {}

    pool = parse_proxy_list(os.environ.get("PROXY_POOL", ""))
    if pool:
        return pool

    single = str(os.environ.get("PROXY") or "").strip()
    if single:
        return parse_proxy_list(single)

    pool = parse_proxy_list(cfg.get("proxy_pool") or "")
    if pool:
        return pool

    single = str(cfg.get("proxy") or "").strip()
    if single:
        return parse_proxy_list(single)

    use_sys = False
    env_sys = _env_truthy("USE_SYSTEM_PROXY")
    if env_sys is not None:
        use_sys = env_sys
    elif cfg.get("use_system_proxy") is not None:
        use_sys = bool(cfg.get("use_system_proxy"))
    if use_sys:
        for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
            val = str(os.environ.get(key) or "").strip()
            if val:
                return parse_proxy_list(val)
    return []


def _ensure_states(proxies: list[str]) -> None:
    global _states
    with _lock:
        new_states: dict[str, ProxyState] = {}
        for p in proxies:
            new_states[p] = _states.get(p) or ProxyState(url=p)
        _states = new_states


def refresh_proxy_cache(config: dict[str, Any] | None = None) -> list[str]:
    """Re-read env/config into process cache. Call after load_config / CLI overrides."""
    global _cached_list, _index
    proxies = resolve_proxy_list(config)
    with _lock:
        _cached_list = list(proxies)
        _index = 0
    _ensure_states(proxies)
    return proxies


def get_proxy_list(config: dict[str, Any] | None = None) -> list[str]:
    """Return configured proxies.

    If *config* is passed, resolve from that config (does not require prior cache).
    If omitted, use process cache filled by refresh_proxy_cache().
    """
    global _cached_list
    if config is not None:
        return resolve_proxy_list(config)
    with _lock:
        if _cached_list is None:
            _cached_list = resolve_proxy_list(None)
            _ensure_states(_cached_list)
        return list(_cached_list)


def _cooldown_sec(config: dict[str, Any] | None = None) -> float:
    cfg = config or {}
    try:
        v = float(cfg.get("proxy_cooldown_sec", os.environ.get("PROXY_COOLDOWN_SEC", 60)) or 60)
    except Exception:
        v = 60.0
    return max(5.0, min(v, 3600.0))


def _fail_threshold(config: dict[str, Any] | None = None) -> int:
    cfg = config or {}
    try:
        v = int(cfg.get("proxy_fail_threshold", os.environ.get("PROXY_FAIL_THRESHOLD", 3)) or 3)
    except Exception:
        v = 3
    return max(1, min(v, 20))


def _probe_timeout(config: dict[str, Any] | None = None) -> float:
    cfg = config or {}
    try:
        v = float(cfg.get("proxy_probe_timeout_sec", os.environ.get("PROXY_PROBE_TIMEOUT_SEC", 5)) or 5)
    except Exception:
        v = 5.0
    return max(2.0, min(v, 30.0))


def _probe_enabled(config: dict[str, Any] | None = None) -> bool:
    env = _env_truthy("PROXY_PROBE")
    if env is not None:
        return env
    cfg = config or {}
    if "proxy_probe" in cfg and cfg.get("proxy_probe") is not None:
        val = cfg.get("proxy_probe")
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)
    return True  # default: probe when pool/proxy present


def _is_available(state: ProxyState, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if state.cooldown_until and now < state.cooldown_until:
        return False
    return True


def available_proxies(config: dict[str, Any] | None = None) -> list[str]:
    """Proxies not in cooldown (may still be unprobed)."""
    proxies = get_proxy_list(config)
    now = time.time()
    out: list[str] = []
    with _lock:
        for p in proxies:
            st = _states.get(p) or ProxyState(url=p)
            if _is_available(st, now):
                out.append(p)
    return out


def proxy_pool_stats(config: dict[str, Any] | None = None) -> dict[str, Any]:
    proxies = get_proxy_list(config)
    now = time.time()
    avail = 0
    cooling = 0
    with _lock:
        for p in proxies:
            st = _states.get(p) or ProxyState(url=p)
            if _is_available(st, now):
                avail += 1
            else:
                cooling += 1
    return {
        "total": len(proxies),
        "available": avail,
        "cooling": cooling,
    }


def next_proxy(config: dict[str, Any] | None = None) -> str:
    """Round-robin among available (non-cooldown) proxies. Empty if none."""
    global _index
    proxies = get_proxy_list(config)
    if not proxies:
        return ""
    now = time.time()
    with _lock:
        proxies = list(_cached_list or [])
        if not proxies:
            return ""
        n = len(proxies)
        for _ in range(n):
            proxy = proxies[_index % n]
            _index = (_index + 1) % n
            st = _states.get(proxy) or ProxyState(url=proxy)
            if _is_available(st, now):
                return proxy
        # All cooling: return next anyway (better than hard fail)
        proxy = proxies[_index % n]
        _index = (_index + 1) % n
        return proxy


def proxy_count(config: dict[str, Any] | None = None) -> int:
    return len(get_proxy_list(config))


def get_thread_proxy(*, rotate: bool = False, config: dict[str, Any] | None = None) -> str:
    """Sticky per-thread proxy; rotate=True picks the next available pool entry."""
    current = getattr(_tls, "proxy", None)
    if rotate or current is None:
        # If sticky proxy went into cooldown, re-pick
        if current and not rotate:
            with _lock:
                st = _states.get(current)
                if st is not None and not _is_available(st):
                    current = None
        if rotate or current is None:
            picked = next_proxy(config)
            _tls.proxy = picked
            return picked
    return current or ""


def clear_thread_proxy() -> None:
    _tls.proxy = None


def rotate_thread_proxy(config: dict[str, Any] | None = None) -> str:
    """Force next available pool entry for this thread."""
    return get_thread_proxy(rotate=True, config=config)


def report_proxy_success(proxy: str | None, *, exit_ip: str = "") -> None:
    """Mark proxy healthy after a successful outbound use."""
    p = (proxy or "").strip()
    if not p:
        return
    with _lock:
        st = _states.get(p) or ProxyState(url=p)
        st.failures = 0
        st.cooldown_until = 0.0
        st.last_error = ""
        st.probe_ok = True
        st.last_ok_at = time.time()
        if exit_ip:
            st.last_exit_ip = exit_ip
        _states[p] = st


def report_proxy_failure(
    proxy: str | None,
    error: str = "",
    *,
    config: dict[str, Any] | None = None,
) -> None:
    """Record failure; after threshold, put gateway into cooldown."""
    p = (proxy or "").strip()
    if not p:
        return
    thr = _fail_threshold(config)
    cool = _cooldown_sec(config)
    with _lock:
        st = _states.get(p) or ProxyState(url=p)
        st.failures = int(st.failures or 0) + 1
        st.last_error = (error or "")[:240]
        st.probe_ok = False
        if st.failures >= thr:
            st.cooldown_until = time.time() + cool
            st.failures = 0  # reset after cooling starts
        _states[p] = st


def _extract_exit_ip(body: str) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for k in ("ip", "origin", "query"):
                if data.get(k):
                    return str(data[k]).split(",")[0].strip()
    except Exception:
        pass
    # Cloudflare trace: ip=x.x.x.x
    m = re.search(r"(?m)^ip=([0-9a-fA-F\.:]+)\s*$", text)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", text)
    return m.group(1) if m else ""


def probe_proxy(
    proxy: str,
    *,
    timeout: float = 5.0,
    probe_urls: tuple[str, ...] | None = None,
) -> tuple[bool, str, str]:
    """Probe one proxy. Returns (ok, message, exit_ip).

    Success: any HTTP status (200/403 OK). Prefer system curl, then curl_cffi/requests.
    """
    import shutil
    import subprocess

    p = (proxy or "").strip()
    if not p:
        return False, "empty proxy", ""
    # One URL is enough for speed; multi-URL multiplies fail latency
    urls = probe_urls or (DEFAULT_PROBE_URLS[0],)
    last_err = ""
    proxies = {"http": p, "https": p}

    def _ok(status: int, body: str, backend: str = "") -> tuple[bool, str, str]:
        exit_ip = _extract_exit_ip(body)
        msg = f"可用 - HTTP {status}"
        if exit_ip:
            msg += f"，出口 {exit_ip}"
        if backend:
            msg += f" [{backend}]"
        return True, msg, exit_ip

    for url in urls:
        curl = shutil.which("curl")
        if curl:
            try:
                proc = subprocess.run(
                    [
                        curl,
                        "-x",
                        p,
                        "-m",
                        str(max(2, int(timeout))),
                        "-sS",
                        "-L",
                        "--max-redirs",
                        "2",
                        "-A",
                        "grok-register-proxy-probe/1.0",
                        "-w",
                        "\n__HTTP_CODE__=%{http_code}",
                        url,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout + 2,
                )
                blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
                m = re.search(r"__HTTP_CODE__=(\d+)", blob)
                code = int(m.group(1)) if m else 0
                body = blob.split("__HTTP_CODE__=")[0]
                if proc.returncode == 0 and code > 0:
                    return _ok(code, body, "curl")
                last_err = (proc.stderr or body or f"curl exit {proc.returncode}").strip()
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)

        try:
            from curl_cffi import requests as curl_requests

            r = curl_requests.get(
                url,
                proxies=proxies,
                timeout=timeout,
                headers={"User-Agent": "grok-register-proxy-probe/1.0", "Accept": "*/*"},
                verify=False,
            )
            return _ok(
                int(getattr(r, "status_code", 0) or 0),
                str(getattr(r, "text", "") or "")[:4096],
                "curl_cffi",
            )
        except Exception as exc:  # noqa: BLE001
            last_err = f"{exc} [curl_cffi]"

        try:
            import requests as std_requests

            r = std_requests.get(
                url,
                proxies=proxies,
                timeout=timeout,
                headers={"User-Agent": "grok-register-proxy-probe/1.0", "Accept": "*/*"},
            )
            return _ok(int(r.status_code), (r.text or "")[:4096], "requests")
        except Exception as exc:  # noqa: BLE001
            last_err = f"{exc} [requests]"

    err = re.sub(r"\s+", " ", last_err or "probe failed")[:200]
    if "400" in err and ("CONNECT" in err.upper() or "tunnel" in err.lower()):
        err += " | 网关拒绝 HTTPS CONNECT（换节点/查账号，或先 --headed 直连）"
    return False, f"不可用 - {err}", ""


def probe_proxy_pool(
    config: dict[str, Any] | None = None,
    *,
    log: LogFn | None = None,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Probe all proxies at startup; cool down dead ones. Returns stats + details."""
    out = log or (lambda m: print(m, flush=True))
    cfg = config or {}
    proxies = refresh_proxy_cache(cfg)
    if not proxies:
        return {"total": 0, "available": 0, "cooling": 0, "details": []}
    if not _probe_enabled(cfg):
        out(f"[*] 代理池: {len(proxies)} 个（探测已关闭 PROXY_PROBE=0）")
        return {**proxy_pool_stats(cfg), "details": [], "probed": False}

    timeout = _probe_timeout(cfg)
    thr = _fail_threshold(cfg)
    cool = _cooldown_sec(cfg)
    out(f"[*] 代理池: {len(proxies)} 个，启动探测 timeout={timeout}s fail_threshold={thr} cooldown={cool}s")

    details: list[dict[str, Any]] = []

    def _one(p: str) -> dict[str, Any]:
        ok, msg, exit_ip = probe_proxy(p, timeout=timeout)
        return {"proxy": p, "ok": ok, "msg": msg, "exit_ip": exit_ip}

    # Limited parallel probe (residential gateways often rate-limit concurrent CONNECT)
    results: list[dict[str, Any]] = []
    workers = max(1, min(3, int(max_workers), len(proxies)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, p): p for p in proxies}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                p = futs[fut]
                results.append({"proxy": p, "ok": False, "msg": f"不可用 - {exc}", "exit_ip": ""})

    # Apply results (order by original list)
    by_p = {r["proxy"]: r for r in results}
    for p in proxies:
        r = by_p.get(p) or {"proxy": p, "ok": False, "msg": "不可用 - no result", "exit_ip": ""}
        label = proxy_log_label(p)
        if r["ok"]:
            report_proxy_success(p, exit_ip=r.get("exit_ip") or "")
            out(f"[*] 代理 {label}: {r['msg']}")
        else:
            # Force cooldown immediately for startup probe failure
            with _lock:
                st = _states.get(p) or ProxyState(url=p)
                st.failures = thr
                st.last_error = r.get("msg") or ""
                st.probe_ok = False
                st.cooldown_until = time.time() + cool
                st.failures = 0
                _states[p] = st
            out(f"[!] 代理 {label}: {r['msg']}（冷却 {int(cool)}s）")
        details.append(r)

    stats = proxy_pool_stats(cfg)
    out(f"[*] 代理池可用: {stats['available']}/{stats['total']}" + (
        f"，冷却中 {stats['cooling']}" if stats["cooling"] else ""
    ))
    if stats["available"] == 0 and stats["total"] > 0:
        out("[!] 代理池当前无可用节点；将仍轮询（可能失败）。检查 PROXY_POOL 或稍后重试。")
    stats["details"] = details
    stats["probed"] = True
    return stats


def proxy_rotate_every_account(config: dict[str, Any] | None = None) -> bool:
    """Whether to rotate pool gateway on every account.

    Default False = thread-sticky gateway (exit IP may still rotate on residential).
    Env PROXY_ROTATE_EVERY_ACCOUNT=1 overrides config.
    """
    env = _env_truthy("PROXY_ROTATE_EVERY_ACCOUNT")
    if env is not None:
        return env
    cfg = config or {}
    val = cfg.get("proxy_rotate_every_account", False)
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    return bool(val)


def proxy_dict(proxy: str | None) -> dict[str, str]:
    p = (proxy or "").strip()
    if not p:
        return {}
    return {"http": p, "https": p}


def proxy_has_userinfo(proxy: str | None) -> bool:
    p = (proxy or "").strip()
    if not p or "://" not in p:
        if "@" in p and "://" not in p:
            return True
        return False
    try:
        u = urlparse(p)
        return bool(u.username)
    except Exception:
        return "@" in p.split("://", 1)[-1].split("/")[0]


def proxy_scheme(proxy: str | None) -> str:
    p = (proxy or "").strip()
    if not p:
        return ""
    try:
        u = urlparse(p if "://" in p else f"http://{p}")
        return (u.scheme or "http").lower()
    except Exception:
        return "http"


def proxy_for_chromium(proxy: str | None) -> str:
    """Chromium --proxy-server value.

    user:pass@host is forwarded via local_auth_proxy (127.0.0.1:port) because
    Chromium cannot embed credentials in --proxy-server.
    """
    p = (proxy or "").strip()
    if not p:
        return ""
    try:
        from grok_register.proxy.local_auth import chromium_proxy_server

        return chromium_proxy_server(p)
    except Exception:
        pass
    u = urlparse(p if "://" in p else f"http://{p}")
    host = u.hostname or ""
    if not host:
        return ""
    scheme = (u.scheme or "http").lower()
    if scheme in ("socks5h", "socks"):
        scheme = "socks5"
    if scheme not in ("http", "https", "socks5", "socks4"):
        scheme = "http"
    port = u.port
    if port is None:
        if scheme in ("https",):
            port = 443
        elif scheme.startswith("socks"):
            port = 1080
        else:
            port = 80
    return f"{scheme}://{host}:{port}"


def proxy_log_label(proxy: str | None) -> str:
    """Redact userinfo for logs."""
    p = (proxy or "").strip()
    if not p:
        return "(none)"
    try:
        u = urlparse(p if "://" in p else f"http://{p}")
        host = u.hostname or "?"
        port = u.port or ""
        auth = "user:***@" if u.username else ""
        return f"{u.scheme or 'http'}://{auth}{host}{(':' + str(port)) if port else ''}"
    except Exception:
        return "(proxy)"


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


def resolve_headless(config: dict[str, Any] | None = None) -> bool:
    """Whether user requested a non-interactive / background browser.

    Priority:
      1. env HEADLESS / BROWSER_HEADLESS
      2. config headless (if set)
      3. Linux without DISPLAY/WAYLAND → True (server)
      4. else False (macOS/Windows desktop)

    See also :func:`resolve_headless_mode` for pure vs offscreen implementation.
    """
    for key in ("HEADLESS", "BROWSER_HEADLESS"):
        t = _env_truthy(key)
        if t is not None:
            return t

    cfg = config or {}
    if "headless" in cfg and cfg.get("headless") is not None:
        val = cfg.get("headless")
        if isinstance(val, str):
            s = val.strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off", ""):
                return False
        return bool(val)

    if sys.platform.startswith("linux"):
        display = (os.environ.get("DISPLAY") or "").strip()
        wayland = (os.environ.get("WAYLAND_DISPLAY") or "").strip()
        if not display and not wayland:
            return True
    return False


def resolve_headless_mode(config: dict[str, Any] | None = None) -> str:
    """How to implement background Chrome: ``off`` | ``offscreen`` | ``pure``.

    - **off**: normal headed window
    - **offscreen**: real Chrome window moved off-screen (much better vs Cloudflare)
    - **pure**: ``--headless=new`` + stealth flags (hardest vs CF; needs proxy often)

    Config/env: ``headless_mode`` / ``HEADLESS_MODE`` =
    ``auto`` (default) | ``offscreen`` | ``pure`` | ``new``.
    """
    if not resolve_headless(config):
        return "off"
    cfg = config or {}
    raw = (
        os.environ.get("HEADLESS_MODE")
        or cfg.get("headless_mode")
        or "auto"
    )
    mode = str(raw).strip().lower()
    if mode in ("pure", "new", "chrome", "true-headless", "true"):
        return "pure"
    if mode in ("offscreen", "hidden", "virtual", "bg"):
        return "offscreen"
    # auto
    if sys.platform.startswith("linux"):
        display = (os.environ.get("DISPLAY") or "").strip()
        wayland = (os.environ.get("WAYLAND_DISPLAY") or "").strip()
        if not display and not wayland:
            return "pure"
    # Desktop: prefer offscreen — CF almost always blocks pure headless on accounts.x.ai
    return "offscreen"


def linux_server_chromium_flags() -> tuple[str, ...]:
    if not sys.platform.startswith("linux"):
        return ()
    return (
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
    )


def find_browser_path() -> str:
    for cand in BROWSER_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        for d in (os.environ.get("PATH") or "").split(os.pathsep):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return ""


def describe_proxy_mode(config: dict[str, Any] | None = None) -> str:
    proxies = get_proxy_list(config)
    if not proxies:
        return "proxy=off (direct, optional)"
    stats = proxy_pool_stats(config)
    rotate = proxy_rotate_every_account(config)
    sticky = "rotate-every-account" if rotate else "thread-sticky"
    if len(proxies) == 1:
        return f"proxy=single {proxy_log_label(proxies[0])} available={stats['available']}/1"
    return (
        f"proxy=pool size={len(proxies)} available={stats['available']}/{stats['total']} "
        f"mode={sticky} first={proxy_log_label(proxies[0])}"
    )


def collect_startup_warnings(
    config: dict[str, Any] | None = None,
    *,
    extension_path: str | None = None,
) -> list[str]:
    cfg = config or {}
    warnings: list[str] = []
    headless = resolve_headless(cfg)
    hmode = resolve_headless_mode(cfg)
    proxies = get_proxy_list(cfg)

    browser = find_browser_path()
    if not browser:
        warnings.append(
            "未找到 Chrome/Chromium 可执行文件；请安装后重试，或由 DrissionPage 使用默认路径"
        )

    if headless:
        if hmode == "pure":
            warnings.append(
                "headless_mode=pure（--headless=new）：accounts.x.ai 仍易被 CF 拦；"
                "建议默认 auto/offscreen，或加可用 PROXY_POOL；"
                "Linux 无显示器可: xvfb-run -a python register_cli.py --extra N --headed"
            )
        else:
            warnings.append(
                f"headless_mode={hmode}：使用离屏真实 Chrome（非纯无头），过 CF 成功率更高；"
                "仍失败请加代理或改 --headed"
            )
        if not (extension_path and os.path.isdir(extension_path)):
            warnings.append("未找到 turnstilePatch 目录，过 Turnstile 更难")

    for p in proxies:
        if proxy_has_userinfo(p):
            warnings.append(
                f"代理含账号密码 {proxy_log_label(p)}：已启用 local_auth_proxy "
                f"（127.0.0.1 转发 + Proxy-Authorization），Chromium 可走鉴权"
            )
            break

    stats = proxy_pool_stats(cfg)
    if proxies and stats["available"] == 0:
        warnings.append("代理池当前无可用节点（探测失败或全在冷却），注册可能大量失败")

    ep = str(cfg.get("email_provider") or "").strip()
    if not ep:
        warnings.append("config.email_provider 为空；请设置 tempmail_lol / yyds / cloudflare / duckmail")

    return warnings


def print_startup_report(
    config: dict[str, Any] | None = None,
    *,
    extension_path: str | None = None,
    log: LogFn | None = None,
    probe: bool | None = None,
) -> list[str]:
    """Print proxy/headless/browser summary + optional pool probe. Returns warnings."""
    out = log or (lambda m: print(m, flush=True))
    cfg = config or {}
    refresh_proxy_cache(cfg)
    browser = find_browser_path() or "(auto/default)"

    proxies = get_proxy_list(cfg)
    if proxies and (probe if probe is not None else _probe_enabled(cfg)):
        probe_proxy_pool(cfg, log=out)
    else:
        out(f"[*] {describe_proxy_mode(cfg)}")

    out(
        f"[*] headless={resolve_headless(cfg)} mode={resolve_headless_mode(cfg)} "
        f"browser={browser} platform={sys.platform} "
        f"DISPLAY={os.environ.get('DISPLAY', '')!r}"
    )
    ep = str(cfg.get("email_provider") or "?")
    out(f"[*] registration=browser email_provider={ep}")
    warns = collect_startup_warnings(cfg, extension_path=extension_path)
    for w in warns:
        out(f"[!] {w}")
    return warns
