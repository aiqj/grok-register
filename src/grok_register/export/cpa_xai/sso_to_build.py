"""Convert Grok Web SSO cookie → Build OAuth tokens (Sub2API-compatible).

Port of Wei-Shaw/sub2api ``internal/pkg/xai/sso_device.go`` ``ConvertSSOToBuild``:

1. Validate SSO against accounts.x.ai
2. Start device code with **SSOBuildScope** (includes conversations:read/write)
3. Auto verify + approve using SSO session cookies (no interactive browser)
4. Poll device token

This matches Sub2API admin "SSO → Grok OAuth" import path more closely than
browser device-code mint used for CPA CLIProxyAPI.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Callable

from .oauth_device import CLIENT_ID, DEVICE_CODE_URL, TOKEN_URL
from .proxyutil import resolve_proxy

# Sub2API xai.SSOBuildScope — broader than plain CLI device-code scope
SSO_BUILD_SCOPE = (
    "openid profile email offline_access grok-cli:access api:access "
    "conversations:read conversations:write"
)
SSO_ACCOUNTS_URL = "https://accounts.x.ai/"
SSO_VERIFY_URL = "https://auth.x.ai/oauth2/device/verify"
SSO_APPROVE_URL = "https://auth.x.ai/oauth2/device/approve"
SSO_DEFAULT_TOKEN_TTL = 6 * 3600
SSO_CONVERSION_TIMEOUT = 90.0

LogFn = Callable[[str], None]


class SSOBuildError(RuntimeError):
    pass


def normalize_sso_token(value: str) -> str:
    """Mirror xai.NormalizeSSOToken — accept raw token or cookie string."""
    value = (value or "").strip()
    if not value:
        return ""
    low = value.lower()
    if low.startswith("cookie:"):
        value = value[len("cookie:") :].strip()
    if "=" in value and (";" in value or value.lower().startswith("sso")):
        for part in value.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, tok = part.split("=", 1)
            if name.strip().lower() in ("sso", "sso-rw"):
                return tok.strip()
    if ";" in value:
        value = value.split(";", 1)[0].strip()
    return value.strip()


def _safe_xai_url(url: str) -> bool:
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    return host == "x.ai" or host.endswith(".x.ai")


class _SSODeviceFlow:
    def __init__(
        self,
        sso_token: str,
        *,
        proxy: str | None = None,
        user_agent: str = "sub2api-grok-oauth/1.0",
        log: LogFn | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.sso = normalize_sso_token(sso_token)
        if not self.sso:
            raise SSOBuildError("empty sso token")
        self.log = log or (lambda _m: None)
        self.timeout = timeout
        self.user_agent = user_agent
        self.jar = CookieJar()
        # Seed SSO cookies (both names, like Sub2API)
        self._seed_cookie("sso", self.sso)
        self._seed_cookie("sso-rw", self.sso)
        self.opener = self._make_opener(proxy)

    def _make_opener(self, proxy: str | None) -> urllib.request.OpenerDirector:
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self.jar)]
        p = resolve_proxy("" if proxy is None else proxy)
        if p:
            handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
        else:
            # Disable env/system proxy fallback
            class _Direct(urllib.request.ProxyHandler):
                def __init__(self):  # noqa: D107
                    urllib.request.BaseHandler.__init__(self)
                    self.proxies = {}

                def proxy_open(self, req, proxy, type):  # noqa: A002
                    return None

            handlers.append(_Direct())
        # Do not follow redirects automatically — Sub2API inspects Location.
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
                return None

        handlers.append(_NoRedirect())
        return urllib.request.build_opener(*handlers)

    def _seed_cookie(self, name: str, value: str) -> None:
        # Minimal cookie for .x.ai
        import http.cookiejar as cj

        for domain in (".x.ai", "accounts.x.ai", "auth.x.ai"):
            c = cj.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": True},
                rfc2109=False,
            )
            try:
                self.jar.set_cookie(c)
            except Exception:
                pass

    def convert(self) -> dict[str, Any]:
        status, final_url, _body = self._do("GET", SSO_ACCOUNTS_URL, None)
        if status == 401 or "sign-in" in final_url or "sign-up" in final_url:
            raise SSOBuildError("sso unauthorized / redirected to sign-in")
        if status < 200 or status >= 400:
            raise SSOBuildError(f"validate sso HTTP {status} url={final_url}")

        status, _, body = self._do(
            "POST",
            DEVICE_CODE_URL,
            {"client_id": CLIENT_ID, "scope": SSO_BUILD_SCOPE},
        )
        if status < 200 or status >= 300:
            raise SSOBuildError(f"device code HTTP {status}: {body!r}")
        try:
            device = json.loads(body) if isinstance(body, (str, bytes)) else body
        except Exception as e:
            raise SSOBuildError(f"device code json: {e}") from e
        if not isinstance(device, dict):
            raise SSOBuildError("device code response not object")

        device_code = str(device.get("device_code") or "").strip()
        user_code = str(device.get("user_code") or "").strip()
        vuri_complete = str(device.get("verification_uri_complete") or "").strip()
        interval = int(device.get("interval") or 5)
        expires_in = int(device.get("expires_in") or 1800)
        if not device_code or not user_code or not _safe_xai_url(vuri_complete):
            raise SSOBuildError(f"device flow incomplete: {device}")
        if interval <= 0:
            interval = 5
        self.log(f"sso-build device user_code={user_code}")

        status, _, _ = self._do("GET", vuri_complete, None)
        if status < 200 or status >= 400:
            raise SSOBuildError(f"open verification page HTTP {status}")

        status, final_url, _ = self._do(
            "POST", SSO_VERIFY_URL, {"user_code": user_code}
        )
        if status < 200 or status >= 400:
            raise SSOBuildError(f"verify device HTTP {status}")
        if "consent" not in final_url:
            raise SSOBuildError(f"verify did not reach consent page: {final_url}")

        status, final_url, _ = self._do(
            "POST",
            SSO_APPROVE_URL,
            {
                "user_code": user_code,
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
        )
        if status < 200 or status >= 400:
            raise SSOBuildError(f"approve device HTTP {status}")
        if "done" not in final_url:
            raise SSOBuildError(f"approve did not reach done page: {final_url}")

        return self._poll_token(device_code, interval, min(expires_in, 75))

    def _poll_token(
        self, device_code: str, interval: int, wall_sec: int
    ) -> dict[str, Any]:
        if interval < 1:
            interval = 1
        deadline = time.time() + max(10, wall_sec)
        while time.time() < deadline:
            time.sleep(interval)
            status, _, body = self._do(
                "POST",
                TOKEN_URL,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                },
            )
            try:
                payload = json.loads(body) if isinstance(body, (str, bytes)) else {}
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if 200 <= status < 300 and payload.get("access_token"):
                exp_in = int(payload.get("expires_in") or SSO_DEFAULT_TOKEN_TTL)
                return {
                    "access_token": str(payload.get("access_token") or ""),
                    "refresh_token": str(payload.get("refresh_token") or ""),
                    "id_token": str(payload.get("id_token") or ""),
                    "token_type": str(payload.get("token_type") or "Bearer"),
                    "expires_in": exp_in,
                    "scope": str(payload.get("scope") or SSO_BUILD_SCOPE),
                    "source": "sso_to_build",
                }
            err = str(payload.get("error") or "")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err in ("access_denied", "expired_token"):
                raise SSOBuildError(f"device authorization denied: {err}")
            if status >= 400:
                raise SSOBuildError(
                    f"token poll HTTP {status}: "
                    f"{payload.get('error_description') or err or body!r}"
                )
        raise SSOBuildError("sso-build token poll timed out")

    def _do(
        self, method: str, endpoint: str, form: dict[str, str] | None
    ) -> tuple[int, str, str]:
        if not _safe_xai_url(endpoint):
            raise SSOBuildError(f"untrusted url: {endpoint}")
        current_url = endpoint
        current_method = method
        current_form = form
        for _ in range(9):
            data = None
            headers = {
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "User-Agent": self.user_agent,
            }
            if current_form is not None:
                data = urllib.parse.urlencode(current_form).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            req = urllib.request.Request(
                current_url,
                data=data,
                headers=headers,
                method=current_method,
            )
            try:
                # Don't auto-follow; we need Location for consent/done checks
                # build_opener may follow redirects — use opener that doesn't for redirects
                with self.opener.open(req, timeout=self.timeout) as resp:
                    status = getattr(resp, "status", 200) or 200
                    final = resp.geturl() or current_url
                    raw = resp.read().decode("utf-8", errors="replace")
                    if 300 <= status <= 399:
                        # urllib usually follows; if we get here without redirect, ok
                        pass
                    return int(status), final, raw
            except urllib.error.HTTPError as e:
                status = int(e.code)
                raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
                # HTTPError may still have Location
                loc = e.headers.get("Location") if e.headers else None
                if 300 <= status <= 399 and loc:
                    base = urllib.parse.urlparse(current_url)
                    next_u = urllib.parse.urljoin(current_url, loc)
                    if not _safe_xai_url(next_u):
                        raise SSOBuildError(f"redirect untrusted: {next_u}") from e
                    current_url = next_u
                    if status == 303 or (
                        status in (301, 302)
                        and current_method not in ("GET", "HEAD")
                    ):
                        current_method = "GET"
                        current_form = None
                    continue
                # Non-redirect HTTPError
                final = current_url
                return status, final, raw
            except Exception as e:
                raise SSOBuildError(f"request failed: {e}") from e
        raise SSOBuildError("too many redirects")


def convert_sso_to_build(
    sso_token: str,
    *,
    proxy: str | None = None,
    log: LogFn | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Convert web SSO → Build OAuth token dict (access/refresh/id/scope/…)."""
    flow = _SSODeviceFlow(
        sso_token, proxy=proxy, log=log, timeout=timeout
    )
    return flow.convert()
