"""High-level: mint CPA xai-*.json for one free registered account."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .browser_confirm import mint_with_browser
from .probe import probe_mini_response, probe_models
from .proxyutil import proxy_log_label, resolve_proxy, set_runtime_proxy
from .schema import DEFAULT_BASE_URL, build_cpa_xai_auth
from .writer import write_cpa_xai_auth

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def mint_and_export(
    *,
    email: str,
    password: str,
    auth_dir: str | Path,
    page: Any | None = None,
    proxy: str | None = None,
    headless: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    probe: bool = True,
    probe_chat: bool = False,
    browser_timeout_sec: float = 240.0,
    force_standalone: bool = True,
    cookies: Any | None = None,
    reuse_browser: bool = True,
    recycle_every: int = 15,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Full pipeline: device-auth → write CPA file → optional probe.

    Returns dict with keys: ok, path, email, probe, error?
    """
    log = log or _noop
    email = (email or "").strip()
    if not email or not password:
        return {"ok": False, "email": email, "error": "missing email/password"}

    # Config/explicit proxy wins over shell https_proxy (common 7890 trap).
    # Thread-local pin — safe under concurrent mint workers.
    # Always pin (including "") so urllib never falls back to macOS system proxy.
    resolved = resolve_proxy("" if proxy is None else proxy)
    set_runtime_proxy(resolved)  # pin direct as "" when empty
    log(f"mint start: {email} proxy={proxy_log_label(resolved) or '(none)'}")
    try:
        tokens = mint_with_browser(
            email=email,
            password=password,
            page=None if force_standalone else page,
            proxy=resolved,  # pass "" not None so opener stays direct
            headless=headless,
            browser_timeout_sec=browser_timeout_sec,
            force_standalone=force_standalone,
            cookies=cookies,
            reuse_browser=reuse_browser,
            recycle_every=recycle_every,
            poll_log=log,
            cancel=cancel,
        )
    except Exception as e:  # noqa: BLE001
        err = str(e)
        err_l = err.lower()
        # Transient network / SSL / tunnel: one more full mint attempt
        if any(
            m in err_l
            for m in (
                "tunnel connection failed",
                "503 service unavailable",
                "proxy error",
                "unexpected_eof",
                "ssl",
                "timed out",
                "connection reset",
                "temporarily unavailable",
            )
        ):
            log(f"mint failed (will retry once): {e}")
            try:
                time.sleep(2.0)
                set_runtime_proxy("")
                tokens = mint_with_browser(
                    email=email,
                    password=password,
                    page=None if force_standalone else page,
                    proxy="",
                    headless=headless,
                    browser_timeout_sec=browser_timeout_sec,
                    force_standalone=force_standalone,
                    cookies=cookies,
                    reuse_browser=False,  # fresh browser after SSL/CF mess
                    recycle_every=recycle_every,
                    poll_log=log,
                    cancel=cancel,
                )
            except Exception as e2:  # noqa: BLE001
                log(f"mint failed: {e2}")
                return {"ok": False, "email": email, "error": str(e2)}
        else:
            log(f"mint failed: {e}")
            return {"ok": False, "email": email, "error": str(e)}

    payload = build_cpa_xai_auth(
        email=email,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        id_token=tokens.get("id_token"),
        expires_in=tokens.get("expires_in"),
        base_url=base_url,
    )
    path = write_cpa_xai_auth(auth_dir, payload)
    log(f"wrote {path}")

    result: dict[str, Any] = {
        "ok": True,
        "email": email,
        "path": str(path),
        "user_code": tokens.get("user_code"),
        "base_url": base_url,
        "proxy": proxy_log_label(resolved),
    }

    if probe:
        pr = probe_models(tokens["access_token"], base_url=base_url, proxy=resolved)
        result["probe_models"] = pr
        log(f"probe models: ok={pr.get('ok')} has_grok_45={pr.get('has_grok_45')} ids={pr.get('model_ids')}")
        if not pr.get("has_grok_45"):
            result["ok"] = False
            result["error"] = "token ok but grok-4.5 not listed"
        if probe_chat and pr.get("has_grok_45"):
            ch = probe_mini_response(
                tokens["access_token"], base_url=base_url, proxy=resolved
            )
            result["probe_chat"] = ch
            log(f"probe chat: ok={ch.get('ok')} model={ch.get('model')} text={ch.get('text')!r}")
            if not ch.get("ok"):
                result["ok"] = False
                result["error"] = f"chat probe failed: {ch.get('error') or ch.get('status')}"
    return result
