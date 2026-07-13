"""xAI OAuth device-code grant (Grok CLI / CPA client).

Endpoints from https://auth.x.ai/.well-known/openid-configuration
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .proxyutil import build_opener

# Keep in sync with CLIProxyAPI internal/auth/xai/types.go
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
ISSUER = "https://auth.x.ai"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
SCOPE = "openid profile email offline_access grok-cli:access api:access"

LogFn = Callable[[str], None]


def _noop_log(_: str) -> None:
    return None


def _opener(proxy: str | None = None) -> urllib.request.OpenerDirector:
    # Must use build_opener from proxyutil so direct mode disables macOS system proxy.
    return build_opener(proxy)


def _is_transient_network_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        m in msg
        for m in (
            "unexpected_eof",
            "eof occurred",
            "ssl",
            "timed out",
            "timeout",
            "connection reset",
            "connection aborted",
            "temporarily unavailable",
            "network is unreachable",
            "name resolution",
            "broken pipe",
            "remote end closed",
            "tunnel connection failed",
            "503",
        )
    )


def _post_form_urllib(
    url: str,
    form: dict[str, str],
    timeout: float,
    *,
    proxy: str | None = None,
) -> tuple[int, dict[str, Any] | str]:
    data = urllib.parse.urlencode(form).encode("utf-8")
    # Fresh Request every call — some TLS failures leave Request unusable
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "grok-reg-cpa-xai-minter/1.0",
            "Connection": "close",
        },
    )
    opener = _opener(proxy)
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200) or 200
            try:
                return int(status), json.loads(body)
            except json.JSONDecodeError:
                return int(status), body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return int(e.code), json.loads(body)
        except json.JSONDecodeError:
            return int(e.code), body


def _post_form_curl_cffi(
    url: str,
    form: dict[str, str],
    timeout: float,
    *,
    proxy: str | None = None,
) -> tuple[int, dict[str, Any] | str]:
    """TLS-fingerprint friendly fallback — urllib on macOS often hits SSL EOF to auth.x.ai."""
    from curl_cffi import requests as curl_requests

    proxies = None
    p = (proxy or "").strip()
    if p:
        proxies = {"http": p, "https": p}
    resp = curl_requests.post(
        url,
        data=form,
        timeout=timeout,
        proxies=proxies,
        impersonate="chrome",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    body_text = resp.text or ""
    try:
        parsed: dict[str, Any] | str = resp.json()
    except Exception:
        parsed = body_text
    return int(resp.status_code or 0), parsed


def _post_form(
    url: str,
    form: dict[str, str],
    timeout: float = 30.0,
    *,
    proxy: str | None = None,
    retries: int = 4,
    prefer_curl: bool = False,
) -> tuple[int, dict[str, Any] | str]:
    """POST form; try urllib then curl_cffi on TLS/network failures."""
    last_exc: BaseException | None = None
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        use_curl = prefer_curl or attempt >= 2
        try:
            if use_curl:
                try:
                    return _post_form_curl_cffi(url, form, timeout, proxy=proxy)
                except Exception as curl_exc:  # noqa: BLE001
                    # curl missing / failed — fall back to urllib this attempt
                    last_exc = curl_exc
                    if attempt == 1 and prefer_curl:
                        # try urllib once before giving up this attempt
                        return _post_form_urllib(url, form, timeout, proxy=proxy)
                    raise
            return _post_form_urllib(url, form, timeout, proxy=proxy)
        except urllib.error.HTTPError:
            raise
        except Exception as e:  # noqa: BLE001 — SSL EOF / reset / tunnel
            last_exc = e
            if attempt >= attempts or not _is_transient_network_error(e):
                break
            time.sleep(0.5 * attempt)
            # Next loop will prefer curl
            prefer_curl = True
    assert last_exc is not None
    raise last_exc


@dataclass
class DeviceCodeSession:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    raw: dict[str, Any]


@dataclass
class TokenResult:
    access_token: str
    refresh_token: str
    id_token: str | None
    token_type: str
    expires_in: int
    raw: dict[str, Any]


class OAuthDeviceError(RuntimeError):
    pass


def request_device_code(
    *,
    client_id: str = CLIENT_ID,
    scope: str = SCOPE,
    timeout: float = 30.0,
    proxy: str | None = None,
) -> DeviceCodeSession:
    status, body = _post_form(
        DEVICE_CODE_URL,
        {"client_id": client_id, "scope": scope},
        timeout=timeout,
        proxy=proxy,
        prefer_curl=True,
    )
    if status != 200 or not isinstance(body, dict):
        raise OAuthDeviceError(f"device code request failed HTTP {status}: {body!r}")
    device_code = str(body.get("device_code") or "").strip()
    user_code = str(body.get("user_code") or "").strip()
    if not device_code or not user_code:
        raise OAuthDeviceError(f"device code response missing fields: {body}")
    vuri = str(body.get("verification_uri") or "https://accounts.x.ai/oauth2/device").strip()
    vcomplete = str(
        body.get("verification_uri_complete") or f"{vuri}?user_code={user_code}"
    ).strip()
    expires_in = int(body.get("expires_in") or 1800)
    interval = max(int(body.get("interval") or 5), 1)
    return DeviceCodeSession(
        device_code=device_code,
        user_code=user_code,
        verification_uri=vuri,
        verification_uri_complete=vcomplete,
        expires_in=expires_in,
        interval=interval,
        raw=body,
    )


def poll_device_token(
    device_code: str,
    *,
    client_id: str = CLIENT_ID,
    interval: int = 5,
    expires_in: int = 1800,
    timeout: float = 30.0,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
    proxy: str | None = None,
    max_network_errors: int = 12,
) -> TokenResult:
    """Poll token endpoint until authorized or expired.

    After the user approves on the device page, the server should return tokens.
    Transient SSL/EOF errors (common with bare urllib on macOS) are retried with
    curl_cffi; too many consecutive network failures abort instead of hanging
    for the full device-code lifetime.
    """
    log = log or _noop_log
    # Cap poll wall time: device codes last up to 1800s but mint should not hang that long
    wall = max(min(int(expires_in), 300), 45)
    deadline = time.time() + wall
    sleep_for = max(interval, 1)
    consecutive_net_err = 0
    prefer_curl = True  # token poll is the flaky path — start with curl_cffi
    while time.time() < deadline:
        if cancel and cancel():
            raise OAuthDeviceError("cancelled")
        try:
            status, body = _post_form(
                TOKEN_URL,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": client_id,
                },
                timeout=timeout,
                proxy=proxy,
                retries=2,
                prefer_curl=prefer_curl,
            )
            consecutive_net_err = 0
        except Exception as e:  # noqa: BLE001
            consecutive_net_err += 1
            prefer_curl = True
            log(
                f"oauth poll network error ({consecutive_net_err}/{max_network_errors}): "
                f"{e} (sleep {sleep_for}s)"
            )
            if consecutive_net_err >= max_network_errors:
                raise OAuthDeviceError(
                    f"token poll aborted after {consecutive_net_err} network/SSL errors "
                    f"(last: {e}). Device may already be authorized — retry remint for this account."
                ) from e
            time.sleep(sleep_for)
            # mild backoff on repeated SSL failures
            sleep_for = min(sleep_for + 1, 12)
            continue
        if status == 200 and isinstance(body, dict) and body.get("access_token"):
            access = str(body["access_token"]).strip()
            refresh = str(body.get("refresh_token") or "").strip()
            if not refresh:
                raise OAuthDeviceError("token response missing refresh_token")
            return TokenResult(
                access_token=access,
                refresh_token=refresh,
                id_token=(str(body["id_token"]).strip() if body.get("id_token") else None),
                token_type=str(body.get("token_type") or "Bearer"),
                expires_in=int(body.get("expires_in") or 21600),
                raw=body,
            )
        err = ""
        desc = ""
        if isinstance(body, dict):
            err = str(body.get("error") or "")
            desc = str(body.get("error_description") or "")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                sleep_for = min(sleep_for + 5, 30)
            log(f"oauth poll: {err} (sleep {sleep_for}s)")
            time.sleep(sleep_for)
            continue
        if err in ("expired_token", "access_denied"):
            raise OAuthDeviceError(f"device auth failed: {err}: {desc}")
        if status == 400 and err:
            raise OAuthDeviceError(f"device auth token error: {err}: {desc or body}")
        if status == 429:
            sleep_for = min(sleep_for + 5, 30)
            log(f"oauth poll HTTP 429 (sleep {sleep_for}s)")
            time.sleep(sleep_for)
            continue
        log(f"oauth poll unexpected HTTP {status}: {body!r}")
        time.sleep(sleep_for)
    raise OAuthDeviceError(
        f"device auth timed out waiting for token (wall={wall}s). "
        "If the browser already showed 设备已授权, run --remint-missing for this account."
    )
