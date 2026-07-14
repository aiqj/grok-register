"""Register-machine hook: mint CPA xai auth after successful registration.

OIDC package lives at grok_register.export.cpa_xai.
Optional override: config `api_reverse_tools` / env `API_REVERSE_TOOLS`
points at a directory that *contains* the `cpa_xai` package.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

from grok_register.paths import PROJECT_ROOT

_REG_DIR = PROJECT_ROOT  # project root (src layout)
# Unified layout: exports/cpa/*.json  and  exports/sub2api/*.json
_DEFAULT_EXPORT_ROOT = _REG_DIR / "exports"
_DEFAULT_OUT = _DEFAULT_EXPORT_ROOT / "cpa"
_DEFAULT_CPA = Path("")  # empty = do not assume a machine-local CPA path


def resolve_export_dirs(config: dict | None = None) -> tuple[Path, Path, Path]:
    """Return (export_root, cpa_dir, sub2api_dir) under the register project.

    Flat layout (legacy / ``export_batch_enabled=false``)::

        exports/
          cpa/
          sub2api/

    Per-batch layout (default when CLI creates a run batch)::

        exports/
          20260712_153045/
            accounts.txt
            cpa/
            sub2api/
            meta.json

    When ``cpa_auth_dir`` / ``sub2api_export_dir`` are set, they win over
    ``export_root/cpa`` defaults (batch setup writes these explicitly).
    """
    cfg = config or {}
    root_raw = str(cfg.get("export_root") or "./exports").strip() or "./exports"
    root = Path(root_raw).expanduser()
    if not root.is_absolute():
        root = (_REG_DIR / root).resolve()
    else:
        root = root.resolve()

    cpa_raw = str(cfg.get("cpa_auth_dir") or "").strip()
    if cpa_raw:
        cpa = Path(cpa_raw).expanduser()
        cpa = cpa if cpa.is_absolute() else (_REG_DIR / cpa).resolve()
    else:
        cpa = (root / "cpa").resolve()

    sub_raw = str(cfg.get("sub2api_export_dir") or "").strip()
    if sub_raw:
        sub = Path(sub_raw).expanduser()
        sub = sub if sub.is_absolute() else (_REG_DIR / sub).resolve()
    else:
        sub = (root / "sub2api").resolve()

    return root, cpa, sub


def make_batch_id(name: str | None = None) -> str:
    """Build a filesystem-safe batch id: ``YYYYMMDD_HHMMSS[_name]``."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    extra = (name or "").strip()
    if not extra:
        return stamp
    # keep alnum, dash, underscore only
    safe = re.sub(r"[^\w.\-]+", "_", extra, flags=re.UNICODE).strip("._-")
    safe = safe[:64] if safe else ""
    return f"{stamp}_{safe}" if safe else stamp


_BATCH_ID_RE = re.compile(r"^\d{8}_\d{6}(?:_.+)?$")
_SKIP_EXPORT_NAMES = frozenset({"cpa", "sub2api", "cpa_auths", ".git", "__pycache__"})


def _export_parent_path(config: dict | None = None, parent: str | Path | None = None) -> Path:
    cfg = config or {}
    parent_raw = parent
    if parent_raw is None:
        parent_raw = cfg.get("export_batch_parent") or cfg.get("export_root") or "./exports"
    parent_path = Path(str(parent_raw)).expanduser()
    if not parent_path.is_absolute():
        parent_path = (_REG_DIR / parent_path).resolve()
    else:
        parent_path = parent_path.resolve()
    return parent_path


def _is_batch_dir(path: Path) -> bool:
    """True if dir looks like a registration batch (timestamp name or has cpa/meta)."""
    if not path.is_dir():
        return False
    name = path.name
    if name in _SKIP_EXPORT_NAMES or name.startswith("."):
        return False
    if _BATCH_ID_RE.match(name):
        return True
    if (path / "meta.json").is_file():
        return True
    if (path / "cpa").is_dir():
        return True
    if (path / "accounts.txt").is_file():
        return True
    return False


def _batch_sort_key(path: Path) -> float:
    """Prefer newest CPA file mtime, else directory mtime."""
    cpa = path / "cpa"
    latest = 0.0
    scan = cpa if cpa.is_dir() else path
    try:
        for p in scan.glob("xai-*.json"):
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    if latest > 0:
        return latest
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def find_latest_export_batch(
    config: dict | None = None,
    *,
    parent: str | Path | None = None,
    require_cpa_files: bool = True,
) -> Path | None:
    """Return the newest registration batch directory under exports parent.

    Batch dirs look like ``exports/20260712_153045/`` (optional ``_name`` suffix),
    or any subdir with ``cpa/`` / ``meta.json`` / ``accounts.txt``.
    Skips flat ``exports/cpa`` and ``exports/sub2api``.

    When ``require_cpa_files`` is True, only batches that contain at least one
    ``xai-*.json`` (under ``cpa/`` or the batch root) are considered.
    """
    parent_path = _export_parent_path(config, parent)
    if not parent_path.is_dir():
        return None

    candidates: list[Path] = []
    try:
        children = list(parent_path.iterdir())
    except OSError:
        return None

    for child in children:
        if not _is_batch_dir(child):
            continue
        if require_cpa_files:
            cpa_dir = child / "cpa"
            has = False
            try:
                if cpa_dir.is_dir() and any(cpa_dir.glob("xai-*.json")):
                    has = True
                elif any(child.glob("xai-*.json")):
                    has = True
            except OSError:
                has = False
            if not has:
                continue
        candidates.append(child)

    if not candidates:
        return None
    candidates.sort(key=_batch_sort_key, reverse=True)
    return candidates[0]


def apply_batch_export_layout(
    config: dict,
    *,
    batch_name: str | None = None,
    batch_id: str | None = None,
    parent: str | Path | None = None,
    also_global_accounts: bool | None = None,
) -> dict[str, Any]:
    """Create one batch subdirectory and rewrite config export paths into it.

    Layout::

        {parent}/{batch_id}/
          accounts.txt
          cpa/
          sub2api/
          meta.json

    Mutates ``config`` in place. Returns info dict with paths.
    """
    import json as _json

    cfg = config
    parent_raw = parent
    if parent_raw is None:
        parent_raw = cfg.get("export_batch_parent") or cfg.get("export_root") or "./exports"
    parent_path = Path(str(parent_raw)).expanduser()
    if not parent_path.is_absolute():
        parent_path = (_REG_DIR / parent_path).resolve()
    else:
        parent_path = parent_path.resolve()

    bid = (batch_id or "").strip() or make_batch_id(batch_name)
    batch_dir = (parent_path / bid).resolve()
    cpa_dir = batch_dir / "cpa"
    sub_dir = batch_dir / "sub2api"
    accounts_path = batch_dir / "accounts.txt"
    meta_path = batch_dir / "meta.json"

    batch_dir.mkdir(parents=True, exist_ok=True)
    cpa_dir.mkdir(parents=True, exist_ok=True)
    sub_dir.mkdir(parents=True, exist_ok=True)
    if not accounts_path.is_file():
        accounts_path.write_text("", encoding="utf-8")

    # Point all export knobs at this batch (relative paths ok for readability).
    cfg["export_root"] = str(batch_dir)
    cfg["cpa_auth_dir"] = str(cpa_dir)
    cfg["sub2api_export_dir"] = str(sub_dir)
    cfg["sub2api_combined_file"] = str(sub_dir / "sub2api-accounts.json")
    cfg["export_batch_id"] = bid
    cfg["export_batch_dir"] = str(batch_dir)
    cfg["export_batch_accounts_file"] = str(accounts_path)

    if also_global_accounts is None:
        also_global_accounts = bool(cfg.get("export_batch_also_global_accounts", True))
    cfg["export_batch_also_global_accounts"] = also_global_accounts

    meta = {
        "batch_id": bid,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "batch_dir": str(batch_dir),
        "cpa_dir": str(cpa_dir),
        "sub2api_dir": str(sub_dir),
        "accounts_file": str(accounts_path),
    }
    try:
        meta_path.write_text(_json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass

    return meta


def _ensure_cpa_xai_on_path(tools_dir: str | Path | None = None) -> Path:
    """Put the parent of `cpa_xai` on sys.path. Default: this project root."""
    if tools_dir:
        tools = Path(tools_dir).expanduser().resolve()
    else:
        env = (os.environ.get("API_REVERSE_TOOLS") or "").strip()
        tools = Path(env).expanduser().resolve() if env else _REG_DIR
    # If user pointed at .../cpa_xai itself, use its parent
    if tools.name == "cpa_xai" and (tools / "__init__.py").is_file():
        tools = tools.parent
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    return tools


def export_cookies_from_page(page: Any) -> list[dict]:
    """Best-effort export of cookies from a DrissionPage tab/browser."""
    if page is None:
        return []
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
    if not cookies:
        try:
            browser = getattr(page, "browser", None)
            if browser is not None:
                cookies = browser.cookies()
        except Exception:
            cookies = None
    if isinstance(cookies, list):
        return [c for c in cookies if isinstance(c, dict)]
    return []


def export_cpa_xai_for_account(
    email: str,
    password: str,
    *,
    page: Any | None = None,
    cookies: Any | None = None,
    sso: str | None = None,
    config: dict | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Mint OIDC + write xai-<email>.json under exports/cpa (and optional CPA hotload dir)."""
    cfg = config or {}
    log = log_callback or (lambda m: print(m, flush=True))

    if not cfg.get("cpa_export_enabled", True):
        log("[cpa] export disabled")
        return {"ok": False, "skipped": True, "reason": "disabled"}

    tools_dir = cfg.get("api_reverse_tools") or cfg.get("cpa_xai_parent") or None
    _ensure_cpa_xai_on_path(tools_dir)

    try:
        from grok_register.export.cpa_xai import mint_and_export  # type: ignore
    except Exception as e:  # noqa: BLE001
        log(f"[cpa] import grok_register.export.cpa_xai failed: {e}")
        return {"ok": False, "error": f"import: {e}"}

    _root, out_dir, _sub = resolve_export_dirs(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)

    hotload_raw = (cfg.get("cpa_hotload_dir") or "").strip()
    cpa_dir = Path(hotload_raw).expanduser() if hotload_raw else None
    if cpa_dir and not cpa_dir.is_absolute():
        cpa_dir = (_REG_DIR / cpa_dir).resolve()

    # Proxy is optional (empty = direct).
    # Priority: cpa_proxy > thread sticky / PROXY|PROXY_POOL / config.proxy.
    # Does not silently inherit shell https_proxy unless use_system_proxy.
    proxy = (cfg.get("cpa_proxy") or "").strip()
    if not proxy:
        try:
            from grok_register.proxy.pool import get_thread_proxy, resolve_proxy_list

            proxy = (get_thread_proxy(config=cfg) or "").strip()
            if not proxy:
                lst = resolve_proxy_list(cfg)
                proxy = lst[0] if lst else ""
        except Exception:
            proxy = (cfg.get("proxy") or "").strip()
            if not proxy:
                proxy = (os.environ.get("PROXY") or os.environ.get("PROXY_POOL") or "").strip()
                if "," in proxy or ";" in proxy:
                    proxy = proxy.split(",")[0].split(";")[0].strip()
    # Headless: explicit cpa_headless > shared headless. Mint is CF-sensitive;
    # warm register browser path ignores standalone headless cold-start issues.
    # Note: remint / --headed sets cpa_headless=False so cold mint is visible.
    if "cpa_headless" in cfg and cfg.get("cpa_headless") is not None:
        headless = bool(cfg.get("cpa_headless"))
    elif "headless" in cfg and cfg.get("headless") is not None:
        headless = bool(cfg.get("headless"))
    else:
        try:
            from grok_register.proxy.pool import resolve_headless

            headless = resolve_headless(cfg)
        except Exception:
            headless = bool(cfg.get("cpa_headless", True))
    # Desktop remint safety: never cold-mint headless when user asked headed.
    if str(os.environ.get("HEADLESS", "")).strip() in ("0", "false", "False", "no"):
        headless = False
    probe = bool(cfg.get("cpa_probe_after_write", True))
    probe_chat = bool(cfg.get("cpa_probe_chat", False))
    timeout = float(cfg.get("cpa_mint_timeout_sec", 240))
    base_url = cfg.get("cpa_base_url") or "https://cli-chat-proxy.grok.com/v1"
    # Prefer reusing the just-registered warm browser (cookies+CF clearance).
    prefer_warm = bool(cfg.get("cpa_mint_prefer_warm_browser", True))
    force_standalone = bool(cfg.get("cpa_force_standalone", True))
    if prefer_warm and page is not None:
        force_standalone = False
        # Warm tab is already the register browser (usually headed). Do not
        # relaunch a second headless Chromium for the same job.
        headless = False
        log("[cpa] prefer warm register browser for mint (force_standalone=false)")
    elif force_standalone and headless:
        log(
            "[cpa] WARN standalone+headless mint is CF-prone; "
            "prefer --headed or warm register browser"
        )
    cookie_inject = bool(cfg.get("cpa_mint_cookie_inject", True))
    # Warm path: never reuse mint-thread pool browser; use the register tab.
    reuse_browser = bool(cfg.get("cpa_mint_browser_reuse", True))
    if not force_standalone and page is not None:
        reuse_browser = False
    recycle_every = int(cfg.get("cpa_mint_browser_recycle_every", 15) or 0)

    # cookies: explicit arg > page export > none
    use_cookies = cookies
    sso_val = (sso or "").strip()
    if use_cookies is None and cookie_inject and page is not None:
        use_cookies = export_cookies_from_page(page)
    if not cookie_inject:
        use_cookies = None
    else:
        # Always attach SSO cookie clones — register cookies alone often miss accounts.x.ai host
        if not sso_val and isinstance(use_cookies, list):
            for c in use_cookies:
                if isinstance(c, dict) and c.get("name") in ("sso", "sso-rw") and c.get("value"):
                    sso_val = str(c.get("value"))
                    break
        if sso_val:
            base = list(use_cookies) if isinstance(use_cookies, list) else []
            for name in ("sso", "sso-rw"):
                for dom in (".x.ai", "accounts.x.ai", ".accounts.x.ai", "auth.x.ai", "grok.com", ".grok.com"):
                    base.append({
                        "name": name,
                        "value": sso_val,
                        "domain": dom,
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    })
            use_cookies = base

    out_dir.mkdir(parents=True, exist_ok=True)
    log(
        f"[cpa] mint OIDC for {email} -> {out_dir} proxy={proxy or '(none)'} "
        f"cookies={len(use_cookies) if isinstance(use_cookies, list) else (1 if use_cookies else 0)} "
        f"reuse={reuse_browser}"
    )

    def _log(msg: str) -> None:
        log(f"[cpa] {msg}")

    log(
        f"[cpa] force_standalone={force_standalone} headless={headless} "
        f"prefer_warm={prefer_warm} has_page={page is not None}"
    )
    result = mint_and_export(
        email=email,
        password=password,
        auth_dir=out_dir,
        page=None if force_standalone else page,
        proxy=proxy if proxy else "",
        headless=headless,
        base_url=base_url,
        probe=probe,
        probe_chat=probe_chat,
        browser_timeout_sec=timeout,
        force_standalone=force_standalone,
        cookies=use_cookies,
        reuse_browser=reuse_browser,
        recycle_every=recycle_every,
        sso=sso_val,
        prefer_sso_build=bool(cfg.get("cpa_mint_prefer_sso_build", True)),
        log=_log,
    )

    if result.get("ok") and result.get("path") and cfg.get("cpa_copy_to_hotload", False) and cpa_dir:
        try:
            cpa_dir.mkdir(parents=True, exist_ok=True)
            src = Path(result["path"])
            dst = cpa_dir / src.name
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
            result["cpa_path"] = str(dst)
            log(f"[cpa] hotload copy -> {dst}")
        except Exception as e:  # noqa: BLE001
            log(f"[cpa] hotload copy failed: {e}")
            result["cpa_copy_error"] = str(e)

    # failure log under register dir
    if not result.get("ok"):
        fail_path = out_dir / "cpa_auth_failed.txt"
        with open(fail_path, "a", encoding="utf-8") as f:
            f.write(f"{email}----{result.get('error') or 'unknown'}----{int(time.time())}\n")
        log(f"[cpa] FAILED: {result.get('error') or 'unknown'} (see {fail_path})")
        if cfg.get("cpa_mint_required", False):
            raise RuntimeError(f"CPA mint required but failed: {result.get('error')}")
    else:
        log(f"[cpa] OK path={result.get('path')}")
        # Always try Sub2API after successful CPA (both formats)
        if result.get("path") and cfg.get("sub2api_export_enabled", True):
            try:
                import grok_register.export.cpa_to_sub2api as cpa_to_sub2api

                sub_res = cpa_to_sub2api.export_after_cpa_result(
                    result,
                    config=cfg,
                    log_callback=log,
                )
                result["sub2api"] = sub_res
                if sub_res.get("ok"):
                    log(
                        f"[sub2api] OK single={sub_res.get('path')} "
                        f"combined={sub_res.get('combined_path')}"
                    )
                    # Online Sub2API: prefer single-account file just written
                    if cfg.get("sub2api_cloud_upload_enabled", False):
                        try:
                            from grok_register.core import upload_sub2api_data_file_to_cloud

                            up_path = sub_res.get("path") or sub_res.get("combined_path")
                            cloud_s2 = upload_sub2api_data_file_to_cloud(
                                up_path, cfg=cfg, log_callback=log
                            )
                            result["cloud_sub2api_upload"] = cloud_s2
                            if cloud_s2.get("ok"):
                                log(
                                    f"[cloud-sub2api] online import ok: "
                                    f"created={cloud_s2.get('account_created')} "
                                    f"failed={cloud_s2.get('account_failed')}"
                                )
                            elif cloud_s2.get("skipped"):
                                log(f"[cloud-sub2api] skipped: {cloud_s2.get('reason')}")
                            else:
                                log(
                                    f"[cloud-sub2api] online import failed: "
                                    f"{cloud_s2.get('error') or cloud_s2}"
                                )
                        except Exception as e:  # noqa: BLE001
                            log(f"[cloud-sub2api] upload exception: {e}")
                            result["cloud_sub2api_upload"] = {"ok": False, "error": str(e)}
                else:
                    log(f"[sub2api] not ok: {sub_res}")
            except Exception as e:  # noqa: BLE001
                log(f"[sub2api] export failed: {e}")
                result["sub2api_error"] = str(e)
        elif result.get("path") and not cfg.get("sub2api_export_enabled", True):
            log("[sub2api] export disabled (sub2api_export_enabled=false)")

        # Online CLIProxyAPI: POST /v0/management/auth-files (when enabled)
        if result.get("path") and cfg.get("cpa_cloud_upload_enabled", False):
            try:
                from grok_register.core import upload_cpa_auth_file_to_cloud

                cloud_path = result.get("cpa_path") or result.get("path")
                cloud_res = upload_cpa_auth_file_to_cloud(
                    cloud_path, cfg=cfg, log_callback=log
                )
                result["cloud_cpa_upload"] = cloud_res
                if cloud_res.get("ok"):
                    log(f"[cloud-cpa] online import ok: {cloud_res.get('name')}")
                elif cloud_res.get("skipped"):
                    log(f"[cloud-cpa] skipped: {cloud_res.get('reason')}")
                else:
                    log(
                        f"[cloud-cpa] online import failed: "
                        f"{cloud_res.get('error') or cloud_res}"
                    )
            except Exception as e:  # noqa: BLE001
                log(f"[cloud-cpa] upload exception: {e}")
                result["cloud_cpa_upload"] = {"ok": False, "error": str(e)}

    return result
