#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
registerlib.core — 浏览器 DOM 流程 / 邮箱 / 配置实现。

批量入口请用: python register_cli.py
兼容: python -m grok_register.core cli
"""

import threading
import datetime
import time
import os
import sys
import gc
import queue
import secrets
import struct
import random
import re
import string
import json
import traceback

from grok_register.paths import PACKAGE_DIR, PROJECT_ROOT

for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def safe_print(*args, **kwargs):
    """Print without letting terminal encoding errors abort registration."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        text = sep.join(str(arg) for arg in args)
        stream = kwargs.get("file") or sys.stdout
        try:
            stream.write(text.encode("utf-8", "replace").decode("utf-8", "replace") + end)
            if kwargs.get("flush"):
                stream.flush()
        except Exception:
            # Last-resort ASCII fallback; logging must never break the flow.
            stream.write(text.encode("ascii", "replace").decode("ascii") + end)
            if kwargs.get("flush"):
                stream.flush()

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
from curl_cffi import requests as curl_requests
import requests as std_requests


CONFIG_FILE = str(PROJECT_ROOT / "config.json")
CRASH_LOG_FILE = str(PROJECT_ROOT / "logs" / "runtime_crash.log")


def write_crash_log(title, exc_type=None, exc_value=None, exc_tb=None):
    try:
        with open(CRASH_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"{datetime.datetime.now().isoformat()} {title}\n")
            if exc_type is not None:
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
    except Exception:
        pass


def _global_excepthook(exc_type, exc_value, exc_tb):
    write_crash_log("UNHANDLED", exc_type, exc_value, exc_tb)
    try:
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    except Exception:
        pass


sys.excepthook = _global_excepthook
if hasattr(threading, "excepthook"):
    def _thread_excepthook(args):
        write_crash_log(f"THREAD {getattr(args.thread, 'name', '')}", args.exc_type, args.exc_value, args.exc_traceback)
    threading.excepthook = _thread_excepthook
MEMORY_CLEANUP_INTERVAL = 5

DEFAULT_CONFIG = {
    "duckmail_api_key": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "none",
    "cloudflare_path_domains": "/api/domains",
    "cloudflare_path_accounts": "/api/new_address",
    "cloudflare_path_token": "/api/token",
    "cloudflare_path_messages": "/api/mails",
    # 代理（可选）：不填则直连。二选一也可用环境变量 PROXY / PROXY_POOL
    "proxy": "",
    "proxy_pool": "",
    # 是否继承 shell 的 https_proxy（默认否，避免误用本机 7890）
    "use_system_proxy": False,
    # 多网关池：默认线程粘性（同 worker 复用同一网关；住宅仍可旋转出口）
    "proxy_rotate_every_account": False,
    # 启动探测 + 失败冷却
    "proxy_probe": True,
    "proxy_probe_timeout_sec": 5,
    "proxy_fail_threshold": 3,
    "proxy_cooldown_sec": 60,
    # CF cookie 预热（无头 Chrome + 代理，借鉴号池预缓存）
    # 仅有可用代理时预热；直连默认关（无头预热 cookie 注入 headed 易触发 CF）
    "cf_prewarm": "auto",
    "cf_prewarm_max_proxies": 2,
    "cf_prewarm_timeout_sec": 15,
    "cf_prewarm_budget_sec": 35,
    "cf_prewarm_headless": True,
    # 无交互浏览器：Linux 无显示器默认开；macOS/Windows 默认关
    "headless": False,
    # auto=桌面 offscreen（离屏真窗口，易过 CF）| pure=--headless=new | offscreen 强制离屏
    "headless_mode": "auto",
    "enable_nsfw": True,
    "register_count": 1,
    "email_provider": "tempmail_lol",
    # empty/auto = use real Chrome UA (do NOT force Windows UA on macOS — breaks Turnstile)
    "user_agent": "",
    # auto = headed 不加载扩展（扩展易导致「验证失败」）；pure/offscreen 才加载
    "turnstile_extension": "auto",
    "grok2api_auto_add_local": True,
    "grok2api_local_token_file": "",
    "grok2api_pool_name": "ssoBasic",
    "grok2api_auto_add_remote": False,
    "grok2api_remote_base": "",
    "grok2api_remote_app_key": "",
    "yyds_preferred_domains": "",
    "yyds_blocked_domains": "",
    "yyds_domain_selection": "random",
    # TempMail.lol: single key and/or multi-key pool (comma-separated).
    # Env fallbacks: TEMPMAIL_LOL_API_KEY / TEMPMAIL_LOL_API_KEYS
    "tempmail_lol_api_key": "",
    "tempmail_lol_api_keys": "",
    "tempmail_lol_api_base": "https://api.tempmail.lol/v2",
    "tempmail_lol_domain": "",
    "tempmail_lol_prefix": "",
    "max_mail_retry": 3,
    "mail_pool_size": 3,
    "code_poll_timeout": 60,
    "code_poll_interval": 3,
    "register_threads": 1,
    "thread_start_interval": 0.8,
    "cpa_export_enabled": True,
    "api_reverse_tools": "",
    # Unified export tree: exports/ or exports/YYYYMMDD_HHMMSS/{cpa,sub2api,accounts.txt}
    "export_root": "./exports",
    "export_batch_enabled": True,
    "export_batch_parent": "./exports",
    "export_batch_also_global_accounts": True,
    "cpa_auth_dir": "./exports/cpa",
    "cpa_copy_to_hotload": False,
    "cpa_hotload_dir": "",
    "cpa_base_url": "https://cli-chat-proxy.grok.com/v1",
    "cpa_proxy": "",
    "cpa_headless": True,
    # When false + warm page available, mint reuses register Chromium (recommended).
    "cpa_force_standalone": True,
    # Prefer mint on register tab before recycle (avoids cold headless CF block).
    "cpa_mint_prefer_warm_browser": True,
    "cpa_mint_timeout_sec": 300,
    "cpa_mint_required": False,
    "cpa_probe_after_write": True,
    "cpa_probe_chat": False,
    "cpa_mint_cookie_inject": True,
    "cpa_mint_browser_reuse": True,
    "cpa_mint_browser_recycle_every": 15,
    "sub2api_export_enabled": True,
    "sub2api_export_dir": "./exports/sub2api",
    "sub2api_combined_file": "./exports/sub2api/sub2api-accounts.json",
    "cpa_cloud_upload_enabled": False,
    "cpa_cloud_api_base": "",
    "cpa_cloud_management_key": "",
    "cpa_cloud_upload_timeout": 30,
    "cpa_cloud_upload_retries": 3,
    # Online Sub2API (Wei-Shaw/sub2api ≥0.1.153): POST /api/v1/admin/accounts/data
    "sub2api_cloud_upload_enabled": False,
    "sub2api_cloud_api_base": "",
    "sub2api_cloud_admin_key": "",
    "sub2api_cloud_jwt": "",
    "sub2api_cloud_skip_default_group_bind": False,
    "sub2api_cloud_timeout": 60,
    "sub2api_cloud_retries": 3,
}


config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0
_yyds_runtime_blocked_domains = set()
_tempmail_lol_key_lock = threading.Lock()
_tempmail_lol_key_index = 0


class RegistrationCancelled(Exception):
    pass


class AccountRetryNeeded(Exception):
    pass


class EmailDomainRejected(Exception):
    pass


def split_config_list(value):
    return [x.strip().lower() for x in str(value or "").replace(";", ",").split(",") if x.strip()]


def email_domain(address):
    text = str(address or "").strip().lower()
    return text.rsplit("@", 1)[-1] if "@" in text else ""


def config_int(name, default, minimum=None, maximum=None):
    try:
        value = int(config.get(name, default) or default)
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def get_max_mail_retry():
    return config_int("max_mail_retry", 3, minimum=1, maximum=20)


def get_code_poll_timeout():
    base = config_int("code_poll_timeout", 60, minimum=15, maximum=300)
    # Fast batch: don't sit on empty inbox longer than needed
    if PERF_FLAGS.get("fast"):
        return max(15, min(base, 25))
    return base


def get_code_poll_interval():
    base = config_int("code_poll_interval", 3, minimum=1, maximum=30)
    # TempMail.lol hard limit: check inbox at most once every ~3–5s
    try:
        provider = get_email_provider()
    except Exception:
        provider = ""
    if provider == "tempmail_lol":
        return max(3, int(base or 3))
    if PERF_FLAGS.get("fast"):
        return min(base, 1)
    return base


def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            # utf-8-sig strips BOM written by some Windows/PowerShell editors
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("config.json root must be an object")
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception as exc:
            safe_print(f"[!] 加载 config.json 失败，使用默认配置: {exc}")
            config = DEFAULT_CONFIG.copy()
    # Refresh proxy pool cache from config + env (PROXY / PROXY_POOL)
    try:
        from grok_register.proxy.pool import refresh_proxy_cache

        refresh_proxy_cache(config)
    except Exception:
        pass
    return config


def save_config():
    try:
        # Write without BOM so subsequent loads stay portable
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        safe_print(f"保存配置失败: {e}")


def ensure_stable_python_runtime():
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        safe_print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        safe_print(
            "[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。"
        )


if __name__ == "__main__":
    ensure_stable_python_runtime()
    warn_runtime_compatibility()

load_config()

EXTENSION_PATH = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser", "extensions", "turnstilePatch")
)


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"


def get_proxies():
    """HTTP proxies for requests/curl_cffi. Uses sticky per-thread pool entry."""
    try:
        from grok_register.proxy.pool import get_thread_proxy, proxy_dict

        return proxy_dict(get_thread_proxy(config=config))
    except Exception:
        proxy = str(config.get("proxy") or "").strip()
        if proxy:
            return {"http": proxy, "https": proxy}
        return {}


def get_active_proxy() -> str:
    """Current thread's sticky proxy URL (may be empty)."""
    try:
        from grok_register.proxy.pool import get_thread_proxy

        return get_thread_proxy(config=config) or ""
    except Exception:
        return str(config.get("proxy") or "").strip()


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "none") or "none").lower()


def get_cloudflare_path(key, default_path):
    raw = str(config.get(key, default_path) or default_path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def cloudflare_build_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def cloudflare_apply_auth_params(params=None):
    merged = dict(params or {})
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key and mode == "query-key":
        merged["key"] = key
    return merged


def cloudflare_next_default_domain():
    """按配置轮换选择 Cloudflare 临时邮箱域名。"""
    global _cf_domain_index
    domains = [x.strip() for x in str(config.get("defaultDomains", "") or "").split(",") if x.strip()]
    if not domains:
        return ""
    domain = domains[_cf_domain_index % len(domains)]
    _cf_domain_index += 1
    return domain


def cloudflare_is_admin_create_path(path):
    """判断当前创建邮箱路径是否为 cloudflare_temp_email 管理员创建接口。"""
    return str(path or "").rstrip("/").lower() == "/admin/new_address"


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data.get("results")
        if isinstance(data.get("hydra:member"), list):
            return data.get("hydra:member")
        if isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data.get("messages"), list):
            return data.get("messages")
        if isinstance(data.get("data"), dict):
            nested = data.get("data")
            if isinstance(nested.get("messages"), list):
                return nested.get("messages")
    return []


def cloudflare_create_temp_address(api_base):
    """适配 cloudflare_temp_email 新建地址接口并兼容 admin 创建模式。"""
    path = get_cloudflare_path("cloudflare_path_accounts", "/api/new_address")
    url = f"{api_base}{path}"
    domain = cloudflare_next_default_domain()
    is_admin_create = cloudflare_is_admin_create_path(path)
    if is_admin_create:
        payload = {"name": generate_username(10), "enablePrefix": True}
        if domain:
            payload["domain"] = domain
        headers = cloudflare_build_headers(content_type=True)
    else:
        payload = {}
        if domain:
            payload["domain"] = domain
        headers = {"Content-Type": "application/json"}
    resp = http_post(url, json=payload, headers=headers)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare {path} 返回非JSON: {resp.text[:300]}")
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare {path} 缺少 address/jwt: {data}")
    return address, jwt


def get_user_agent() -> str:
    """Return custom UA, or empty to let real Chrome use its native UA.

    Forcing a Windows/old Chrome UA on macOS is a common Turnstile failure cause
    (platform/UA mismatch → widget shows 验证失败 / 故障排除).
    """
    raw = str(config.get("user_agent", "") or "").strip()
    if not raw or raw.lower() in ("auto", "default", "system", "native", "none"):
        return ""
    # Ignore obvious cross-platform spoof on desktop Chrome
    plat = sys.platform
    if plat == "darwin" and ("Windows NT" in raw or "Linux" in raw):
        return ""
    if plat.startswith("linux") and "Windows NT" in raw and "Linux" not in raw:
        return ""
    if plat.startswith("win") and ("Macintosh" in raw or "X11" in raw):
        return ""
    return raw


def _should_load_turnstile_extension(hmode: str) -> bool:
    """Headed: default off. Background modes: default on. Config overrides."""
    mode = str(config.get("turnstile_extension", "auto") or "auto").strip().lower()
    if mode in ("0", "false", "off", "no", "disable", "disabled"):
        return False
    if mode in ("1", "true", "on", "yes", "enable", "enabled"):
        return True
    # auto
    return hmode in ("pure", "offscreen")


def resolve_grok2api_local_token_file():
    configured = str(config.get("grok2api_local_token_file", "") or "").strip()
    if configured:
        return configured
    return str(PROJECT_ROOT / "accounts" / "token.json")


def _normalize_sso_token(raw_token):
    token = str(raw_token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def add_token_to_grok2api_local_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_file = resolve_grok2api_local_token_file()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip()
    if not pool_name:
        pool_name = "ssoBasic"
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    data = {}
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    pool = data.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token in existing:
        if log_callback:
            log_callback(f"[*] grok2api 本地池已存在 token: {pool_name}")
        return True
    entry = {"token": token, "tags": ["auto-register"], "note": email}
    pool.append(entry)
    data[pool_name] = pool
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 本地池: {pool_name} ({token_file})")
    return True


def get_grok2api_remote_api_bases(base):
    """生成 grok2api 管理 API 候选根路径。

    参数:
      - base str: 用户配置的 grok2api 远端地址

    返回:
      - list[str]: 依次尝试的管理 API 根路径
    """
    normalized = str(base or "").strip().rstrip("/")
    if not normalized:
        return []
    lower = normalized.lower()
    candidates = [normalized]
    if lower.endswith("/admin/api"):
        return candidates
    if lower.endswith("/admin"):
        candidates.append(f"{normalized}/api")
    else:
        candidates.append(f"{normalized}/admin/api")
    seen = set()
    unique = []
    for item in candidates:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def add_token_to_grok2api_remote_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    base = str(config.get("grok2api_remote_base", "") or "").strip().rstrip("/")
    app_key = str(config.get("grok2api_remote_app_key", "") or "").strip()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    if not base or not app_key:
        if log_callback:
            log_callback("[Debug] grok2api 远端未配置 base/app_key，跳过")
        return False
    headers = {"Content-Type": "application/json"}
    query = {"app_key": app_key}
    pool_map = {"ssoBasic": "basic", "ssoSuper": "super"}
    remote_pool = pool_map.get(pool_name, "basic")
    api_bases = get_grok2api_remote_api_bases(base)
    add_errors = []
    # 优先使用 add 接口，避免全量覆盖远端池
    add_payload = {"tokens": [token], "pool": remote_pool, "tags": ["auto-register"]}
    for api_base in api_bases:
        endpoint = f"{api_base}/tokens/add"
        try:
            resp_add = http_post(
                endpoint,
                headers=headers,
                params=query,
                json=add_payload,
                timeout=30,
                proxies={},
            )
            resp_add.raise_for_status()
            if log_callback:
                log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({endpoint})")
            return True
        except Exception as add_exc:
            add_errors.append(f"{endpoint}: {add_exc}")
    if log_callback:
        log_callback(f"[Debug] /tokens/add 写入失败，尝试 /tokens 全量模式: {'; '.join(add_errors)}")

    # 兜底：全量保存接口
    current = {}
    fallback_base = api_bases[0] if api_bases else base
    for api_base in api_bases or [base]:
        try:
            resp = http_get(f"{api_base}/tokens", headers=headers, params=query, timeout=20, proxies={})
            if resp.status_code == 200:
                payload = resp.json()
                current = payload.get("tokens", {}) if isinstance(payload, dict) else {}
                fallback_base = api_base
                break
        except Exception:
            continue
    if not isinstance(current, dict):
        current = {}
    pool = current.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token not in existing:
        pool.append({"token": token, "tags": ["auto-register"], "note": email})
    current[pool_name] = pool
    save_errors = []
    save_bases = []
    for item in [fallback_base, *(api_bases or [base])]:
        if item and item not in save_bases:
            save_bases.append(item)
    for api_base in save_bases:
        try:
            resp2 = http_post(f"{api_base}/tokens", headers=headers, params=query, json=current, timeout=30, proxies={})
            resp2.raise_for_status()
            if log_callback:
                log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({api_base}/tokens)")
            return True
        except Exception as save_exc:
            save_errors.append(f"{api_base}/tokens: {save_exc}")
    raise RuntimeError(f"grok2api 远端 /tokens 全量模式写入失败: {'; '.join(save_errors)}")


def add_token_to_grok2api_pools(raw_token, email="", log_callback=None):
    if config.get("grok2api_auto_add_local", True):
        try:
            add_token_to_grok2api_local_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 本地池失败: {exc}")
    if config.get("grok2api_auto_add_remote", False):
        try:
            add_token_to_grok2api_remote_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 远端池失败: {exc}")


def _add_chromium_args(options, flags) -> None:
    banned = {str(x).strip().lower() for x in CHROMIUM_BANNED_CLI_FLAGS}
    for flag in flags or ():
        f = str(flag or "").strip()
        if not f or f.lower() in banned:
            continue
        # Also block any blink AutomationControlled variant
        if "automationcontrolled" in f.lower():
            continue
        try:
            options.set_argument(f)
        except Exception:
            pass


def _scrub_banned_chromium_args(options) -> None:
    """Remove flags that trigger Chrome yellow banner / hurt CF (best-effort)."""
    try:
        args = list(getattr(options, "arguments", None) or [])
    except Exception:
        args = []
    if not args:
        return
    drop = []
    for arg in args:
        s = str(arg or "")
        low = s.lower()
        if "automationcontrolled" in low:
            drop.append(arg)
            continue
        for banned in CHROMIUM_BANNED_CLI_FLAGS:
            if low == banned.lower() or low.startswith(banned.lower() + "="):
                drop.append(arg)
                break
    for arg in drop:
        try:
            options.remove_argument(arg)
        except Exception:
            try:
                # Some versions only accept the flag key without value
                options.remove_argument(str(arg).split("=", 1)[0])
            except Exception:
                pass


def create_browser_options():
    """Build ChromiumOptions: headed stays near-stock Chrome; background adds slim flags.

    Critical: do **not** pass ``--disable-blink-features=AutomationControlled``.
    Modern Chrome shows a yellow unsupported-flag banner and Cloudflare/Turnstile
    success on headed drops sharply (same lesson as gpt/codex_team_auth).
    """
    from grok_register.proxy.pool import (
        BROWSER_CANDIDATES,
        describe_proxy_mode,
        find_browser_path,
        get_thread_proxy,
        linux_server_chromium_flags,
        proxy_for_chromium,
        proxy_has_userinfo,
        proxy_log_label,
        resolve_headless,
        resolve_headless_mode,
    )

    # read_file=False: ignore DrissionPage configs.ini / leftover user ini flags
    try:
        options = ChromiumOptions(read_file=False)
    except TypeError:
        options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)

    hmode = resolve_headless_mode(config)
    # Prefer real system Chrome for all modes
    browser_path = find_browser_path()
    if browser_path:
        try:
            options.set_browser_path(browser_path)
        except Exception:
            pass
    else:
        for cand in BROWSER_CANDIDATES:
            if os.path.isfile(cand):
                try:
                    options.set_browser_path(cand)
                except Exception:
                    pass
                break

    # Minimal flags for every mode
    _add_chromium_args(options, CHROMIUM_MINIMAL_FLAGS)

    # Only override UA when config provides a platform-compatible string.
    # Empty = native Chrome UA (best for Turnstile on real desktop).
    try:
        ua = get_user_agent()
        if ua:
            options.set_user_agent(ua)
    except Exception:
        pass

    try:
        options.set_pref("credentials_enable_service", False)
        options.set_pref("profile.password_manager_enabled", False)
    except Exception:
        pass

    if hmode == "pure":
        # Pure headless: slim + Linux server flags only (no banned CLI stealth)
        _add_chromium_args(options, CHROMIUM_SLIM_FLAGS)
        _add_chromium_args(options, linux_server_chromium_flags())
        _add_chromium_args(options, CHROMIUM_STEALTH_FLAGS)
        try:
            options.set_argument("--headless=new")
        except Exception:
            try:
                options.headless(True)
            except Exception:
                pass
        _add_chromium_args(options, ("--hide-scrollbars",))
    elif hmode == "offscreen":
        # Real Chrome window (not --headless). Keep flags light so CF can pass.
        try:
            options.headless(False)
        except Exception:
            pass
        _add_chromium_args(
            options,
            (
                "--window-position=40,40",
                "--window-size=1100,800",
                "--force-device-scale-factor=1",
            ),
        )
        # Linux servers still need no-sandbox etc. even with a virtual display
        _add_chromium_args(options, linux_server_chromium_flags())
    else:
        # Headed: near-stock Chrome — stable CF path. No forced --lang spoof.
        try:
            options.headless(False)
        except Exception:
            pass
        _add_chromium_args(options, ("--window-size=1440,1000",))

    # turnstilePatch: headed defaults OFF — extension all_frames patch often yields
    # Turnstile UI 「验证失败 / 故障排除」. Enable via turnstile_extension=on.
    if _should_load_turnstile_extension(hmode) and os.path.exists(EXTENSION_PATH):
        try:
            options.add_extension(EXTENSION_PATH)
        except Exception:
            pass

    proxy = get_thread_proxy(config=config)
    chrome_proxy = proxy_for_chromium(proxy)
    if chrome_proxy:
        if proxy and proxy_has_userinfo(proxy):
            try:
                if not getattr(_page_ctx, "_auth_proxy_warned", False):
                    safe_print(
                        f"[*] 代理含账号密码 {proxy_log_label(proxy)} → "
                        f"本地鉴权转发 {chrome_proxy}（Chromium 可用）"
                    )
                    _page_ctx._auth_proxy_warned = True
            except Exception:
                pass
        try:
            options.set_proxy(chrome_proxy)
        except Exception:
            try:
                options.set_argument(f"--proxy-server={chrome_proxy}")
            except Exception:
                pass
    # Final scrub: never ship AutomationControlled / invalid chrome-driver-only flags
    _scrub_banned_chromium_args(options)
    try:
        options._grok_proxy_label = proxy_log_label(proxy)  # type: ignore[attr-defined]
        options._grok_headless = resolve_headless(config)  # type: ignore[attr-defined]
        options._grok_headless_mode = hmode  # type: ignore[attr-defined]
        options._grok_proxy_mode = describe_proxy_mode(config)  # type: ignore[attr-defined]
    except Exception:
        pass
    return options


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def _is_tls_backend_error(exc):
    err = str(exc).lower()
    return (
        "tls connect error" in err
        or "openssl_internal:invalid library" in err
        or "curl: (35)" in err
        or "invalid library (0)" in err
    )


def _to_std_request_kwargs(kwargs):
    std_kwargs = _build_request_kwargs(**kwargs)
    # curl_cffi accepts extra options that requests does not.
    for key in ("impersonate", "default_headers", "http_version"):
        std_kwargs.pop(key, None)
    return std_kwargs


def http_get(url, **kwargs):
    try:
        return curl_requests.get(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if _is_tls_backend_error(exc):
            return std_requests.get(url, **_to_std_request_kwargs(kwargs))
        # 代理不可用时自动回退为直连，避免整个流程直接失败
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            try:
                return curl_requests.get(url, **_build_request_kwargs(**retry_kwargs))
            except Exception as retry_exc:
                if _is_tls_backend_error(retry_exc):
                    return std_requests.get(url, **_to_std_request_kwargs(retry_kwargs))
                raise
        raise


def http_post(url, **kwargs):
    try:
        return curl_requests.post(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if _is_tls_backend_error(exc):
            return std_requests.post(url, **_to_std_request_kwargs(kwargs))
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            try:
                return curl_requests.post(url, **_build_request_kwargs(**retry_kwargs))
            except Exception as retry_exc:
                if _is_tls_backend_error(retry_exc):
                    return std_requests.post(url, **_to_std_request_kwargs(retry_kwargs))
                raise
        raise


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("鐢ㄦ埛鍋滄娉ㄥ唽")


def sleep_with_cancel(seconds, cancel_callback=None, *, scale: bool = True):
    """Interruptible sleep. Applies PERF_FLAGS['sleep_scale'] when scale=True (fast mode)."""
    s = max(float(seconds or 0), 0.0)
    if scale:
        try:
            s *= float(PERF_FLAGS.get("sleep_scale", 1.0) or 1.0)
        except Exception:
            pass
        # Keep a tiny floor so UI/DOM can settle under aggressive scale
        if 0 < s < 0.05:
            s = 0.05
    deadline = time.time() + s
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def get_domains(api_key=None):
    headers = {}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def create_account(address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = http_post(f"{DUCKMAIL_API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_token(address, password):
    data = {"address": address, "password": password}
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json=data)
    resp.raise_for_status()
    return resp.json().get("token")


def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_domains(api_base, api_key=None):
    headers = cloudflare_build_headers(content_type=False)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_domains", "/domains")
    params = cloudflare_apply_auth_params()
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return _pick_list_payload(resp.json())


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_cloudflare_path("cloudflare_path_accounts", "/accounts")
    params = cloudflare_apply_auth_params()
    resp = http_post(f"{api_base}{path}", json=payload, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_token(api_base, address, password, api_key=None):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_token", "/token")
    resp = http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=headers,
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data.get("token")
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"].get("token")
    return None


def cloudflare_get_messages(api_base, token):
    headers = {"Authorization": f"Bearer {token}"}
    path = get_cloudflare_path("cloudflare_path_messages", "/messages")
    params = {"limit": 20, "offset": 0}
    params = cloudflare_apply_auth_params(params)
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")
    return _pick_list_payload(data)


def cloudflare_get_message_detail(api_base, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_cloudflare_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(
                url,
                headers=headers,
                params=cloudflare_apply_auth_params(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
            continue
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")


YYDS_API_BASE = "https://maliapi.215.im/v1"
TEMPMAIL_LOL_DEFAULT_API_BASE = "https://api.tempmail.lol/v2"


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鍒涘缓閭澶辫触: {data}")


def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(
        f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 鑾峰彇token澶辫触: {data}")


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(
        f"{YYDS_API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鑾峰彇閭欢璇︽儏澶辫触: {data}")


def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def yyds_pick_domain(api_key=None, jwt=None):
    global _yyds_runtime_blocked_domains
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    blocked = set(split_config_list(config.get("yyds_blocked_domains", "")))
    blocked.update(_yyds_runtime_blocked_domains)
    verified = [
        d for d in domains
        if d.get("isVerified") and str(d.get("domain", "")).strip().lower() not in blocked
    ]
    preferred = split_config_list(config.get("yyds_preferred_domains", ""))
    if preferred:
        domain_map = {str(d.get("domain", "")).strip().lower(): d for d in verified}
        for name in preferred:
            if name in domain_map:
                return domain_map[name]["domain"]
    private = [d for d in verified if not d.get("isPublic")]
    if private:
        random.shuffle(private)
        return private[0]["domain"]
    public = [d for d in verified if d.get("isPublic")]
    if public:
        if str(config.get("yyds_domain_selection", "random")).lower() == "random":
            random.shuffle(public)
        return public[0]["domain"]
    if verified:
        return verified[0]["domain"]
    raise Exception(f"YYDS 鏃犲凡楠岃瘉鍩熷悕鍙敤，已排除: {', '.join(sorted(blocked)) or 'none'}")


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        address=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("鑾峰彇 YYDS token 澶辫触")
    safe_print(f"[*] 宸插垱寤?YYDS 閭: {address}")
    return address, temp_token


def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    last_wait_log = 0
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        now = time.time()
        if log_callback and now - last_wait_log >= 15:
            left = max(0, int(deadline - now))
            log_callback(f"[Debug] YYDS 等待验证码中，剩余 {left}s")
            last_wait_log = now
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue
            try:
                detail = yyds_get_message_detail(msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] YYDS 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


def _split_api_keys(value):
    """Parse comma/semicolon/whitespace separated API keys, preserving order & uniqueness."""
    raw = str(value or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,;\s]+", raw)
    seen = set()
    keys = []
    for part in parts:
        key = part.strip().strip('"').strip("'")
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def get_tempmail_lol_api_base():
    base = (
        str(config.get("tempmail_lol_api_base") or "").strip()
        or os.environ.get("TEMPMAIL_LOL_API_BASE", "").strip()
        or TEMPMAIL_LOL_DEFAULT_API_BASE
    )
    return base.rstrip("/")


def get_tempmail_lol_api_keys():
    """
    Collect TempMail.lol API keys for the key pool.

    Priority:
      1. config tempmail_lol_api_keys (multi, comma-separated)
      2. config tempmail_lol_api_key (single)
      3. env TEMPMAIL_LOL_API_KEYS
      4. env TEMPMAIL_LOL_API_KEY

    Empty list means free tier (no Authorization header).
    """
    keys = _split_api_keys(config.get("tempmail_lol_api_keys", ""))
    if not keys:
        single = str(config.get("tempmail_lol_api_key") or "").strip()
        if single:
            keys = _split_api_keys(single)
    if not keys:
        keys = _split_api_keys(os.environ.get("TEMPMAIL_LOL_API_KEYS", ""))
    if not keys:
        env_single = str(os.environ.get("TEMPMAIL_LOL_API_KEY") or "").strip()
        if env_single:
            keys = _split_api_keys(env_single)
    return keys


def next_tempmail_lol_api_key():
    """Round-robin pick from the key pool. Returns "" for free tier."""
    global _tempmail_lol_key_index
    keys = get_tempmail_lol_api_keys()
    if not keys:
        return ""
    with _tempmail_lol_key_lock:
        key = keys[_tempmail_lol_key_index % len(keys)]
        _tempmail_lol_key_index = (_tempmail_lol_key_index + 1) % len(keys)
        return key


def tempmail_lol_build_headers(api_key=None, content_type=False):
    headers = {
        "Accept": "application/json",
        "User-Agent": "GrokRegister/TempMail.lol",
    }
    if content_type:
        headers["Content-Type"] = "application/json"
    key = (api_key if api_key is not None else next_tempmail_lol_api_key()) or ""
    key = str(key).strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def tempmail_lol_create_inbox(api_key=None, domain=None, prefix=None):
    """
    Create a TempMail.lol inbox via POST /v2/inbox/create.
    Returns (address, token). Token is the inbox access token used to poll mail.
    """
    api_base = get_tempmail_lol_api_base()
    key = api_key if api_key is not None else next_tempmail_lol_api_key()
    domain = (domain if domain is not None else str(config.get("tempmail_lol_domain") or "")).strip() or None
    prefix = (prefix if prefix is not None else str(config.get("tempmail_lol_prefix") or "")).strip() or None
    payload = {"domain": domain, "prefix": prefix}
    headers = tempmail_lol_build_headers(api_key=key, content_type=True)
    resp = http_post(f"{api_base}/inbox/create", json=payload, headers=headers)
    if resp.status_code == 429:
        raise Exception(f"TempMail.lol 触发限流: {resp.text}")
    if resp.status_code >= 400:
        raise Exception(f"TempMail.lol 创建邮箱失败 HTTP {resp.status_code}: {resp.text}")
    data = resp.json() if resp.text else {}
    address = str(data.get("address") or "").strip()
    token = str(data.get("token") or "").strip()
    if not address or not token:
        raise Exception(f"TempMail.lol 创建邮箱返回异常: {data}")
    key_hint = (str(key)[:6] + "...") if key else "free"
    safe_print(f"[*] 已创建 TempMail.lol 邮箱: {address} (key={key_hint})")
    return address, token


def tempmail_lol_get_emails(inbox_token, api_key=None):
    """Fetch emails for an inbox token via GET /v2/inbox?token=..."""
    api_base = get_tempmail_lol_api_base()
    token = str(inbox_token or "").strip()
    if not token:
        raise Exception("TempMail.lol inbox token 为空")
    headers = tempmail_lol_build_headers(api_key=api_key if api_key is not None else "", content_type=False)
    # Prefer not rotating keys on poll; free token-only access is enough for inbox fetch.
    # If a key is provided, attach it for higher rate limits.
    if api_key is None:
        # Use first configured key (stable) if any, else free tier.
        keys = get_tempmail_lol_api_keys()
        if keys:
            headers = tempmail_lol_build_headers(api_key=keys[0], content_type=False)
    resp = http_get(f"{api_base}/inbox", params={"token": token}, headers=headers)
    if resp.status_code == 429:
        raise Exception(f"TempMail.lol 拉取邮件限流: {resp.text}")
    if resp.status_code >= 400:
        raise Exception(f"TempMail.lol 拉取邮件失败 HTTP {resp.status_code}: {resp.text}")
    data = resp.json() if resp.text else {}
    if data.get("expired") is True:
        raise Exception("TempMail.lol inbox token 已过期")
    emails = data.get("emails")
    if emails is None:
        return []
    if not isinstance(emails, list):
        return []
    return emails


def tempmail_lol_get_email_and_token(api_key=None):
    return tempmail_lol_create_inbox(api_key=api_key)


def tempmail_lol_get_oai_code(
    inbox_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    last_wait_log = 0
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        now = time.time()
        if log_callback and now - last_wait_log >= 15:
            left = max(0, int(deadline - now))
            log_callback(f"[Debug] TempMail.lol 等待验证码中，剩余 {left}s")
            last_wait_log = now
        if resend_callback and now >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = tempmail_lol_get_emails(inbox_token)
        except Exception as exc:
            err = str(exc)
            if log_callback:
                log_callback(f"[Debug] TempMail.lol 拉取邮件失败: {exc}")
            # 429 rate limit: wait longer before next check
            backoff = max(float(poll_interval), 4.0)
            if "限流" in err or "429" in err or "Rate Limited" in err:
                backoff = max(backoff, 5.0)
            sleep_with_cancel(backoff, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] TempMail.lol 本轮邮件数量: {len(messages)}")
        for msg in messages:
            # TempMail.lol emails have no stable id; fingerprint content.
            msg_id = (
                msg.get("id")
                or msg.get("date")
                or f"{msg.get('from','')}|{msg.get('subject','')}|{msg.get('body','')[:80]}"
            )
            msg_id = str(msg_id)
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addr = str(msg.get("to") or msg.get("recipient") or "").strip().lower()
            if to_addr and email and to_addr != str(email).strip().lower():
                # still parse if mismatch fields drift; only skip when clearly different
                if log_callback:
                    log_callback(f"[Debug] TempMail.lol 邮件收件人不匹配: {to_addr} != {email}")
                # continue parsing anyway — some payloads may use different casing/format
            parts = []
            body = msg.get("body") or msg.get("text") or ""
            if isinstance(body, str) and body.strip():
                parts.append(body)
            html = msg.get("html")
            if isinstance(html, str) and html.strip():
                parts.append(re.sub(r"<[^>]+>", " ", html))
            elif isinstance(html, list):
                for h in html:
                    if isinstance(h, str) and h.strip():
                        parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject") or "")
            combined = "\n".join(parts)
            if log_callback:
                log_callback(f"[Debug] TempMail.lol 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] TempMail.lol 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(max(float(poll_interval), 3.0), cancel_callback)
    raise Exception(f"TempMail.lol 在 {timeout}s 内未收到验证码邮件")


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 鏃犲凡楠岃瘉鍩熷悕鍙敤")


def get_email_provider():
    raw = str(config.get("email_provider", "duckmail") or "duckmail").strip().lower()
    aliases = {
        "tempmail": "tempmail_lol",
        "tempmail.lol": "tempmail_lol",
        "tempmaillol": "tempmail_lol",
        "temp_mail_lol": "tempmail_lol",
        "tmlol": "tempmail_lol",
    }
    return aliases.get(raw, raw)


def _get_email_and_token_direct(api_key=None):
    """Always hit provider API (used by mail pool producer + fallback)."""
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider in ("tempmail_lol",):
        return tempmail_lol_get_email_and_token(api_key=api_key)
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            # 兜底回退到 Mail.tm 风格
            key = api_key or get_cloudflare_api_key()
            domains = cloudflare_get_domains(api_base, api_key=key)
            if not domains:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
            verified = [d for d in domains if d.get("isVerified")]
            target = verified[0] if verified else domains[0]
            domain = target.get("domain")
            if not domain:
                raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
            username = generate_username(10)
            address = f"{username}@{domain}"
            password = secrets.token_urlsafe(12)
            cloudflare_create_account(
                api_base, address, password, api_key=key, expires_in=0
            )
            token = cloudflare_get_token(api_base, address, password, api_key=key)
            if not token:
                raise Exception("获取 Cloudflare 邮箱 token 失败")
            return address, token
    key = api_key or get_duckmail_api_key()
    domain = pick_domain(api_key=key)
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    create_account(address, password, api_key=key, expires_in=0)
    token = get_token(address, password)
    if not token:
        raise Exception("获取 DuckMail token 失败")
    return address, token


def get_email_and_token(api_key=None):
    """Prefer pre-created mailbox from pool; fall back to direct API create."""
    if api_key is None:
        try:
            from grok_register.mail.pool import acquire_mailbox, get_mail_pool

            if get_mail_pool() is not None:
                # Short wait: pool should already have items; don't block long
                hit = acquire_mailbox(timeout=0.05)
                if hit and hit[0] and hit[1]:
                    return hit
                # Brief wait if producer is mid-create
                hit = acquire_mailbox(timeout=2.5)
                if hit and hit[0] and hit[1]:
                    return hit
        except Exception:
            pass
    return _get_email_and_token_direct(api_key=api_key)


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "tempmail_lol":
        return tempmail_lol_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def extract_verification_code(text, subject=""):
    if subject:
        match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if email.lower() not in recipients:
                continue
            try:
                detail = get_message_detail(dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"在 {timeout}s 内未收到验证码邮件")


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    api_base = get_cloudflare_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    # 同一封邮件正文可能延迟可读，允许多次重试解析，避免偶发漏码
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = cloudflare_get_messages(api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")

        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            msg_addr = str(msg.get("address", "")).lower()
            # 优先匹配目标邮箱；若结构不一致也允许继续解析，避免接口字段漂移导致漏码
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if not address_matched and log_callback:
                log_callback(f"[Debug] 跳过疑似非目标邮件 id={msg_id} address={msg_addr} to={recipients}")
                continue
            parts = []
            # 先直接从列表项取内容，避免 detail 接口差异导致漏码
            for field in ("text", "raw", "content", "intro", "body", "snippet"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            # 再尝试 detail 接口补全内容
            try:
                detail = cloudflare_get_message_detail(api_base, dev_token, msg_id)
                for field in ("text", "raw", "content", "intro", "body", "snippet"):
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list2 = detail.get("html") or []
                if isinstance(html_list2, str):
                    html_list2 = [html_list2]
                for h in html_list2:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", h)
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare detail接口失败，改用列表内容解析: {exc}")
            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def response_preview(res, limit=200):
    try:
        text = str(res.text or "")
    except Exception:
        text = ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def is_cloudflare_block_response(res):
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(res.headers).items()}
        text = str(res.text or "").lower()
        server = headers.get("server", "")
        content_type = headers.get("content-type", "")
        return (
            res.status_code in (403, 429, 503)
            and (
                "cloudflare" in server
                or "cloudflare" in text
                or "cf-error" in text
                or "__cf_chl" in text
                or "text/html" in content_type
            )
        )
    except Exception:
        return False


def set_birth_date(session, log_callback=None):
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] set_birth_date status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_birth_date 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_birth_date HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False, f"set_birth_date 异常: {e}"


def set_tos_accepted(session, log_callback=None):
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_tos_accepted 被 accounts.x.ai 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_tos_accepted HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False, f"set_tos_accepted 异常: {e}"


def encode_grpc_nsfw_settings():
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] update_nsfw status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "update_nsfw_settings 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"update_nsfw_settings HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False, f"update_nsfw_settings 异常: {e}"


def _cookie_pairs_from_browser(page=None, cookies=None, token="", cf_clearance=""):
    """Build cookie name→value map from browser export + SSO token."""
    pairs = {}
    if token:
        pairs["sso"] = str(token).strip()
        pairs["sso-rw"] = str(token).strip()
    if cf_clearance:
        pairs["cf_clearance"] = str(cf_clearance).strip()
    if isinstance(cookies, (list, tuple)):
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name = c.get("name") or c.get("Name")
            value = c.get("value") or c.get("Value")
            if name and value is not None:
                pairs[str(name)] = str(value)
    if page is not None:
        for getter in (
            lambda: page.cookies(all_domains=True, all_info=True),
            lambda: page.cookies(all_domains=True),
            lambda: page.cookies(),
        ):
            try:
                raw = getter()
            except TypeError:
                continue
            except Exception:
                continue
            if not raw:
                continue
            if isinstance(raw, list):
                for c in raw:
                    if isinstance(c, dict) and c.get("name") and c.get("value") is not None:
                        pairs[str(c["name"])] = str(c["value"])
            break
        # also try browser jar
        try:
            br = getattr(page, "browser", None)
            if br is not None:
                raw = br.cookies()
                if isinstance(raw, list):
                    for c in raw:
                        if isinstance(c, dict) and c.get("name") and c.get("value") is not None:
                            pairs[str(c["name"])] = str(c["value"])
        except Exception:
            pass
    return pairs


def _enable_nsfw_via_browser(page, token, log_callback=None):
    """Use warm Chromium session (has CF cookies) to call NSFW RPCs via fetch."""
    log = log_callback or (lambda _m: None)
    if page is None:
        return False, "no page"
    token = str(token or "").strip()
    if not token:
        return False, "empty sso"
    try:
        # Ensure SSO cookies on relevant hosts
        for domain in (".x.ai", "accounts.x.ai", ".grok.com", "grok.com"):
            for name in ("sso", "sso-rw"):
                try:
                    page.set.cookies(
                        {
                            "name": name,
                            "value": token,
                            "domain": domain,
                            "path": "/",
                            "secure": True,
                        }
                    )
                except Exception:
                    try:
                        br = getattr(page, "browser", None)
                        if br is not None:
                            br.set.cookies(
                                {
                                    "name": name,
                                    "value": token,
                                    "domain": domain,
                                    "path": "/",
                                    "secure": True,
                                }
                            )
                    except Exception:
                        pass
        page.get("https://grok.com/")
        time.sleep(1.2)
        # Binary body as base64 for fetch
        import base64

        body_b64 = base64.b64encode(encode_grpc_nsfw_settings()).decode("ascii")
        js = """
const b64 = arguments[0];
const bin = atob(b64);
const bytes = new Uint8Array(bin.length);
for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
return fetch('https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls', {
  method: 'POST',
  credentials: 'include',
  headers: {
    'content-type': 'application/grpc-web+proto',
    'x-grpc-web': '1',
    'origin': 'https://grok.com',
    'referer': 'https://grok.com/'
  },
  body: bytes
}).then(async (r) => {
  let t = '';
  try { t = await r.text(); } catch (e) {}
  return { status: r.status, ok: r.ok, text: (t || '').slice(0, 200) };
}).catch((e) => ({ status: 0, ok: false, text: String(e) }));
"""
        result = page.run_js(js, body_b64)
        # DrissionPage may return promise already resolved
        if hasattr(result, "get") is False and result is not None:
            # wait a bit and re-query if needed
            time.sleep(0.5)
        status = 0
        ok = False
        text = ""
        if isinstance(result, dict):
            status = int(result.get("status") or 0)
            ok = bool(result.get("ok"))
            text = str(result.get("text") or "")
        log(f"[Debug] browser update_nsfw status={status} ok={ok} body={text[:120]}")
        if ok or 200 <= status < 300:
            return True, "browser session NSFW ok"
        return False, f"browser NSFW HTTP {status}: {text[:120]}"
    except Exception as exc:
        return False, f"browser NSFW: {exc}"


def enable_nsfw_for_token(
    token,
    cf_clearance="",
    log_callback=None,
    page=None,
    cookies=None,
):
    """Enable NSFW after register.

    Prefer warm browser (CF cookies already present). HTTP path soft-skips
    ToS/birth-date CF blocks and still attempts the NSFW feature flag call.
    Account is kept either way by callers.
    """
    log = log_callback or (lambda _m: None)
    token = str(token or "").strip()
    if not token:
        return False, "empty sso"

    # 1) Warm browser path
    if page is not None:
        ok, msg = _enable_nsfw_via_browser(page, token, log_callback=log)
        if ok:
            return True, msg
        log(f"[Debug] 热浏览器 NSFW 未成功: {msg}，改试 HTTP")

    # 2) HTTP with full cookie jar from register session
    pairs = _cookie_pairs_from_browser(
        page=page, cookies=cookies, token=token, cf_clearance=cf_clearance
    )
    cookie_header = "; ".join(f"{k}={v}" for k, v in pairs.items() if k and v)
    proxies = get_proxies()
    user_agent = get_user_agent()
    try:
        with curl_requests.Session(impersonate="chrome120", proxies=proxies) as session:
            session.headers.update(
                {
                    "user-agent": user_agent,
                    "cookie": cookie_header,
                    "accept": "*/*",
                    "accept-language": "en-US,en;q=0.9",
                }
            )
            # Load domains into jar when possible (helps host-scoped cookies)
            for name, value in pairs.items():
                try:
                    session.cookies.set(name, value, domain=".x.ai")
                    session.cookies.set(name, value, domain=".grok.com")
                except Exception:
                    pass

            ok, message = set_tos_accepted(session, log_callback)
            if not ok:
                # CF 403 on accounts ToS is common cold HTTP; not fatal for NSFW flag
                log(f"[!] ToS 步骤跳过: {message}")
            ok, message = set_birth_date(session, log_callback)
            if not ok:
                log(f"[!] 生日步骤跳过: {message}")
            ok, message = update_nsfw_settings(session, log_callback)
            if not ok:
                return False, message
            return True, "成功开启 NSFW（HTTP）"
    except Exception as e:
        return False, f"异常: {str(e)}"


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

browser = None
page = None

# ── Page context (thread-local; multi-thread CLI via TabPool) ──
_page_ctx = threading.local()

from grok_register.browser.tab_pool import TabPool  # re-export for register_cli

# Safe on all modes — does not trigger Chrome's yellow "unsupported flag" banner.
CHROMIUM_MINIMAL_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--mute-audio",
)

# Only for pure headless / Linux servers (not headed — heavy flags hurt CF / Turnstile).
CHROMIUM_SLIM_FLAGS = (
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-dev-shm-usage",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-sync",
    "--disable-translate",
    "--metrics-recording-only",
)

# Background-only extras. NEVER put AutomationControlled here:
# Chrome shows「不受支持的命令行标记」and CF success collapses on headed.
CHROMIUM_STEALTH_FLAGS = (
    "--lang=en-US,en",
    "--window-size=1920,1080",
)

# Explicitly banned CLI args (historical mistakes / ChromeDriver-only options).
CHROMIUM_BANNED_CLI_FLAGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--excludeSwitches=enable-automation",
    "--exclude-switches=enable-automation",
    "--useAutomationExtension=false",
)

PERF_FLAGS = {
    "fast": False,
    "sleep_scale": 1.0,
    "skip_debug_io": False,
    "cookie_snapshot": True,
    "async_side_effects": False,
    "browser_reuse": True,
    "browser_recycle_every": 25,
}

_used_accounts_lock = threading.Lock()
_used_accounts: list[dict] = []
_error_accounts: list[dict] = []


def get_page():
    """Prefer thread-local page; fall back to module global (singleton fallback)."""
    p = getattr(_page_ctx, "page", None)
    if p is not None:
        return p
    return page


def get_browser_obj():
    b = getattr(_page_ctx, "browser", None)
    if b is not None:
        return b
    return browser


def set_page_context(browser_obj, page_obj):
    """Bind page for current thread and mirror to module globals (compat)."""
    global browser, page
    _page_ctx.browser = browser_obj
    _page_ctx.page = page_obj
    browser = browser_obj
    page = page_obj


def clear_page_context():
    global browser, page
    _page_ctx.browser = None
    _page_ctx.page = None
    # Do not wipe other threads' globals blindly in multi-thread; only clear if
    # this thread owned the global (best-effort single-thread).
    if getattr(TabPool, "_options_factory", None) is None:
        browser = None
        page = None


def configure_perf(
    *,
    fast: bool = False,
    sleep_scale: float = 1.0,
    skip_debug_io: bool = False,
    cookie_snapshot: bool = True,
    async_side_effects: bool = False,
    browser_reuse: bool = True,
    browser_recycle_every: int = 25,
):
    PERF_FLAGS["fast"] = bool(fast)
    PERF_FLAGS["sleep_scale"] = float(sleep_scale)
    PERF_FLAGS["skip_debug_io"] = bool(skip_debug_io)
    PERF_FLAGS["cookie_snapshot"] = bool(cookie_snapshot)
    PERF_FLAGS["async_side_effects"] = bool(async_side_effects)
    PERF_FLAGS["browser_reuse"] = bool(browser_reuse)
    PERF_FLAGS["browser_recycle_every"] = max(1, int(browser_recycle_every))


def _get_page():
    return get_page()


def mark_used(email: str, password: str = "") -> None:
    with _used_accounts_lock:
        _used_accounts.append(
            {
                "email": email,
                "password": password,
                "ts": datetime.datetime.now().isoformat(),
            }
        )


def mark_error(email: str, reason: str = "") -> None:
    with _used_accounts_lock:
        _error_accounts.append(
            {
                "email": email,
                "reason": reason,
                "ts": datetime.datetime.now().isoformat(),
            }
        )


def save_cookies_snapshot(page_obj, tag: str, email: str = "") -> None:
    if PERF_FLAGS.get("skip_debug_io"):
        return
    if page_obj is None:
        return
    try:
        cookies = page_obj.cookies(all_domains=True, all_info=True) or []
    except Exception:
        try:
            cookies = page_obj.cookies() or []
        except Exception:
            cookies = []
    try:
        out_dir = str(PROJECT_ROOT / "logs" / "cookie_snapshots")
        os.makedirs(out_dir, exist_ok=True)
        safe_email = (email or "unknown").replace("@", "_at_").replace("/", "_")
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(out_dir, f"cookies_{tag}_{safe_email}_{stamp}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cookies, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def prepare_browser_for_next_account(log_callback=None) -> None:
    """Reuse TabPool session or recycle every N accounts; set page context.

    Proxy pool default is **thread-sticky** (same worker keeps one proxy so the
    Chromium process can be reused). Set ``proxy_rotate_every_account=true`` to
    rotate IP every account (forces browser relaunch because --proxy-server is
    fixed at process start).
    """
    log = log_callback or (lambda _m: None)
    rotate_proxy = False
    try:
        from grok_register.proxy.pool import (
            get_thread_proxy,
            proxy_count,
            proxy_log_label,
            proxy_rotate_every_account,
            rotate_thread_proxy,
        )

        n_proxies = proxy_count(config)
        rotate_proxy = n_proxies > 1 and proxy_rotate_every_account(config)
        if rotate_proxy:
            new_p = rotate_thread_proxy(config)
            log(f"[*] proxy rotate every-account → {proxy_log_label(new_p)}")
        else:
            # Pin sticky proxy once per thread (round-robin assign on first use)
            get_thread_proxy(config=config)
    except Exception:
        n_proxies = 0
        rotate_proxy = False

    use_pool = getattr(TabPool, "_options_factory", None) is not None
    if not use_pool:
        try:
            restart_browser(log_callback=log)
        except Exception as exc:
            log(f"[!] prepare_browser restart failed: {exc}")
        return

    force_recycle = rotate_proxy or not PERF_FLAGS.get("browser_reuse", True)
    if force_recycle:
        TabPool.release_tab()
        clear_page_context()
        tab = TabPool.get_tab()
        set_page_context(TabPool.get_browser(), tab)
        reason = "proxy rotate" if rotate_proxy else "reuse disabled"
        log(f"[*] browser full recycle ({reason})")
        return

    served = TabPool.mark_served()
    every = int(PERF_FLAGS.get("browser_recycle_every") or 25)
    if every > 0 and served % every == 0:
        log(f"[*] browser recycle every={every} served={served}")
        TabPool.release_tab()
        clear_page_context()
        tab = TabPool.get_tab()
        set_page_context(TabPool.get_browser(), tab)
        return

    ok = TabPool.clear_session(log_callback=log)
    br = TabPool.get_browser()
    tab = getattr(TabPool._thread_local, "tab", None) or (TabPool.get_tab() if br else None)
    if br is not None and tab is not None:
        set_page_context(br, tab)
    if not ok:
        log("[!] clear_session soft-failed; next account may need recycle")


def start_browser(log_callback=None):
    """Start Chromium; use TabPool when initialized (CLI multi-thread)."""
    global browser, page
    log = log_callback or (lambda _m: None)
    # TabPool path (register_cli)
    if getattr(TabPool, "_options_factory", None) is not None:
        last_exc = None
        for attempt in range(1, 5):
            try:
                tab = TabPool.get_tab()
                br = TabPool.get_browser()
                set_page_context(br, tab)
                if log_callback and attempt > 1:
                    log(f"[*] 浏览器第 {attempt} 次启动成功 (TabPool)")
                return br, tab
            except Exception as exc:
                last_exc = exc
                log(f"[Debug] TabPool 浏览器启动失败(第{attempt}/4次): {exc}")
                try:
                    TabPool.release_tab()
                except Exception:
                    pass
                clear_page_context()
                time.sleep(min(1.5 * attempt, 4))
        raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")

    # singleton path
    last_exc = None
    for attempt in range(1, 5):
        try:
            br = Chromium(create_browser_options())
            tabs = br.get_tabs()
            pg = tabs[-1] if tabs else br.new_tab()
            set_page_context(br, pg)
            if log_callback and getattr(br, "user_data_path", None):
                log(f"[Debug] 当前浏览器资料目录: {br.user_data_path}")
            if log_callback and attempt > 1:
                log(f"[*] 浏览器第 {attempt} 次启动成功")
            return br, pg
        except Exception as exc:
            last_exc = exc
            if log_callback:
                log(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
            try:
                if browser is not None:
                    browser.quit(del_data=True)
            except Exception:
                pass
            clear_page_context()
            browser = None
            page = None
            time.sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser():
    global browser, page
    if getattr(TabPool, "_options_factory", None) is not None:
        try:
            TabPool.release_tab()
        except Exception:
            pass
        clear_page_context()
        return
    if browser is not None:
        try:
            browser.quit(del_data=True)
        except Exception:
            pass
    browser = None
    page = None
    clear_page_context()


def restart_browser(log_callback=None):
    stop_browser()
    return start_browser(log_callback=log_callback)


def cleanup_runtime_memory(log_callback=None, reason="定期清理"):
    if log_callback:
        log_callback(f"[*] {reason}: 关闭浏览器并清理内存")
    stop_browser()
    collected = gc.collect()
    if log_callback:
        log_callback(f"[*] Python GC 已回收对象数: {collected}")


def refresh_active_page():
    global browser, page
    br = get_browser_obj()
    if br is None:
        restart_browser()
        br = get_browser_obj()
    try:
        tabs = br.get_tabs()
        if tabs:
            pg = tabs[-1]
            set_page_context(br, pg)
            return get_page()
        pg = br.new_tab()
        set_page_context(br, pg)
        return pg
    except Exception:
        restart_browser()
        return get_page()



class CloudflareBlockedError(Exception):
    """Signup page is a Cloudflare challenge / hard block interstitial."""


def page_looks_like_cloudflare(html_or_text: str = "", title: str = "") -> bool:
    t = f"{title or ''} {html_or_text or ''}".lower()
    markers = (
        "attention required",
        "just a moment",
        "cf-browser-verification",
        "checking your browser before accessing",
        "sorry, you have been blocked",
        "you are unable to access",
        "cdn-cgi/challenge",
        "cloudflare ray id",
        "enable javascript and cookies to continue",
    )
    return any(m in t for m in markers)


def cloudflare_challenge_kind(html_or_text: str = "", title: str = "") -> str:
    """Return ``hard`` | ``soft`` | ``none`` for CF interstitial severity."""
    t = f"{title or ''} {html_or_text or ''}".lower()
    if any(
        m in t
        for m in (
            "you have been blocked",
            "unable to access",
            "attention required",
            "sorry, you have been blocked",
        )
    ):
        return "hard"
    if page_looks_like_cloudflare(html_or_text, title):
        return "soft"  # Just a Moment / checking browser — may auto-clear
    return "none"


_STEALTH_INIT_JS = """
try {
  Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true,
  });
} catch (e) {}
try {
  // Some environments expose webdriver as true via prototype
  const proto = Navigator.prototype;
  if (proto) {
    Object.defineProperty(proto, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  }
} catch (e) {}
try {
  window.chrome = window.chrome || { runtime: {} };
} catch (e) {}
try {
  Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
    configurable: true,
  });
} catch (e) {}
"""


def apply_page_stealth(page=None, log_callback=None) -> None:
    """Best-effort navigator.webdriver hide (CDP init script + current page)."""
    pg = page or get_page()
    if pg is None:
        return
    try:
        pg.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=_STEALTH_INIT_JS)
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] stealth cdp: {exc}")
    try:
        pg.run_js(_STEALTH_INIT_JS)
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] stealth js: {exc}")


def page_looks_like_chrome_error(html_or_text: str = "", title: str = "") -> bool:
    """Chromium network/proxy error interstitial (not real xAI HTML)."""
    t = f"{title or ''} {html_or_text or ''}".lower()
    markers = (
        "err_proxy",
        "err_tunnel",
        "err_connection",
        "err_timed_out",
        "err_name_not_resolved",
        "err_empty_response",
        "err_ssl",
        "this site can't be reached",
        "无法访问此网站",
        "代理服务器",
        "proxy server",
        "chromium authors",  # chrome://network error shell
        "differs from the usual 40x error",
    )
    return any(m in t for m in markers)


class ProxyOrNetworkPageError(Exception):
    """Browser landed on Chrome error page (often proxy auth / tunnel fail)."""


def _page_cf_snapshot() -> tuple[str, str, str]:
    """Return (url, title, html_snip) for CF detection."""
    pg = get_page()
    if pg is None:
        return "", "", ""
    url = ""
    title = ""
    html = ""
    try:
        url = str(pg.url or "")
    except Exception:
        pass
    try:
        title = str(pg.title or "")
    except Exception:
        try:
            title = str(pg.run_js("return document.title || ''") or "")
        except Exception:
            pass
    try:
        html = str(pg.html or "")[:2000]
    except Exception:
        try:
            html = str(pg.run_js("return (document.documentElement&&document.documentElement.outerHTML)||''") or "")[:2000]
        except Exception:
            pass
    return url, title, html


def raise_if_cloudflare_block(log_callback=None) -> None:
    url, title, html = _page_cf_snapshot()
    if page_looks_like_chrome_error(html, title):
        if log_callback:
            log_callback(
                f"[!] 浏览器错误页（多为代理鉴权/隧道失败） title={title!r} url={url[:120]!r}"
            )
        raise ProxyOrNetworkPageError(
            "Chrome 错误页（非 xAI 注册页）。常见原因：代理 user:pass 未走本地鉴权转发、"
            "隧道 400/503、或网络超时。请确认代理探测可用，并使用本版本 local_auth_proxy。"
        )
    if page_looks_like_cloudflare(html, title) or page_looks_like_cloudflare(url):
        if log_callback:
            log_callback(
                f"[!] Cloudflare 拦截注册页 title={title!r} url={url[:120]!r}"
            )
        raise CloudflareBlockedError(
            "Cloudflare 拦截 accounts.x.ai（Attention Required / challenge）。"
            "批量/无头极易触发。请用: python register_cli.py --extra N --threads 1 --headed"
            "；或配置 PROXY/PROXY_POOL 后重试。"
        )


def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        raise_if_cloudflare_block(log_callback)
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = get_page().run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
const target = candidates[0]?.node || null;
if (!target) {
    return false;
}
target.click();
return candidates[0].text || true;
        """)

        if clicked:
            if log_callback:
                detail = f": {clicked}" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已点击「使用邮箱注册」按钮{detail}")
            sleep_with_cancel(2, cancel_callback)
            return True

        if log_callback:
            current_url = get_page().url if get_page() else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        sleep_with_cancel(1, cancel_callback)

    # Final CF / Chrome error check before generic error
    raise_if_cloudflare_block(log_callback)
    if log_callback:
        page_html = get_page().html[:500] if get_page() else "no page"
        log_callback(f"[Debug] 页面内容片段: {page_html}")
        if page_looks_like_chrome_error(page_html):
            raise ProxyOrNetworkPageError(
                "未找到注册按钮且页面为 Chrome 错误页（代理/网络失败）"
            )

    raise Exception("未找到「使用邮箱注册」按钮")


def open_signup_page(log_callback=None, cancel_callback=None):
    global browser, page  # set via set_page_context
    raise_if_cancelled(cancel_callback)
    if get_browser_obj() is None:
        start_browser()
        if log_callback:
            log_callback("[*] 浏览器已启动")
    try:
        br = get_browser_obj()
        pg = br.get_tab(0)
        set_page_context(br, pg)
        get_page().get(SIGNUP_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        try:
            br = get_browser_obj()
            pg = br.new_tab(SIGNUP_URL)
            set_page_context(br, pg)
        except Exception as e2:
            if log_callback:
                log_callback(f"[Debug] 创建新标签页异常: {e2}")
            restart_browser()
            br = get_browser_obj()
            pg = br.new_tab(SIGNUP_URL)
            set_page_context(br, pg)
    # Optional CF cookie inject — only when using a real proxy cache entry.
    # Headless-prewarmed "direct" cookies injected into headed multi-thread often
    # make Attention Required worse; skip for direct mode.
    try:
        from grok_register.browser.cf_prewarm import get_cached_cf_cookies, inject_cf_cookies_to_page
        from grok_register.proxy.pool import get_proxy_list, get_thread_proxy, resolve_headless

        proxy = get_thread_proxy(config=config)
        has_proxy = bool(get_proxy_list(config))
        if has_proxy and get_cached_cf_cookies(proxy):
            inject_cf_cookies_to_page(
                get_page(),
                proxy,
                log=log_callback or (lambda _m: None),
            )
            try:
                get_page().get(SIGNUP_URL)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 重新打开注册页失败: {exc}")
        elif log_callback and not has_proxy:
            log_callback("[*] 直连模式：跳过 CF 预热 cookie 注入（避免污染 headed 会话）")
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] CF cookie 注入跳过: {exc}")

    # Stealth JS only for background modes. Headed: leave real Chrome signals alone
    # (aggressive patches + extension can produce Turnstile 「验证失败」).
    try:
        from grok_register.proxy.pool import resolve_headless as _rh, resolve_headless_mode as _rhm

        is_headless = _rh(config)
        hmode = _rhm(config)
    except Exception:
        is_headless = False
        hmode = "off"
    if is_headless or hmode in ("pure", "offscreen"):
        apply_page_stealth(get_page(), log_callback=log_callback)
    try:
        get_page().wait.doc_loaded()
    except Exception:
        pass
    sleep_with_cancel(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {get_page().url}")

    # CF wait: hard block fail-fast; soft "Just a Moment" can auto-clear even offscreen/pure
    if not is_headless:
        wait_rounds = 40  # ~60s headed
    elif hmode == "offscreen":
        wait_rounds = 24  # ~36s — offscreen real Chrome may pass soft CF
    else:
        wait_rounds = 16  # ~24s pure headless (soft only)
    for i in range(wait_rounds):
        raise_if_cancelled(cancel_callback)
        url, title, html = _page_cf_snapshot()
        if page_looks_like_chrome_error(html, title):
            raise_if_cloudflare_block(log_callback)
        kind = cloudflare_challenge_kind(html, title)
        if kind == "hard":
            # Hard block rarely clears in background modes — short grace then fail
            if i >= (1 if hmode == "pure" else 3):
                raise_if_cloudflare_block(log_callback)
            if log_callback and i == 0:
                log_callback(
                    f"[*] Cloudflare 硬拦截（{hmode}），短暂等待后重试 title={title!r}"
                )
            sleep_with_cancel(1.5, cancel_callback)
            continue
        if kind == "none":
            try:
                ready = get_page().run_js(
                    r"""
const t = (document.body && (document.body.innerText||'')) || '';
return t.includes('使用邮箱') || t.includes('email') || t.includes('Sign') ||
       !!document.querySelector('button, a[href], input');
"""
                )
                if ready:
                    break
            except Exception:
                break
        else:
            # soft CF
            if log_callback and (i == 0 or i % 4 == 0):
                log_callback(
                    f"[*] Cloudflare 软挑战等待中（{hmode} {i+1}/{wait_rounds}）title={title!r}"
                )
            sleep_with_cancel(1.5, cancel_callback)
    else:
        raise_if_cloudflare_block(log_callback)

    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )


def has_profile_form(log_callback=None):
    refresh_active_page()
    try:
        return bool(
            get_page().run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


def fill_email_and_submit(timeout=45, log_callback=None, cancel_callback=None):
    raise_if_cancelled(cancel_callback)
    # Overlap TempMail API with DOM readiness (saves 0.5–2s per account)
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    mail_executor = ThreadPoolExecutor(max_workers=1)
    mail_fut = mail_executor.submit(get_email_and_token)
    email, dev_token = "", ""
    try:
        # Briefly wait for email input while mailbox is created in background
        ready_deadline = time.time() + min(8.0, float(timeout) * 0.4)
        while time.time() < ready_deadline:
            raise_if_cancelled(cancel_callback)
            try:
                ready = get_page().run_js(
                    r"""
const sels = 'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]';
const n = document.querySelector(sels);
if (!n) return false;
const st = getComputedStyle(n);
const r = n.getBoundingClientRect();
return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0 && !n.disabled;
"""
                )
                if ready:
                    break
            except Exception:
                pass
            if mail_fut.done():
                break
            sleep_with_cancel(0.25, cancel_callback, scale=False)
        try:
            email, dev_token = mail_fut.result(timeout=max(5.0, float(timeout)))
        except FuturesTimeout as exc:
            raise Exception("获取邮箱超时") from exc
    finally:
        mail_executor.shutdown(wait=False, cancel_futures=True)

    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    deadline = time.time() + timeout
    last_diag_time = 0
    last_reclick_time = 0
    last_snapshot = None
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = get_page().run_js(
            """
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
}
function describeInput(node) {
    return [
        `type=${node.getAttribute('type') || ''}`,
        `name=${node.getAttribute('name') || ''}`,
        `id=${node.getAttribute('id') || ''}`,
        `placeholder=${node.getAttribute('placeholder') || ''}`,
        `aria=${node.getAttribute('aria-label') || ''}`,
        `testid=${node.getAttribute('data-testid') || ''}`,
    ].join(' ').replace(/\\s+/g, ' ').trim().slice(0, 160);
}
function describeAction(node) {
    return textOf(node).slice(0, 120);
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const visibleInputs = Array.from(document.querySelectorAll('input, textarea'))
    .filter((node) => isVisible(node) && !node.disabled && !node.readOnly)
    .map(describeInput)
    .slice(0, 8);
const visibleActions = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map(describeAction)
    .filter(Boolean)
    .slice(0, 10);
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) {
    return {
        state: 'not-ready',
        url: location.href,
        title: document.title,
        inputs: visibleInputs,
        buttons: visibleActions,
    };
}
input.focus(); input.click();
const valueProto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const valueSetter = Object.getOwnPropertyDescriptor(valueProto, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
const inputType = (input.getAttribute('type') || '').toLowerCase();
const isValid = inputType !== 'email' || input.checkValidity();
if ((input.value || '').trim() !== email || !isValid) {
    return {
        state: 'fill-failed',
        value: input.value || '',
        valid: isValid,
        input: describeInput(input),
        url: location.href,
    };
}
input.blur();
return {
    state: 'filled',
    input: describeInput(input),
    url: location.href,
};
            """,
            email,
        )
        state = filled.get("state") if isinstance(filled, dict) else filled
        if isinstance(filled, dict):
            last_snapshot = filled
        if state == "not-ready":
            now = time.time()
            if now - last_reclick_time >= 3:
                reclicked = get_page().run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
if (!candidates.length) return false;
candidates[0].node.click();
return candidates[0].text || true;
                """)
                last_reclick_time = now
                if reclicked and log_callback:
                    detail = f": {reclicked}" if isinstance(reclicked, str) else ""
                    log_callback(f"[Debug] 邮箱输入框未出现，已再次触发邮箱注册入口{detail}")
            if log_callback and now - last_diag_time >= 5:
                last_diag_time = now
                inputs = " | ".join((filled or {}).get("inputs", [])[:6]) if isinstance(filled, dict) else ""
                buttons = " | ".join((filled or {}).get("buttons", [])[:8]) if isinstance(filled, dict) else ""
                url = (filled or {}).get("url", get_page().url if get_page() else "") if isinstance(filled, dict) else (get_page().url if get_page() else "")
                log_callback(f"[Debug] 等待邮箱输入框: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if state != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        sleep_with_cancel(0.8, cancel_callback)
        clicked = get_page().run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !(input.value || '').trim()) return false;
const inputType = (input.getAttribute('type') || '').toLowerCase();
if (inputType === 'email' && !input.checkValidity()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find((node) => {
    const text = textOf(node).replace(/\\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        text.includes('继续') ||
        text.includes('下一步') ||
        text.includes('确认') ||
        lower.includes('signup') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next') ||
        lower.includes('createaccount') ||
        lower.includes('submit')
    );
});
if (submitButton) {
    submitButton.click();
    return textOf(submitButton) || true;
}
const form = input.closest('form');
if (form) {
    if (form.requestSubmit) form.requestSubmit();
    else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    return 'form-submit';
}
input.focus();
input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
return 'enter';
            """
        )
        if clicked:
            sleep_with_cancel(1.0, cancel_callback)
            rejection = get_page().run_js(
                r"""
const text = String(document.body?.innerText || document.body?.textContent || '');
const compact = text.replace(/\\s+/g, ' ');
const lower = compact.toLowerCase();
if (
  (compact.includes('已被拒绝') && (compact.includes('邮箱域名') || compact.includes('域名'))) ||
  (lower.includes('rejected') && (lower.includes('email') || lower.includes('domain'))) ||
  lower.includes('email domain') && lower.includes('not allowed')
) {
  return compact.slice(0, 500);
}
return '';
                """
            )
            if rejection:
                domain = email_domain(email)
                if domain:
                    _yyds_runtime_blocked_domains.add(domain)
                if log_callback:
                    log_callback(f"[!] 邮箱域名被目标站拒绝，已临时跳过该域名: {domain or email}")
                raise EmailDomainRejected(f"邮箱域名被拒绝: {domain or email}; {rejection}")
            if log_callback:
                detail = f" ({clicked})" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已填写邮箱并提交: {email}{detail}")
            return email, dev_token
        sleep_with_cancel(0.5, cancel_callback)
    if last_snapshot:
        inputs = " | ".join(last_snapshot.get("inputs", [])[:6])
        buttons = " | ".join(last_snapshot.get("buttons", [])[:8])
        url = last_snapshot.get("url", get_page().url if get_page() else "")
        raise Exception(
            f"未找到邮箱输入框或注册按钮，最后页面: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}"
        )
    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(
    email,
    dev_token,
    timeout=None,
    poll_interval=None,
    log_callback=None,
    cancel_callback=None,
):
    def _resend_code():
        get_page().run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

    if timeout is None:
        timeout = get_code_poll_timeout()
    if poll_interval is None:
        poll_interval = get_code_poll_interval()
    if log_callback:
        log_callback(f"[*] 等待验证码，最多 {timeout}s；超过即更换邮箱")

    code = get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = get_page().run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue

        clicked = get_page().run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next')
    );
});

if (!btn) return 'no-button';
btn.focus();
btn.click();
return 'clicked';
            """
        )

        if clicked == "clicked" or clicked == "no-button":
            if log_callback:
                log_callback(f"[*] 已填写验证码并提交: {code}")
            sleep_with_cancel(1.5, cancel_callback)
            return code

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("验证码已获取，但自动填写/提交失败")


def _read_turnstile_token(page=None) -> str:
    """Read current Turnstile response token if present."""
    pg = page or get_page()
    if pg is None:
        return ""
    try:
        token = pg.run_js(
            """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  const inputs = Array.from(document.querySelectorAll('input[name*="turnstile"], textarea[name*="turnstile"]'));
  for (const el of inputs) {
    const v = String(el.value || '').trim();
    if (v.length >= 80) return v;
  }
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    const r = String(turnstile.getResponse() || '').trim();
    if (r) return r;
  }
  return '';
} catch (e) { return ''; }
"""
        )
        return str(token or "").strip()
    except Exception:
        return ""


def _bring_page_front_for_turnstile(page=None, log_callback=None) -> None:
    """Raise Chrome tab/window so Turnstile can paint and accept real pointer events."""
    pg = page or get_page()
    if pg is None:
        return
    try:
        pg.run_cdp("Page.bringToFront")
    except Exception:
        pass
    try:
        # Restore window if minimized / force a visible on-screen rect
        info = pg.run_cdp("Browser.getWindowForTarget")
        window_id = (info or {}).get("windowId")
        if window_id is not None:
            try:
                pg.run_cdp(
                    "Browser.setWindowBounds",
                    windowId=window_id,
                    bounds={
                        "left": 40,
                        "top": 40,
                        "width": 1100,
                        "height": 800,
                        "windowState": "normal",
                    },
                )
            except Exception:
                try:
                    pg.run_cdp(
                        "Browser.setWindowBounds",
                        windowId=window_id,
                        bounds={"windowState": "normal"},
                    )
                except Exception:
                    pass
    except Exception:
        pass
    try:
        pg.run_js(
            """
try { window.focus(); } catch (e) {}
try { document.body && document.body.click(); } catch (e) {}
"""
        )
    except Exception:
        pass


def _find_turnstile_iframe(page=None):
    """Locate Turnstile challenge iframe via shadow DOM or src match."""
    pg = page or get_page()
    if pg is None:
        return None
    # Path used by DrissionPage for closed shadow + iframe
    try:
        challenge_input = pg.ele("@name=cf-turnstile-response", timeout=0.25)
        if challenge_input:
            node = challenge_input
            for _ in range(6):
                try:
                    parent = node.parent()
                except Exception:
                    parent = None
                if parent is None:
                    break
                try:
                    sr = parent.shadow_root
                    if sr:
                        iframe = sr.ele("tag:iframe", timeout=0.15)
                        if iframe:
                            return iframe
                except Exception:
                    pass
                node = parent
    except Exception:
        pass
    # Fallback: any challenge/turnstile iframe on page
    try:
        for iframe in pg.eles("tag:iframe", timeout=0.3) or []:
            try:
                src = str(iframe.attr("src") or "").lower()
            except Exception:
                src = ""
            if "challenges.cloudflare.com" in src or "turnstile" in src:
                return iframe
    except Exception:
        pass
    return None


def _cdp_pointer_click(page, x: float, y: float) -> bool:
    """Dispatch a real CDP mouse move+click at viewport coordinates."""
    try:
        x = float(x)
        y = float(y)
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mouseMoved",
            x=x,
            y=y,
            modifiers=0,
        )
        time.sleep(random.uniform(0.03, 0.08))
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mousePressed",
            x=x,
            y=y,
            button="left",
            buttons=1,
            clickCount=1,
            modifiers=0,
        )
        time.sleep(random.uniform(0.02, 0.06))
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mouseReleased",
            x=x,
            y=y,
            button="left",
            buttons=0,
            clickCount=1,
            modifiers=0,
        )
        return True
    except Exception:
        return False


def _click_turnstile_widget(page=None, log_callback=None) -> str:
    """
    Multi-strategy click on Turnstile checkbox/widget.
    Returns a short strategy label for debug logs.
    """
    pg = page or get_page()
    if pg is None:
        return "no-page"
    strategies: list[str] = []

    iframe = _find_turnstile_iframe(pg)

    # 1) Patch MouseEvent screenX/Y inside iframe (helps some CF probes)
    if iframe is not None:
        try:
            iframe.run_js(
                """
window.dtp = 1;
function getRandomInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}
try {
  Object.defineProperty(MouseEvent.prototype, 'screenX', { get() { return getRandomInt(800, 1400); } });
  Object.defineProperty(MouseEvent.prototype, 'screenY', { get() { return getRandomInt(300, 900); } });
} catch (e) {}
"""
            )
        except Exception:
            pass

        # 2) CDP coordinate click — checkbox sits on the left of the widget (~300x65)
        try:
            pg.scroll.to_see(iframe)
        except Exception:
            try:
                iframe.scroll.to_see()
            except Exception:
                pass
        try:
            size = iframe.rect.size
            loc = iframe.rect.viewport_location
            w = float(size[0] or 0)
            h = float(size[1] or 0)
            left = float(loc[0] or 0)
            top = float(loc[1] or 0)
            if w > 10 and h > 10:
                # Prefer checkbox zone (left ~28–40px, vertical center)
                cx = left + min(36.0, w * 0.18) + random.uniform(-2, 4)
                cy = top + h * 0.5 + random.uniform(-3, 3)
                if _cdp_pointer_click(pg, cx, cy):
                    strategies.append("cdp-checkbox")
                # Secondary click near center (managed widgets)
                if random.random() < 0.55:
                    mx = left + w * 0.45 + random.uniform(-4, 4)
                    my = top + h * 0.5 + random.uniform(-2, 2)
                    _cdp_pointer_click(pg, mx, my)
                    strategies.append("cdp-mid")
        except Exception:
            pass

        # 3) Element offset click via DrissionPage (uses CDP under the hood)
        try:
            w, h = iframe.rect.size
            ox = max(12, min(int(w * 0.15), 40)) + random.randint(-2, 3)
            oy = max(8, int(h // 2) + random.randint(-3, 3))
            iframe.click.at(offset_x=ox, offset_y=oy)
            strategies.append("click-at")
        except Exception:
            try:
                iframe.click()
                strategies.append("iframe-click")
            except Exception:
                pass

        # 4) Shadow DOM checkbox inside challenge iframe (classic path)
        try:
            body = iframe.ele("tag:body", timeout=0.2)
            body_sr = body.shadow_root if body else None
            btn = None
            if body_sr:
                btn = (
                    body_sr.ele("tag:input", timeout=0.15)
                    or body_sr.ele(".mark", timeout=0.1)
                    or body_sr.ele("@type=checkbox", timeout=0.1)
                )
            if btn is not None:
                try:
                    btn.click()
                except Exception:
                    try:
                        btn.click(by_js=True)
                    except Exception:
                        pass
                strategies.append("shadow-input")
        except Exception:
            pass

    # 5) Host-page container / API kick
    try:
        kicked = pg.run_js(
            """
try {
  const nodes = Array.from(document.querySelectorAll(
    'iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey]'
  ));
  for (const n of nodes) {
    try { n.scrollIntoView({block:'center', inline:'center'}); } catch (e) {}
    try { if (typeof n.click === 'function') n.click(); } catch (e) {}
  }
  // Nudge render: some widgets only finish after a focus cycle
  const cf = document.querySelector('input[name="cf-turnstile-response"]');
  if (cf) {
    try { cf.focus(); cf.blur(); } catch (e) {}
  }
  return nodes.length;
} catch (e) { return 0; }
"""
        )
        if kicked:
            strategies.append(f"host-js:{kicked}")
    except Exception:
        pass

    return "+".join(strategies) if strategies else "no-widget"


def _turnstile_widget_failed(page=None) -> bool:
    """Detect Turnstile failure UI (验证失败 / 故障排除)."""
    pg = page or get_page()
    if pg is None:
        return False
    try:
        hit = pg.run_js(
            r"""
try {
  const t = ((document.body && document.body.innerText) || '') + ' ' + (document.title || '');
  if (/验证失败|故障排除|verification failed|trouble\s*shoot/i.test(t)) return true;
  // error state often keeps empty response + visible error text near widget
  const cf = document.querySelector('input[name="cf-turnstile-response"]');
  if (cf) {
    const wrap = cf.closest('form, div, section') || document.body;
    const wt = (wrap && wrap.innerText) || '';
    if (/验证失败|故障排除|verification failed/i.test(wt)) return true;
  }
  return false;
} catch (e) { return false; }
"""
        )
        return bool(hit)
    except Exception:
        return False


def getTurnstileToken(log_callback=None, cancel_callback=None, *, max_rounds: int | None = None):
    if get_page() is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    try:
        from grok_register.proxy.pool import resolve_headless as _rh, resolve_headless_mode as _rhm

        bg_mode = _rh(config)
        hmode = _rhm(config)
    except Exception:
        bg_mode = False
        hmode = "off"

    # Prefer reading existing token before reset (avoid wiping a just-solved widget)
    existing = _read_turnstile_token()
    if len(existing) >= 80:
        if log_callback:
            log_callback(f"[*] Turnstile 已有 token，跳过 reset，长度={len(existing)}")
        return existing

    # NEVER reset on failure UI / headed: reset often locks Turnstile into 验证失败 loop
    if bg_mode:
        pass  # never reset in background either
    # Raise window so widget paints (critical for offscreen / background Chrome)
    if bg_mode:
        _bring_page_front_for_turnstile(get_page(), log_callback=log_callback)
        try:
            apply_page_stealth(get_page())
        except Exception:
            pass
    else:
        # Headed: just focus tab, do not re-apply stealth patches mid-challenge
        try:
            get_page().run_cdp("Page.bringToFront")
        except Exception:
            pass

    if max_rounds is not None:
        rounds = max_rounds
    elif bg_mode:
        rounds = 70 if hmode == "offscreen" else 50
    elif PERF_FLAGS.get("fast"):
        rounds = 40
    else:
        rounds = 45  # ~40s passive wait for managed Turnstile
    poll = 0.5 if bg_mode else 0.9
    last_strategy = ""
    # Headed: passive first (let managed Turnstile self-solve). Click only later.
    passive_rounds = 0 if bg_mode else (18 if not PERF_FLAGS.get("fast") else 10)
    for i in range(0, rounds):
        raise_if_cancelled(cancel_callback)
        try:
            token = _read_turnstile_token()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            if _turnstile_widget_failed(get_page()):
                if log_callback and i % 5 == 0:
                    log_callback(
                        "[!] Turnstile 显示「验证失败/故障排除」——停止连点；"
                        "有界面请手动点一下刷新图标，或换网络/代理后重试"
                    )
                # Do not spam-click a failed widget (makes it worse)
                sleep_with_cancel(1.2, cancel_callback, scale=False)
                continue

            # Background: click periodically. Headed: wait passively, rare gentle click.
            should_click = False
            if bg_mode:
                should_click = i < 8 or i % 2 == 0
            elif i >= passive_rounds and (i - passive_rounds) % 6 == 0:
                should_click = True
            if should_click:
                strategy = _click_turnstile_widget(get_page(), log_callback=log_callback)
                if strategy and strategy != last_strategy:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 点击策略: {strategy}")
                    last_strategy = strategy
        except Exception as exc:
            if log_callback and i == 0:
                log_callback(f"[Debug] Turnstile 轮询异常: {exc}")
        sleep_with_cancel(poll, cancel_callback, scale=False)

    token = _read_turnstile_token()
    if len(token) >= 80:
        if log_callback:
            log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
        return token
    failed = _turnstile_widget_failed(get_page())
    raise Exception(
        "Turnstile 获取 token 失败"
        + ("（页面显示验证失败/故障排除）" if failed else "")
        + "。有界面：勿强行伪装 UA；可手动点 widget 刷新；或配置 PROXY_POOL。"
        f" last_click={last_strategy or 'none'}"
    )


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    # Avoid trailing '-' so accounts lines "email----password----sso" stay unambiguous
    # (token_urlsafe may end with '-' and corrupt JWT when split on '----').
    tail = secrets.token_urlsafe(8).replace("-", "x").replace("_", "y")[:8]
    password = "N" + secrets.token_hex(4) + "!a7#" + tail
    return given_name, family_name, password


def fill_profile_and_submit(timeout=120, log_callback=None, cancel_callback=None):
    given_name, family_name, password = build_profile()
    # Background modes need more wall time for profile Turnstile
    try:
        from grok_register.proxy.pool import resolve_headless as _rh, resolve_headless_mode as _rhm

        if _rh(config) and timeout < 180:
            timeout = 180 if _rhm(config) == "offscreen" else 160
    except Exception:
        pass
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not form_filled_once:
            filled = get_page().run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

if (submitBtn) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
            """,
                given_name,
                family_name,
                password,
            )

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                if log_callback:
                    token_len = filled.split(":", 1)[1] if ":" in filled else "0"
                    log_callback(f"[*] 资料已填写，等待 Cloudflare 人机验证通过... 当前token长度={token_len}")
                fast = bool(PERF_FLAGS.get("fast"))
                try:
                    from grok_register.proxy.pool import resolve_headless as _rh

                    bg_mode = bool(_rh(config))
                except Exception:
                    bg_mode = False
                if token_len == "0":
                    pause_seconds = 0.4 if fast else random.uniform(0.8, 1.4)
                    if log_callback and not fast and wait_cf_since is None:
                        log_callback(
                            "[*] Cloudflare token 为空，先被动等待 managed 校验"
                            + ("（后台将稍后协助点击）" if bg_mode else "（有界面请勿关窗，可手动点一下勾选框）")
                        )
                    sleep_with_cancel(pause_seconds, cancel_callback, scale=False)
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                    # Background only: raise window immediately. Headed: no click spam.
                    if bg_mode:
                        try:
                            _bring_page_front_for_turnstile(get_page(), log_callback=log_callback)
                        except Exception:
                            pass
                # Headed: wait ~15s before assist. Background: earlier.
                retry_after = (3.0 if fast else 5.0) if bg_mode else (8.0 if fast else 15.0)
                retry_gap = (5.0 if fast else 8.0) if bg_mode else (12.0 if fast else 18.0)
                if now - wait_cf_since >= retry_after and now - last_cf_retry_at >= retry_gap:
                    if _turnstile_widget_failed(get_page()):
                        if log_callback:
                            log_callback(
                                "[!] 页面已是「验证失败/故障排除」，跳过自动连点；"
                                "请手动点 Turnstile 刷新，或停掉后换代理/网络再跑"
                            )
                        last_cf_retry_at = now
                    else:
                        if log_callback:
                            log_callback("[*] Cloudflare 验证协助：主动触发 Turnstile...")
                        try:
                            token = getTurnstileToken(
                                log_callback=log_callback, cancel_callback=cancel_callback
                            )
                            if token:
                                synced = get_page().run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(f"[*] Turnstile 回填完成，长度={synced}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] Turnstile 协助失败: {cf_exc}")
                        last_cf_retry_at = now
                sleep_with_cancel(0.45 if fast else 0.9, cancel_callback, scale=False)
                continue

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                sleep_with_cancel(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                sleep_with_cancel(0.5, cancel_callback)
                continue

        submit_state = get_page().run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'no-submit-button:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'submitted';
            """
        )

        if isinstance(submit_state, str) and submit_state.startswith("wait-cloudflare"):
            if log_callback:
                token_len = submit_state.split(":", 1)[1] if ":" in submit_state else "0"
                log_callback(f"[*] 等待 Cloudflare 人机验证通过后再提交... 当前token长度={token_len}")
            now = time.time()
            try:
                from grok_register.proxy.pool import resolve_headless as _rh

                bg_mode = bool(_rh(config))
            except Exception:
                bg_mode = False
            if wait_cf_since is None:
                wait_cf_since = now
                if bg_mode:
                    try:
                        _bring_page_front_for_turnstile(get_page(), log_callback=log_callback)
                    except Exception:
                        pass
            fast = bool(PERF_FLAGS.get("fast"))
            retry_after = (3.0 if fast else 5.0) if bg_mode else (8.0 if fast else 15.0)
            retry_gap = (5.0 if fast else 8.0) if bg_mode else (12.0 if fast else 18.0)
            if now - wait_cf_since >= retry_after and now - last_cf_retry_at >= retry_gap:
                if _turnstile_widget_failed(get_page()):
                    if log_callback:
                        log_callback("[!] 提交前 Turnstile 已失败，停止自动连点（请手动刷新 widget 或换网络）")
                    last_cf_retry_at = now
                else:
                    if log_callback:
                        log_callback("[*] 提交前仍卡住，主动触发 Turnstile...")
                    try:
                        token = getTurnstileToken(
                            log_callback=log_callback, cancel_callback=cancel_callback
                        )
                        if token:
                            synced = get_page().run_js(
                                """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                """,
                                token,
                            )
                            if log_callback:
                                log_callback(f"[*] Turnstile 回填完成，长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 协助失败: {cf_exc}")
                    last_cf_retry_at = now
            sleep_with_cancel(0.45 if fast else 0.9, cancel_callback, scale=False)
            continue

        if submit_state == "submitted":
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        wait_cf_since = None
        if isinstance(submit_state, str) and submit_state.startswith("no-submit-button") and log_callback:
            visible_buttons = submit_state.split(":", 1)[1] if ":" in submit_state else ""
            suffix = f" 可见按钮: {visible_buttons}" if visible_buttons else ""
            log_callback(f"[Debug] 未找到提交按钮，继续等待页面稳定...{suffix}")

        sleep_with_cancel(0.5, cancel_callback)

    if wait_cf_since is not None:
        raise Exception(
            "最终注册页 Cloudflare/Turnstile 验证失败（等待超时）。"
            "建议：python register_cli.py --extra 1 --threads 1 --headed；"
            "或配置 PROXY_POOL 后重试 --headless --headless-mode offscreen"
        )
    raise Exception("最终注册页资料填写失败")


def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0
    final_no_submit_state = ""
    final_no_submit_since = None
    final_no_submit_timeout = 25

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            refresh_active_page()
            if get_page() is None:
                sleep_with_cancel(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = get_page().run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\\s+/g, '');
    const lower = t.toLowerCase();
    return t.includes('完成注册') || lower.includes('completeyoursignup') || lower.includes('completesignup');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'final-page-no-submit:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and (retried == "final-page-clicked-submit" or (isinstance(retried, str) and retried.startswith("final-page-no-submit"))):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if isinstance(retried, str) and retried.startswith("final-page-no-submit"):
                    if retried != final_no_submit_state:
                        final_no_submit_state = retried
                        final_no_submit_since = now
                    elif final_no_submit_since and now - final_no_submit_since >= final_no_submit_timeout:
                        raise AccountRetryNeeded(
                            f"最终注册页状态 {final_no_submit_timeout}s 未变化且未找到提交按钮，重试当前账号: {retried}"
                        )
                else:
                    final_no_submit_state = ""
                    final_no_submit_since = None
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 10:
                        if log_callback:
                            log_callback("[*] 最终页 Cloudflare 卡住，自动二次复用 Turnstile...")
                        try:
                            token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                            if token:
                                synced = get_page().run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(f"[*] 最终页 Turnstile 二次复用完成，回填长度={synced}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 二次复用失败: {cf_exc}")
                        last_cf_retry_at = now

            cookies = get_page().cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[*] 已获取到 sso cookie")
                    return value
        except PageDisconnectedError:
            refresh_active_page()
        except AccountRetryNeeded:
            raise
        except Exception:
            pass

        sleep_with_cancel(1, cancel_callback)

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


def normalize_cpa_cloud_api_base(raw_base):
    """Normalize user input to ``…/v0/management`` for CLIProxyAPI Management API.

    Accepts host-only, full origin, or paths already including
    ``/v0/management`` or ``/v0/management/auth-files``.
    """
    base = (raw_base or "").strip().rstrip("/")
    if not base:
        return ""
    if not re.match(r"^https?://", base, re.I):
        base = "http://" + base
    # Strip accidental auth-files / management suffixes then re-append management.
    base = re.sub(r"/v0/management/auth-files/?$", "", base, flags=re.I)
    base = re.sub(r"/v0/management/?$", "", base, flags=re.I).rstrip("/")
    return base + "/v0/management"


def get_cpa_cloud_management_key(cfg):
    # Env wins so config.json can omit the secret if desired.
    return (
        os.environ.get("CPA_CLOUD_MANAGEMENT_KEY")
        or os.environ.get("CLI_PROXY_MANAGEMENT_KEY")
        or os.environ.get("MANAGEMENT_PASSWORD")
        or str(cfg.get("cpa_cloud_management_key") or "")
    ).strip()


def _cpa_cloud_cfg_int(cfg, name, default, minimum=None, maximum=None):
    try:
        value = int((cfg or {}).get(name, default) or default)
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def upload_cpa_auth_file_to_cloud(cpa_path, cfg=None, log_callback=None, force=False):
    """Upload one local CPA/OIDC JSON auth file to online CLIProxyAPI.

    Target: ``POST {api}/v0/management/auth-files`` (multipart field ``file``).
    Auth: ``Authorization: Bearer <key>`` and ``X-Management-Key: <key>``
    (CLIProxyAPI accepts either; remote needs ``remote-management.allow-remote``
    or ``MANAGEMENT_PASSWORD`` on the server).

    Config keys (or env):
      - cpa_cloud_upload_enabled / force=True for one-shot CLI upload
      - cpa_cloud_api_base / CPA_CLOUD_API_BASE  e.g. https://cpa.example.com:8317
      - cpa_cloud_management_key / CPA_CLOUD_MANAGEMENT_KEY
      - cpa_cloud_upload_timeout, cpa_cloud_upload_retries
    """
    cfg = cfg or config
    log = log_callback or (lambda m: None)
    if not force and not cfg.get("cpa_cloud_upload_enabled", False):
        return {"ok": False, "skipped": True, "reason": "disabled"}
    path = os.path.abspath(os.path.expanduser(str(cpa_path or "")))
    if not path or not os.path.isfile(path):
        log(f"[cloud-cpa] upload skipped: file not found: {path}")
        return {"ok": False, "error": "file_not_found", "path": path}
    if not path.lower().endswith(".json"):
        log(f"[cloud-cpa] upload skipped: not a .json auth file: {path}")
        return {"ok": False, "error": "not_json", "path": path}
    api_base = normalize_cpa_cloud_api_base(
        cfg.get("cpa_cloud_api_base") or os.environ.get("CPA_CLOUD_API_BASE") or ""
    )
    if not api_base:
        log("[cloud-cpa] upload skipped: cpa_cloud_api_base is empty")
        return {"ok": False, "error": "missing_api_base", "path": path}
    key = get_cpa_cloud_management_key(cfg)
    if not key:
        log("[cloud-cpa] upload skipped: management key is empty")
        return {"ok": False, "error": "missing_management_key", "path": path}
    url = api_base + "/auth-files"
    timeout = _cpa_cloud_cfg_int(cfg, "cpa_cloud_upload_timeout", 30, minimum=5, maximum=180)
    name = os.path.basename(path)
    retries = _cpa_cloud_cfg_int(cfg, "cpa_cloud_upload_retries", 3, minimum=1, maximum=10)
    last_error = None
    # Both headers: Bearer is documented; X-Management-Key is also accepted.
    headers = {
        "Authorization": "Bearer " + key,
        "X-Management-Key": key,
    }
    for attempt in range(1, retries + 1):
        try:
            with open(path, "rb") as fh:
                files = {"file": (name, fh, "application/json")}
                res = std_requests.post(url, headers=headers, files=files, timeout=timeout)
            preview = response_preview(res, 300)
            if 200 <= res.status_code < 300:
                try:
                    data = res.json()
                except Exception:
                    data = {"raw": preview}
                # Docs: { "status": "ok" }; some builds may return uploaded count.
                status_ok = True
                if isinstance(data, dict) and "status" in data:
                    status_ok = str(data.get("status") or "").lower() in ("ok", "success", "")
                if not status_ok and isinstance(data, dict) and data.get("error"):
                    last_error = f"status={res.status_code} body={preview}"
                    log(f"[cloud-cpa] upload rejected: {last_error}")
                    return {
                        "ok": False,
                        "status_code": res.status_code,
                        "path": path,
                        "name": name,
                        "error": preview,
                        "response": data,
                    }
                uploaded = data.get("uploaded") if isinstance(data, dict) else None
                suffix = f" uploaded={uploaded}" if uploaded is not None else ""
                log(f"[cloud-cpa] uploaded -> {name} @ {api_base} status={res.status_code}{suffix}")
                return {
                    "ok": True,
                    "status_code": res.status_code,
                    "path": path,
                    "name": name,
                    "url": url,
                    "response": data,
                }
            last_error = f"status={res.status_code} body={preview}"
            # 403 = remote management disabled; 401 = bad key — no point retrying.
            if res.status_code in (401, 403, 404):
                log(f"[cloud-cpa] upload failed (no retry): {last_error}")
                return {
                    "ok": False,
                    "status_code": res.status_code,
                    "path": path,
                    "name": name,
                    "error": preview,
                }
            if attempt < retries and res.status_code in (408, 429, 500, 502, 503, 504):
                log(f"[cloud-cpa] upload retry {attempt}/{retries}: {last_error}")
                time.sleep(min(2 * attempt, 8))
                continue
            log(f"[cloud-cpa] upload failed: {last_error}")
            return {
                "ok": False,
                "status_code": res.status_code,
                "path": path,
                "name": name,
                "error": preview,
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt < retries:
                log(f"[cloud-cpa] upload retry {attempt}/{retries}: {exc}")
                time.sleep(min(2 * attempt, 8))
                continue
            log(f"[cloud-cpa] upload exception: {exc}")
            return {"ok": False, "path": path, "name": name, "error": str(exc)}
    return {"ok": False, "path": path, "name": name, "error": last_error or "unknown"}


def _cpa_cloud_mgmt_client(cfg=None):
    """Return (api_base, headers, timeout, error_dict_or_None)."""
    cfg = cfg or config
    api_base = normalize_cpa_cloud_api_base(
        cfg.get("cpa_cloud_api_base") or os.environ.get("CPA_CLOUD_API_BASE") or ""
    )
    if not api_base:
        return "", {}, 0, {"ok": False, "error": "missing_api_base"}
    key = get_cpa_cloud_management_key(cfg)
    if not key:
        return "", {}, 0, {"ok": False, "error": "missing_management_key"}
    timeout = _cpa_cloud_cfg_int(cfg, "cpa_cloud_upload_timeout", 30, minimum=5, maximum=180)
    headers = {
        "Authorization": "Bearer " + key,
        "X-Management-Key": key,
        "Accept": "application/json",
    }
    return api_base, headers, timeout, None


def list_cpa_auth_files_on_cloud(cfg=None, log_callback=None):
    """GET /v0/management/auth-files — list online CPA credentials.

    Returns ``{ok, files: [{name, email, provider, ...}], ...}``.
    """
    cfg = cfg or config
    log = log_callback or (lambda m: None)
    api_base, headers, timeout, err = _cpa_cloud_mgmt_client(cfg)
    if err:
        log(f"[cloud-cpa] list skipped: {err.get('error')}")
        return err
    url = api_base + "/auth-files"
    try:
        res = std_requests.get(url, headers=headers, timeout=timeout)
        preview = response_preview(res, 400)
        if not (200 <= res.status_code < 300):
            log(f"[cloud-cpa] list failed: status={res.status_code} body={preview}")
            return {
                "ok": False,
                "status_code": res.status_code,
                "error": preview,
                "files": [],
            }
        try:
            data = res.json()
        except Exception:
            return {"ok": False, "error": "invalid_json", "raw": preview, "files": []}
        files = []
        if isinstance(data, dict):
            raw_files = data.get("files")
            if isinstance(raw_files, list):
                files = [f for f in raw_files if isinstance(f, dict)]
            elif isinstance(data.get("items"), list):
                files = [f for f in data["items"] if isinstance(f, dict)]
        elif isinstance(data, list):
            files = [f for f in data if isinstance(f, dict)]
        log(f"[cloud-cpa] listed {len(files)} auth file(s) @ {api_base}")
        return {"ok": True, "files": files, "api_base": api_base, "raw": data}
    except Exception as exc:
        log(f"[cloud-cpa] list exception: {exc}")
        return {"ok": False, "error": str(exc), "files": []}


def delete_cpa_auth_file_on_cloud(name, cfg=None, log_callback=None):
    """DELETE /v0/management/auth-files?name=<file.json> — exact filename."""
    cfg = cfg or config
    log = log_callback or (lambda m: None)
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "empty_name"}
    # accept path-like input → basename
    name = os.path.basename(name.replace("\\", "/"))
    if not name.endswith(".json"):
        # allow bare stem like xai-user@x.com → xai-user@x.com.json
        if not name.startswith("."):
            name = name + ".json"
    api_base, headers, timeout, err = _cpa_cloud_mgmt_client(cfg)
    if err:
        return err
    url = api_base + "/auth-files"
    try:
        res = std_requests.delete(
            url, headers=headers, params={"name": name}, timeout=timeout
        )
        preview = response_preview(res, 300)
        if 200 <= res.status_code < 300:
            log(f"[cloud-cpa] deleted -> {name}")
            try:
                data = res.json()
            except Exception:
                data = {"raw": preview}
            return {"ok": True, "name": name, "status_code": res.status_code, "response": data}
        log(f"[cloud-cpa] delete failed: name={name} status={res.status_code} body={preview}")
        return {
            "ok": False,
            "name": name,
            "status_code": res.status_code,
            "error": preview,
        }
    except Exception as exc:
        log(f"[cloud-cpa] delete exception: {name}: {exc}")
        return {"ok": False, "name": name, "error": str(exc)}


def _auth_file_match_fields(entry):
    """Collect strings used for fuzzy match on a list entry."""
    if not isinstance(entry, dict):
        return []
    fields = []
    for key in (
        "name",
        "id",
        "email",
        "label",
        "provider",
        "path",
        "auth_index",
        "account",
        "account_type",
    ):
        val = entry.get(key)
        if val is not None and str(val).strip():
            fields.append(str(val))
    return fields


def match_cpa_auth_files(files, patterns, *, case_sensitive=False):
    """Filter auth-file entries by fuzzy patterns (OR across patterns).

    Pattern rules (each pattern):
      - contains ``*`` or ``?`` → shell glob against name / email / id / ...
      - otherwise → substring match (case-insensitive by default)
      - prefix ``re:`` → regular expression
      - exact ``name.json`` still works as substring / glob

    Returns list of matched entry dicts (deduped by name).
    """
    import fnmatch

    pats = []
    if patterns is None:
        pats = []
    elif isinstance(patterns, (str, bytes)):
        pats = [str(patterns)]
    else:
        pats = [str(p) for p in patterns if str(p).strip()]

    if not pats:
        return list(files or [])

    matched = []
    seen = set()
    for entry in files or []:
        if not isinstance(entry, dict):
            continue
        fields = _auth_file_match_fields(entry)
        if not fields:
            continue
        name = str(entry.get("name") or entry.get("id") or fields[0])
        hit = False
        for pat in pats:
            pat = pat.strip()
            if not pat:
                continue
            if pat.lower().startswith("re:"):
                rx = pat[3:]
                flags = 0 if case_sensitive else re.I
                try:
                    cre = re.compile(rx, flags)
                except re.error:
                    continue
                if any(cre.search(f) for f in fields):
                    hit = True
                    break
            elif any(ch in pat for ch in "*?[]"):
                # glob — try against each field and basename-like name
                check = fields + [os.path.basename(f) for f in fields]
                if case_sensitive:
                    if any(fnmatch.fnmatch(f, pat) for f in check):
                        hit = True
                        break
                else:
                    pat_l = pat.lower()
                    if any(fnmatch.fnmatch(f.lower(), pat_l) for f in check):
                        hit = True
                        break
            else:
                # substring
                if case_sensitive:
                    if any(pat in f for f in fields):
                        hit = True
                        break
                else:
                    pl = pat.lower()
                    if any(pl in f.lower() for f in fields):
                        hit = True
                        break
        if hit and name not in seen:
            seen.add(name)
            matched.append(entry)
    return matched


def delete_all_cpa_auth_files_on_cloud(cfg=None, log_callback=None, *, dry_run=True):
    """Delete every on-disk auth JSON on online CPA.

    Uses ``DELETE /v0/management/auth-files?all=true`` when not dry_run
    (server-side bulk delete). Dry-run lists all files first.
    """
    cfg = cfg or config
    log = log_callback or (lambda m: None)

    listed = list_cpa_auth_files_on_cloud(cfg=cfg, log_callback=log)
    if not listed.get("ok"):
        return {
            "ok": False,
            "error": listed.get("error") or "list_failed",
            "status_code": listed.get("status_code"),
            "matched": [],
            "deleted": [],
            "failed": [],
            "dry_run": dry_run,
            "delete_all": True,
        }

    files = listed.get("files") or []
    log(f"[cloud-cpa] delete-all: online files={len(files)} dry_run={dry_run}")
    for e in files:
        log(
            f"  - {e.get('name') or e.get('id')} "
            f"email={e.get('email') or '-'} provider={e.get('provider') or '-'}"
        )

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "delete_all": True,
            "total_online": len(files),
            "matched": files,
            "match_count": len(files),
            "deleted": [],
            "failed": [],
        }

    api_base, headers, timeout, err = _cpa_cloud_mgmt_client(cfg)
    if err:
        return {**err, "delete_all": True, "matched": files, "dry_run": False}

    url = api_base + "/auth-files"
    try:
        res = std_requests.delete(
            url, headers=headers, params={"all": "true"}, timeout=timeout
        )
        preview = response_preview(res, 400)
        if 200 <= res.status_code < 300:
            try:
                data = res.json()
            except Exception:
                data = {"raw": preview}
            deleted_n = data.get("deleted") if isinstance(data, dict) else None
            log(
                f"[cloud-cpa] delete-all OK status={res.status_code}"
                + (f" deleted={deleted_n}" if deleted_n is not None else "")
            )
            return {
                "ok": True,
                "dry_run": False,
                "delete_all": True,
                "total_online": len(files),
                "matched": files,
                "match_count": len(files),
                "deleted_count": deleted_n if deleted_n is not None else len(files),
                "ok_count": deleted_n if deleted_n is not None else len(files),
                "fail_count": 0,
                "deleted": [{"ok": True, "all": True, "response": data}],
                "failed": [],
                "status_code": res.status_code,
                "response": data,
            }
        log(f"[cloud-cpa] delete-all failed: status={res.status_code} body={preview}")
        # Fallback: per-file delete if server rejects all=true
        if res.status_code in (400, 404, 405, 422):
            log("[cloud-cpa] delete-all: falling back to per-file DELETE")
            return delete_cpa_auth_files_on_cloud(
                patterns=None,
                cfg=cfg,
                log_callback=log,
                dry_run=False,
                names=None,
                case_sensitive=False,
                delete_all=True,
                _prelisted_files=files,
            )
        return {
            "ok": False,
            "dry_run": False,
            "delete_all": True,
            "total_online": len(files),
            "matched": files,
            "match_count": len(files),
            "error": preview,
            "status_code": res.status_code,
            "deleted": [],
            "failed": [{"error": preview}],
            "ok_count": 0,
            "fail_count": 1,
        }
    except Exception as exc:
        log(f"[cloud-cpa] delete-all exception: {exc}")
        return {
            "ok": False,
            "dry_run": False,
            "delete_all": True,
            "error": str(exc),
            "matched": files,
            "deleted": [],
            "failed": [{"error": str(exc)}],
        }


def delete_cpa_auth_files_on_cloud(
    patterns=None,
    cfg=None,
    log_callback=None,
    *,
    dry_run=True,
    names=None,
    case_sensitive=False,
    delete_all=False,
    _prelisted_files=None,
):
    """List online auth files, fuzzy-match (or all), optionally delete.

    Args:
      patterns: substring / glob / ``re:regex`` (OR). Fuzzy delete main entry.
      names: exact filenames to delete (optional, OR with patterns).
      delete_all: match every listed file (and prefer bulk API when not dry_run
        via ``delete_all_cpa_auth_files_on_cloud``).
      dry_run: if True, only list matches (default True for safety).

    Returns summary with matched / deleted / failed lists.
    """
    cfg = cfg or config
    log = log_callback or (lambda m: None)

    # Fast path: server-side bulk wipe
    if delete_all and not dry_run and _prelisted_files is None:
        return delete_all_cpa_auth_files_on_cloud(
            cfg=cfg, log_callback=log, dry_run=False
        )
    if delete_all and dry_run and _prelisted_files is None:
        return delete_all_cpa_auth_files_on_cloud(
            cfg=cfg, log_callback=log, dry_run=True
        )

    if _prelisted_files is not None:
        files = list(_prelisted_files)
        listed_ok = True
    else:
        listed = list_cpa_auth_files_on_cloud(cfg=cfg, log_callback=log)
        if not listed.get("ok"):
            return {
                "ok": False,
                "error": listed.get("error") or "list_failed",
                "status_code": listed.get("status_code"),
                "matched": [],
                "deleted": [],
                "failed": [],
                "dry_run": dry_run,
            }
        files = listed.get("files") or []
        listed_ok = True

    matched = []

    if delete_all:
        matched = [e for e in files if isinstance(e, dict)]
    else:
        if names:
            name_set = set()
            for n in names if not isinstance(names, str) else [names]:
                n = os.path.basename(str(n).strip().replace("\\", "/"))
                if n and not n.endswith(".json"):
                    n = n + ".json"
                if n:
                    name_set.add(n.lower() if not case_sensitive else n)
            for entry in files:
                nm = str(entry.get("name") or "")
                key = nm.lower() if not case_sensitive else nm
                if key in name_set:
                    matched.append(entry)

        if patterns:
            matched.extend(
                match_cpa_auth_files(files, patterns, case_sensitive=case_sensitive)
            )

    # dedupe by name
    seen = set()
    uniq = []
    for e in matched:
        nm = str(e.get("name") or e.get("id") or "")
        if nm and nm not in seen:
            seen.add(nm)
            uniq.append(e)
    matched = uniq

    log(
        f"[cloud-cpa] fuzzy match: {len(matched)}/{len(files)} file(s) "
        f"patterns={patterns!r} names={names!r} delete_all={delete_all} dry_run={dry_run}"
    )
    for e in matched:
        log(
            f"  - {e.get('name') or e.get('id')} "
            f"email={e.get('email') or '-'} provider={e.get('provider') or '-'}"
        )

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "delete_all": bool(delete_all),
            "total_online": len(files),
            "matched": matched,
            "deleted": [],
            "failed": [],
            "match_count": len(matched),
        }

    if not matched and not delete_all:
        log("[cloud-cpa] nothing matched; no delete performed")
        return {
            "ok": True,
            "dry_run": False,
            "total_online": len(files),
            "matched": [],
            "match_count": 0,
            "deleted": [],
            "failed": [],
            "ok_count": 0,
            "fail_count": 0,
        }

    deleted = []
    failed = []
    for e in matched:
        nm = str(e.get("name") or "")
        if not nm:
            failed.append({"name": "", "error": "no_name", "entry": e})
            continue
        res = delete_cpa_auth_file_on_cloud(nm, cfg=cfg, log_callback=log)
        if res.get("ok"):
            deleted.append(res)
        else:
            failed.append(res)

    return {
        "ok": len(failed) == 0,
        "dry_run": False,
        "delete_all": bool(delete_all),
        "total_online": len(files),
        "matched": matched,
        "match_count": len(matched),
        "deleted": deleted,
        "failed": failed,
        "ok_count": len(deleted),
        "fail_count": len(failed),
    }


def _resolve_project_path(raw, default_rel="./exports/cpa"):
    text = (raw or default_rel or "").strip() or default_rel
    path = os.path.abspath(os.path.expanduser(text))
    if not os.path.isabs(os.path.expanduser(text)):
        try:
            from grok_register.paths import PROJECT_ROOT

            path = str((PROJECT_ROOT / text).resolve())
        except Exception:
            path = os.path.abspath(text)
    return path


def collect_cpa_auth_files(
    *,
    cpa_dir=None,
    files=None,
    recursive=False,
    pattern="xai-*.json",
    cfg=None,
):
    """Resolve a list of local CPA JSON paths for upload.

    Priority:
      1. explicit ``files`` (paths to .json files, dirs, or globs)
      2. ``cpa_dir`` (batch root, ``…/cpa``, or exports parent)
      3. config ``cpa_auth_dir`` / export parent when recursive

    When ``recursive`` is True, walk subdirs for ``xai-*.json`` (all batches).
    """
    cfg = cfg or config
    import fnmatch
    import glob as _glob

    collected: list[str] = []

    def _add_file(p):
        ap = os.path.abspath(os.path.expanduser(str(p)))
        if not (os.path.isfile(ap) and ap.lower().endswith(".json")):
            return
        base = os.path.basename(ap)
        if base.startswith(".") or base in ("meta.json", "sub2api-accounts.json"):
            return
        if base.startswith("sub2api-"):
            return
        collected.append(ap)

    def _abs(item: str) -> str:
        expanded = os.path.expanduser(str(item))
        if os.path.isabs(expanded):
            return os.path.abspath(expanded)
        try:
            from grok_register.paths import PROJECT_ROOT

            return str((PROJECT_ROOT / expanded).resolve())
        except Exception:
            return os.path.abspath(expanded)

    def _scan_dir(d: str, rec: bool) -> None:
        if not os.path.isdir(d):
            return
        if rec:
            for root, _dirs, names in os.walk(d):
                for name in sorted(names):
                    if fnmatch.fnmatch(name, pattern) or (
                        pattern == "xai-*.json"
                        and name.startswith("xai-")
                        and name.endswith(".json")
                    ):
                        _add_file(os.path.join(root, name))
            return
        # non-recursive: this dir, or batch_dir/cpa
        paths = sorted(_glob.glob(os.path.join(d, pattern)))
        cpa_sub = os.path.join(d, "cpa")
        if not paths and os.path.isdir(cpa_sub):
            paths = sorted(_glob.glob(os.path.join(cpa_sub, pattern)))
        if not paths:
            paths = sorted(
                p
                for p in _glob.glob(os.path.join(d, "*.json"))
                if not os.path.basename(p).startswith(".")
            )
        for p in paths:
            _add_file(p)

    file_list: list = []
    if files:
        file_list = [files] if isinstance(files, (str, bytes)) else list(files)
    for item in file_list:
        if not item:
            continue
        ap = _abs(str(item))
        if os.path.isfile(ap):
            _add_file(ap)
        elif os.path.isdir(ap):
            _scan_dir(ap, recursive)
        else:
            for g in sorted(_glob.glob(ap)):
                if os.path.isfile(g):
                    _add_file(g)
                elif os.path.isdir(g):
                    _scan_dir(g, recursive)

    if not collected:
        if cpa_dir:
            root = _resolve_project_path(cpa_dir, cpa_dir)
        elif recursive:
            root = _resolve_project_path(
                cfg.get("export_batch_parent") or cfg.get("export_root") or "./exports",
                "./exports",
            )
        else:
            root = _resolve_project_path(
                cfg.get("cpa_auth_dir")
                or cfg.get("export_batch_dir")
                or "./exports/cpa",
                "./exports/cpa",
            )
        _scan_dir(root, recursive)

    seen: set[str] = set()
    out: list[str] = []
    for p in collected:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def upload_cpa_auth_dir_to_cloud(
    cpa_dir=None,
    cfg=None,
    log_callback=None,
    force=False,
    pattern="xai-*.json",
    files=None,
    recursive=False,
):
    """Batch-upload local CPA auth JSON files to online CLIProxyAPI.

    Args:
      cpa_dir: directory (batch root, ``…/cpa``, or exports parent)
      files: explicit file paths / globs (takes priority)
      recursive: walk subdirs for ``xai-*.json`` (all batches under exports/)
    """
    cfg = cfg or config
    log = log_callback or (lambda m: None)
    if not force and not cfg.get("cpa_cloud_upload_enabled", False):
        log("[cloud-cpa] batch upload skipped: cpa_cloud_upload_enabled=false")
        return {"ok": False, "skipped": True, "reason": "disabled", "files": []}

    paths = collect_cpa_auth_files(
        cpa_dir=cpa_dir,
        files=files,
        recursive=recursive,
        pattern=pattern,
        cfg=cfg,
    )
    if not paths:
        root = cpa_dir or cfg.get("cpa_auth_dir") or "./exports"
        log(f"[cloud-cpa] batch: no json files under {root} (recursive={recursive})")
        return {
            "ok": True,
            "dir": root,
            "total": 0,
            "ok_count": 0,
            "fail_count": 0,
            "files": [],
        }

    log(f"[cloud-cpa] batch upload {len(paths)} file(s) recursive={recursive}")
    results = []
    ok_count = 0
    fail_count = 0
    for path in paths:
        res = upload_cpa_auth_file_to_cloud(path, cfg=cfg, log_callback=log, force=True)
        results.append(res)
        if res.get("ok"):
            ok_count += 1
        elif res.get("skipped"):
            pass
        else:
            fail_count += 1
    log(f"[cloud-cpa] batch done: ok={ok_count} fail={fail_count} total={len(paths)}")
    return {
        "ok": fail_count == 0 and ok_count > 0,
        "dir": cpa_dir,
        "total": len(paths),
        "ok_count": ok_count,
        "fail_count": fail_count,
        "paths": paths,
        "files": results,
    }


# ── Sub2API online import (v0.1.153+: POST /api/v1/admin/accounts/data) ─────


def normalize_sub2api_cloud_api_base(raw_base):
    """Normalize to Sub2API origin (no trailing /api)."""
    base = (raw_base or "").strip().rstrip("/")
    if not base:
        return ""
    if not re.match(r"^https?://", base, re.I):
        base = "http://" + base
    base = re.sub(r"/api/v1/admin/?$", "", base, flags=re.I)
    base = re.sub(r"/api/v1/?$", "", base, flags=re.I)
    base = re.sub(r"/api/?$", "", base, flags=re.I)
    return base.rstrip("/")


def get_sub2api_cloud_admin_headers(cfg):
    """Return (headers, error_or_None). Prefer admin API key, then JWT."""
    cfg = cfg or {}
    key = (
        os.environ.get("SUB2API_ADMIN_API_KEY")
        or os.environ.get("SUB2API_CLOUD_ADMIN_KEY")
        or str(cfg.get("sub2api_cloud_admin_key") or "")
    ).strip()
    jwt = (
        os.environ.get("SUB2API_JWT")
        or os.environ.get("SUB2API_CLOUD_JWT")
        or str(cfg.get("sub2api_cloud_jwt") or "")
    ).strip()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if key:
        headers["x-api-key"] = key
        return headers, None
    if jwt:
        headers["Authorization"] = "Bearer " + jwt
        return headers, None
    return headers, "missing_admin_key"


def normalize_sub2api_data_document(doc):
    """Ensure payload is a valid sub2api-data document for v0.1.153+."""
    if not isinstance(doc, dict):
        return None, "not_object"
    # Already a full document
    if "accounts" in doc:
        data = dict(doc)
    elif doc.get("type") in ("sub2api-data", "sub2api-bundle"):
        data = dict(doc)
    else:
        # single account object?
        if doc.get("platform") and doc.get("credentials"):
            data = {
                "type": "sub2api-data",
                "version": 1,
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "proxies": [],
                "accounts": [doc],
            }
        else:
            return None, "missing_accounts"

    data_type = str(data.get("type") or "sub2api-data").strip() or "sub2api-data"
    if data_type not in ("sub2api-data", "sub2api-bundle"):
        data_type = "sub2api-data"
    data["type"] = data_type
    try:
        ver = int(data.get("version") or 1)
    except Exception:
        ver = 1
    if ver != 1:
        ver = 1
    data["version"] = ver
    if not data.get("exported_at"):
        data["exported_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # v0.1.153: proxies/accounts must be non-null arrays
    if data.get("proxies") is None:
        data["proxies"] = []
    if not isinstance(data.get("proxies"), list):
        data["proxies"] = []
    if data.get("accounts") is None:
        data["accounts"] = []
    if not isinstance(data.get("accounts"), list):
        return None, "accounts_not_list"
    # light normalize each account
    for acc in data["accounts"]:
        if not isinstance(acc, dict):
            continue
        if str(acc.get("platform") or "").strip().lower() == "xai":
            acc["platform"] = "grok"
        if not acc.get("type"):
            acc["type"] = "oauth"
    return data, None


def upload_sub2api_data_file_to_cloud(path, cfg=None, log_callback=None, force=False):
    """Upload one local Sub2API JSON file to online Sub2API admin data-import.

    Target: POST {api}/api/v1/admin/accounts/data
    Auth: x-api-key (admin) or Authorization Bearer JWT
    Body: { "data": <sub2api-data>, "skip_default_group_bind": bool }
    Compatible with Sub2API ≥ v0.1.153 (explicit skip_default_group_bind).
    """
    cfg = cfg or config
    log = log_callback or (lambda m: None)
    if not force and not cfg.get("sub2api_cloud_upload_enabled", False):
        return {"ok": False, "skipped": True, "reason": "disabled"}
    fpath = os.path.abspath(os.path.expanduser(str(path or "")))
    if not fpath or not os.path.isfile(fpath):
        log(f"[cloud-sub2api] skipped: file not found: {fpath}")
        return {"ok": False, "error": "file_not_found", "path": fpath}
    if not fpath.lower().endswith(".json"):
        return {"ok": False, "error": "not_json", "path": fpath}

    api_base = normalize_sub2api_cloud_api_base(
        cfg.get("sub2api_cloud_api_base")
        or os.environ.get("SUB2API_BASE_URL")
        or os.environ.get("SUB2API_CLOUD_API_BASE")
        or ""
    )
    if not api_base:
        log("[cloud-sub2api] skipped: sub2api_cloud_api_base is empty")
        return {"ok": False, "error": "missing_api_base", "path": fpath}
    headers, auth_err = get_sub2api_cloud_admin_headers(cfg)
    if auth_err:
        log("[cloud-sub2api] skipped: admin key/jwt empty")
        return {"ok": False, "error": auth_err, "path": fpath}

    try:
        with open(fpath, "r", encoding="utf-8-sig") as fh:
            raw_doc = json.load(fh)
    except Exception as exc:
        log(f"[cloud-sub2api] read failed: {exc}")
        return {"ok": False, "error": f"read: {exc}", "path": fpath}

    data_doc, nerr = normalize_sub2api_data_document(raw_doc)
    if nerr or not data_doc:
        log(f"[cloud-sub2api] invalid document: {nerr}")
        return {"ok": False, "error": nerr or "invalid", "path": fpath}

    # v0.1.153: omit → skip bind defaults to true; we always send explicit bool
    skip_bind = bool(cfg.get("sub2api_cloud_skip_default_group_bind", False))
    body = {
        "data": data_doc,
        "skip_default_group_bind": skip_bind,
    }
    url = api_base + "/api/v1/admin/accounts/data"
    timeout = _cpa_cloud_cfg_int(cfg, "sub2api_cloud_timeout", 60, minimum=10, maximum=300)
    retries = _cpa_cloud_cfg_int(cfg, "sub2api_cloud_retries", 3, minimum=1, maximum=10)
    name = os.path.basename(fpath)
    n_acc = len(data_doc.get("accounts") or [])
    # Idempotency: stable-ish per file content + name
    try:
        import hashlib

        raw_bytes = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        idem = hashlib.sha256(raw_bytes).hexdigest()[:32]
        headers = dict(headers)
        headers["Idempotency-Key"] = f"sub2api-import-{idem}"
    except Exception:
        pass

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            res = std_requests.post(url, headers=headers, json=body, timeout=timeout)
            preview = response_preview(res, 400)
            if not (200 <= res.status_code < 300):
                last_error = f"status={res.status_code} body={preview}"
                if res.status_code in (401, 403, 404):
                    log(f"[cloud-sub2api] upload failed (no retry): {last_error}")
                    return {
                        "ok": False,
                        "status_code": res.status_code,
                        "path": fpath,
                        "name": name,
                        "error": preview,
                    }
                if attempt < retries and res.status_code in (408, 429, 500, 502, 503, 504):
                    log(f"[cloud-sub2api] retry {attempt}/{retries}: {last_error}")
                    time.sleep(min(2 * attempt, 8))
                    continue
                log(f"[cloud-sub2api] upload failed: {last_error}")
                return {
                    "ok": False,
                    "status_code": res.status_code,
                    "path": fpath,
                    "name": name,
                    "error": preview,
                }
            try:
                payload = res.json()
            except Exception:
                payload = {"raw": preview}
            # Envelope: {code, message, data: DataImportResult}
            code = payload.get("code") if isinstance(payload, dict) else None
            if code not in (0, "0", None):
                # Some deployments may only return data without code on 200
                msg = (
                    (payload.get("message") if isinstance(payload, dict) else None)
                    or preview
                )
                log(f"[cloud-sub2api] rejected code={code} msg={msg}")
                return {
                    "ok": False,
                    "status_code": res.status_code,
                    "path": fpath,
                    "name": name,
                    "error": msg,
                    "response": payload,
                }
            result_data = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(result_data, dict):
                result_data = {}
            created = int(result_data.get("account_created") or 0)
            failed = int(result_data.get("account_failed") or 0)
            errors = result_data.get("errors") or []
            ok = failed == 0 and (created > 0 or n_acc == 0)
            partial = created > 0 and failed > 0
            log(
                f"[cloud-sub2api] uploaded {name} accounts_in_file={n_acc} "
                f"created={created} failed={failed} skip_default_group_bind={skip_bind}"
            )
            if errors and isinstance(errors, list):
                for err in errors[:10]:
                    if isinstance(err, dict):
                        log(
                            f"  ! {err.get('kind') or 'item'} "
                            f"{err.get('name') or ''}: {err.get('message') or err}"
                        )
            return {
                "ok": ok or partial,
                "partial": partial,
                "status_code": res.status_code,
                "path": fpath,
                "name": name,
                "account_created": created,
                "account_failed": failed,
                "errors": errors,
                "response": payload,
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt < retries:
                log(f"[cloud-sub2api] retry {attempt}/{retries}: {exc}")
                time.sleep(min(2 * attempt, 8))
                continue
            log(f"[cloud-sub2api] exception: {exc}")
            return {"ok": False, "path": fpath, "name": name, "error": str(exc)}
    return {"ok": False, "path": fpath, "name": name, "error": last_error or "unknown"}


def collect_sub2api_export_files(
    *,
    sub2api_dir=None,
    files=None,
    recursive=False,
    cfg=None,
    prefer_combined=True,
):
    """Collect local Sub2API JSON paths for upload.

    Prefers ``sub2api-accounts.json`` (combined) per directory when present.
    """
    cfg = cfg or config
    import glob as _glob

    collected: list[str] = []

    def _abs(item: str) -> str:
        expanded = os.path.expanduser(str(item))
        if os.path.isabs(expanded):
            return os.path.abspath(expanded)
        try:
            from grok_register.paths import PROJECT_ROOT

            return str((PROJECT_ROOT / expanded).resolve())
        except Exception:
            return os.path.abspath(expanded)

    def _add_file(p):
        ap = os.path.abspath(os.path.expanduser(str(p)))
        if not (os.path.isfile(ap) and ap.lower().endswith(".json")):
            return
        base = os.path.basename(ap)
        if base.startswith(".") or base == "meta.json":
            return
        # skip CPA auth files
        if base.startswith("xai-"):
            return
        collected.append(ap)

    def _scan_dir(d: str, rec: bool) -> None:
        if not os.path.isdir(d):
            return
        # batch root → sub2api/
        sub = os.path.join(d, "sub2api")
        roots = []
        if os.path.isdir(sub):
            roots.append(sub)
        roots.append(d)
        if rec:
            for root, _dirs, names in os.walk(d):
                # prefer combined in each dir that has it
                if prefer_combined and "sub2api-accounts.json" in names:
                    _add_file(os.path.join(root, "sub2api-accounts.json"))
                    continue
                for name in sorted(names):
                    if name == "sub2api-accounts.json" or name.startswith("sub2api-"):
                        _add_file(os.path.join(root, name))
            return
        for root in roots:
            combined = os.path.join(root, "sub2api-accounts.json")
            if prefer_combined and os.path.isfile(combined):
                _add_file(combined)
                return
            for p in sorted(_glob.glob(os.path.join(root, "sub2api-*.json"))):
                _add_file(p)
            if os.path.isfile(combined):
                _add_file(combined)

    if files:
        file_list = [files] if isinstance(files, (str, bytes)) else list(files)
        for item in file_list:
            if not item:
                continue
            ap = _abs(str(item))
            if os.path.isfile(ap):
                _add_file(ap)
            elif os.path.isdir(ap):
                _scan_dir(ap, recursive)
            else:
                for g in sorted(_glob.glob(ap)):
                    if os.path.isfile(g):
                        _add_file(g)
                    elif os.path.isdir(g):
                        _scan_dir(g, recursive)

    if not collected:
        if sub2api_dir:
            root = _resolve_project_path(sub2api_dir, sub2api_dir)
        elif recursive:
            root = _resolve_project_path(
                cfg.get("export_batch_parent") or cfg.get("export_root") or "./exports",
                "./exports",
            )
        else:
            root = _resolve_project_path(
                cfg.get("sub2api_export_dir")
                or cfg.get("export_batch_dir")
                or "./exports/sub2api",
                "./exports/sub2api",
            )
        _scan_dir(root, recursive)

    seen = set()
    out = []
    for p in collected:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def upload_sub2api_dir_to_cloud(
    sub2api_dir=None,
    cfg=None,
    log_callback=None,
    force=False,
    files=None,
    recursive=False,
):
    """Batch-upload local Sub2API JSON files to online Sub2API."""
    cfg = cfg or config
    log = log_callback or (lambda m: None)
    if not force and not cfg.get("sub2api_cloud_upload_enabled", False):
        log("[cloud-sub2api] batch skipped: sub2api_cloud_upload_enabled=false")
        return {"ok": False, "skipped": True, "reason": "disabled", "files": []}

    paths = collect_sub2api_export_files(
        sub2api_dir=sub2api_dir,
        files=files,
        recursive=recursive,
        cfg=cfg,
        prefer_combined=True,
    )
    if not paths:
        root = sub2api_dir or cfg.get("sub2api_export_dir") or "./exports"
        log(f"[cloud-sub2api] batch: no json under {root} recursive={recursive}")
        return {
            "ok": True,
            "dir": root,
            "total": 0,
            "ok_count": 0,
            "fail_count": 0,
            "files": [],
        }

    log(f"[cloud-sub2api] batch upload {len(paths)} file(s) recursive={recursive}")
    results = []
    ok_count = 0
    fail_count = 0
    created_total = 0
    failed_acc_total = 0
    for path in paths:
        res = upload_sub2api_data_file_to_cloud(
            path, cfg=cfg, log_callback=log, force=True
        )
        results.append(res)
        if res.get("ok"):
            ok_count += 1
            created_total += int(res.get("account_created") or 0)
            failed_acc_total += int(res.get("account_failed") or 0)
        elif res.get("skipped"):
            pass
        else:
            fail_count += 1
    log(
        f"[cloud-sub2api] batch done: files_ok={ok_count} files_fail={fail_count} "
        f"accounts_created={created_total} accounts_failed={failed_acc_total}"
    )
    return {
        "ok": fail_count == 0 and ok_count > 0,
        "dir": sub2api_dir,
        "total": len(paths),
        "ok_count": ok_count,
        "fail_count": fail_count,
        "account_created": created_total,
        "account_failed": failed_acc_total,
        "paths": paths,
        "files": results,
    }


def run_cpa_and_sub2api_export(email, password, sso, log_callback=None):
    """Mint CPA xAI auth and convert it to Sub2API JSON after a successful registration."""
    log = log_callback or (lambda m: None)
    if not config.get("cpa_export_enabled", True):
        log("[cpa] export disabled")
        return {"ok": False, "skipped": True, "reason": "disabled"}
    try:
        import grok_register.export.cpa_export as cpa_export

        page_obj = page
        cookies = None
        try:
            cookies = cpa_export.export_cookies_from_page(page_obj)
        except Exception:
            cookies = None
        # The registration page is no longer needed once SSO/password/cookies are
        # captured. Close it before CPA mint so each account cleans its browser
        # profile instead of leaving the success page open during OIDC export.
        try:
            stop_browser()
            log("[*] 注册浏览器已关闭并清理痕迹，开始 CPA/Sub2API 导出")
        except Exception as close_exc:
            log(f"[Debug] 注册浏览器关闭失败: {close_exc}")
        result = cpa_export.export_cpa_xai_for_account(
            email,
            password,
            page=None,
            cookies=cookies,
            sso=sso,
            config=config,
            log_callback=log,
        )
        if result.get("ok"):
            log(f"[cpa] auth -> {result.get('path')}")
            # Cloud upload is done inside export_cpa_xai_for_account when enabled.
            cloud_res = result.get("cloud_cpa_upload")
            if cloud_res is None and config.get("cpa_cloud_upload_enabled", False):
                cloud_path = result.get("cpa_path") or result.get("path")
                cloud_res = upload_cpa_auth_file_to_cloud(cloud_path, config, log)
                result["cloud_cpa_upload"] = cloud_res
            elif cloud_res and cloud_res.get("ok"):
                log(f"[cloud-cpa] online import ok: {cloud_res.get('name')}")
            sub = result.get("sub2api") or {}
            if sub.get("ok"):
                log(f"[sub2api] json -> {sub.get('combined_path') or sub.get('path')}")
            elif result.get("sub2api_error"):
                log(f"[sub2api] export failed: {result.get('sub2api_error')}")
        else:
            log(f"[cpa] auth 未成功: {result.get('error') or result}")
        return result
    except Exception as exc:
        log(f"[cpa/sub2api] export exception: {exc}")
        if config.get("cpa_mint_required", False):
            raise
        return {"ok": False, "error": str(exc)}
    finally:
        # CPA mint uses its own standalone Chromium.  Do not leave that page open
        # after a registration; otherwise it looks like the registrar never
        # closes/cleans up even though the main register browser is stopped.
        try:
            from grok_register.export.cpa_xai.browser_confirm import shutdown_mint_browsers

            shutdown_mint_browsers()
            log("[cpa] mint browser closed")
        except Exception as cleanup_exc:
            log(f"[cpa] mint browser cleanup skipped: {cleanup_exc}")

class CliStopController:
    def __init__(self):
        self.stop_requested = False

    def should_stop(self):
        return self.stop_requested

    def stop(self):
        self.stop_requested = True


def cli_log(message):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    safe_print(f"[{timestamp}] {message}", flush=True)


def run_registration_cli(count):
    from grok_register import RegistrationEngine

    controller = CliStopController()
    success_count = 0
    fail_count = 0
    retry_count_for_slot = 0
    max_slot_retry = 3
    accounts_output_file = os.path.join(
        os.path.dirname(__file__),
        f"accounts_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
    )
    cli_log(f"[*] 终端模式启动，目标数量: {count}")
    cli_log(f"[*] 成功账号将实时保存到: {accounts_output_file}")
    cli_log("[*] RegistrationEngine (browser)")
    engine = RegistrationEngine(config=config, reg_module=sys.modules[__name__])
    try:
        start_browser(log_callback=cli_log)
        cli_log("[*] 浏览器已启动")
        i = 0
        while i < count:
            if controller.should_stop():
                break
            cli_log(f"--- 开始第 {i + 1}/{count} 个账号 ---")
            try:
                result = engine.register_one(
                    log_callback=cli_log,
                    cancel_callback=controller.should_stop,
                    side_effect_profile="single_cli",
                    accounts_file=None,
                    run_side_effects=False,
                    max_mail_retry=get_max_mail_retry(),
                )
                if not result.ok or not str(result.sso or "").strip():
                    raise Exception(
                        f"{result.failure_reason or 'fail'}: {result.error or 'unknown'}"
                    )
                email = result.email
                sso = result.sso
                profile = dict(result.profile or {})
                password = result.password or profile.get("password", "") or ""
                if config.get("enable_nsfw", True):
                    cli_log("[*] 6. 开启 NSFW")
                    nsfw_ok, nsfw_msg = enable_nsfw_for_token(
                        sso,
                        log_callback=cli_log,
                        page=get_page(),
                        cookies=getattr(result, "cookies", None),
                    )
                    if nsfw_ok:
                        cli_log(f"[+] NSFW 开启成功: {nsfw_msg}")
                    else:
                        cli_log(
                            f"[!] NSFW 未开启（账号已保存，可稍后手动开）: {nsfw_msg}"
                        )
                try:
                    line = f"{email}----{password}----{sso}\n"
                    with open(accounts_output_file, "a", encoding="utf-8") as f:
                        f.write(line)
                except Exception as file_exc:
                    cli_log(f"[Debug] 保存账号文件失败: {file_exc}")
                add_token_to_grok2api_pools(sso, email=email, log_callback=cli_log)
                run_cpa_and_sub2api_export(
                    email,
                    password,
                    sso,
                    log_callback=cli_log,
                )
                success_count += 1
                retry_count_for_slot = 0
                i += 1
                cli_log(f"[+] 注册成功: {email} via browser")
                cli_log(f"[*] 当前统计: 成功 {success_count} | 失败 {fail_count}")
                if success_count > 0 and success_count % MEMORY_CLEANUP_INTERVAL == 0 and i < count:
                    cleanup_runtime_memory(
                        log_callback=cli_log,
                        reason=f"已成功 {success_count} 个账号，执行定期清理",
                    )
            except RegistrationCancelled:
                cli_log("[!] 注册被停止")
                break
            except AccountRetryNeeded as exc:
                retry_count_for_slot += 1
                if retry_count_for_slot <= max_slot_retry:
                    cli_log(
                        f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                    )
                else:
                    fail_count += 1
                    retry_count_for_slot = 0
                    i += 1
                    cli_log(f"[-] 当前账号已达到最大重试次数，跳过: {exc}")
            except Exception as exc:
                fail_count += 1
                retry_count_for_slot = 0
                i += 1
                cli_log(f"[-] 注册失败: {exc}")
            finally:
                if controller.should_stop() or i >= count:
                    break
                if get_browser_obj() is None:
                    start_browser(log_callback=cli_log)
                else:
                    restart_browser(log_callback=cli_log)
                sleep_with_cancel(1, controller.should_stop)
    except KeyboardInterrupt:
        controller.stop()
        cli_log("[!] 收到 Ctrl+C，正在停止并清理")
    except Exception as exc:
        cli_log(f"[!] 任务异常: {exc}")
    finally:
        cleanup_runtime_memory(log_callback=cli_log, reason="任务结束")
        cli_log(f"[*] 任务结束。成功 {success_count} | 失败 {fail_count}")


def main_cli():
    load_config()
    count = int(config.get("register_count", 1) or 1)
    cli_log("[*] CLI 已加载配置")
    cli_log(
        f"[*] 当前邮箱服务商: {config.get('email_provider', 'duckmail')} | "
        f"注册数量: {count} | registration=browser"
    )
    cli_log("[*] 输入 start 后开始；按 Ctrl+C 可强制停止")
    try:
        command = input("> ").strip().lower()
    except KeyboardInterrupt:
        cli_log("[!] 已取消")
        return
    if command != "start":
        cli_log("[!] 未输入 start，已退出")
        return
    run_registration_cli(count)


def main():
    """CLI-only entry. Prefer: python register_cli.py --extra N --threads 1 --headed"""
    arg = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
    if arg in ("-h", "--help", "help"):
        safe_print(
            "Grok register core (no GUI).\n"
            "  python register_cli.py --extra N --threads 1 --headed\n"
            "  python -m grok_register.core cli\n",
            flush=True,
        )
        return
    main_cli()


if __name__ == "__main__":
    main()
