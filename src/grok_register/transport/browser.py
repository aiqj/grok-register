"""Browser transport wrapping existing registerlib.core fill_* helpers."""

from __future__ import annotations

import time
from typing import Any

from grok_register.types import FailureReason, StepResult
from .base import AbstractRegisterTransport


class BrowserTransport(AbstractRegisterTransport):
    name = "browser"

    def __init__(self, *, log=None, cancel=None, reg_module=None):
        super().__init__(log=log, cancel=cancel)
        if reg_module is None:
            import grok_register.core as reg_module  # local import avoids circular at package load
        self.reg = reg_module
        self._email = ""
        self._dev_token = ""
        self._profile: dict[str, str] = {}
        self._sso = ""
        self._stage_ms: dict[str, float] = {}

    def _mark(self, stage: str, t0: float) -> None:
        self._stage_ms[stage] = round((time.time() - t0) * 1000.0, 1)

    def stage_summary(self) -> str:
        if not self._stage_ms:
            return ""
        order = ("init", "email", "otp", "profile", "sso")
        parts = [f"{k}={self._stage_ms[k]:.0f}ms" for k in order if k in self._stage_ms]
        extra = [f"{k}={v:.0f}ms" for k, v in self._stage_ms.items() if k not in order]
        return " ".join(parts + extra)

    def start_session(self) -> None:
        self.reg.start_browser(log_callback=self.log)
        # Ensure page context is bound for multi-thread TabPool path
        page = self.reg.get_page()
        browser = getattr(self.reg, "browser", None)
        if hasattr(self.reg, "TabPool") and self.reg.TabPool.get_browser() is not None:
            browser = self.reg.TabPool.get_browser()
            page = self.reg.TabPool.get_tab()
        if browser is not None and page is not None:
            self.reg.set_page_context(browser, page)

    def begin_signup(self) -> StepResult:
        t0 = time.time()
        try:
            self.reg.open_signup_page(log_callback=self.log, cancel_callback=self.cancel)
            self._mark("init", t0)
            return StepResult(step="init", ok=True)
        except self.reg.RegistrationCancelled as exc:
            self._mark("init", t0)
            return StepResult(
                step="init",
                ok=False,
                failure_reason=FailureReason.CANCELLED,
                error=str(exc),
            )
        except Exception as exc:
            self._mark("init", t0)
            err = str(exc)
            reason = FailureReason.UNKNOWN_FLOW
            cf_cls = getattr(self.reg, "CloudflareBlockedError", None)
            proxy_cls = getattr(self.reg, "ProxyOrNetworkPageError", None)
            if proxy_cls is not None and isinstance(exc, proxy_cls):
                reason = FailureReason.NETWORK
            elif cf_cls is not None and isinstance(exc, cf_cls):
                reason = FailureReason.CF_CHALLENGE
            elif any(
                m in err.lower()
                for m in (
                    "cloudflare",
                    "attention required",
                    "you have been blocked",
                    "cf_challenge",
                )
            ):
                reason = FailureReason.CF_CHALLENGE
            elif any(
                m in err.lower()
                for m in ("proxy", "tunnel", "chrome 错误", "err_proxy", "err_tunnel")
            ):
                reason = FailureReason.NETWORK
            return StepResult(
                step="init",
                ok=False,
                failure_reason=reason,
                error=err,
            )

    def _reraise_control_flow(self, exc: BaseException) -> None:
        """Let AccountRetryNeeded / RegistrationCancelled bubble to entrypoints."""
        for name in ("AccountRetryNeeded", "RegistrationCancelled"):
            cls = getattr(self.reg, name, None)
            if cls is not None and isinstance(exc, cls):
                raise exc

    def submit_email(self, email: str = "") -> StepResult:
        """Create temp mail + fill email on page.

        When email is empty, uses reg.fill_email_and_submit (creates mailbox).
        When email is provided, still uses fill_email_and_submit today (creates new);
        future PR may accept pre-created address.
        """
        t0 = time.time()
        try:
            # Existing helper creates mailbox + submits
            addr, token = self.reg.fill_email_and_submit(
                log_callback=self.log, cancel_callback=self.cancel
            )
            self._email = addr
            self._dev_token = token
            self.log(f"[*] 邮箱: {addr}")
            # Parity with GUI/single-cli: append mail credentials (best-effort)
            try:
                import os

                path = os.path.join(
                    os.path.dirname(os.path.abspath(self.reg.__file__)),
                    "mail_credentials.txt",
                )
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(f"{addr}\t{token}\n")
            except Exception:
                pass
            self._mark("email", t0)
            return StepResult(
                step="email",
                ok=True,
                data={"email": addr, "dev_token": token},
            )
        except self.reg.EmailDomainRejected as exc:
            self._mark("email", t0)
            return StepResult(
                step="email",
                ok=False,
                failure_reason=FailureReason.EMAIL_DOMAIN_REJECTED,
                error=str(exc),
            )
        except Exception as exc:
            self._mark("email", t0)
            self._reraise_control_flow(exc)
            msg = str(exc)
            reason = FailureReason.UNKNOWN_FLOW
            if "未收到验证码" in msg or ("验证码" in msg and "超时" in msg):
                reason = FailureReason.OTP_TIMEOUT
            return StepResult(step="email", ok=False, failure_reason=reason, error=msg)

    def submit_otp(self, email: str, code: str = "") -> StepResult:
        """Poll mail for OTP and submit on page.

        When code is empty, fill_code_and_submit polls and fills.
        """
        t0 = time.time()
        try:
            got = self.reg.fill_code_and_submit(
                email or self._email,
                self._dev_token,
                log_callback=self.log,
                cancel_callback=self.cancel,
            )
            self.log(f"[*] 验证码: {got}")
            self._mark("otp", t0)
            return StepResult(step="otp", ok=True, data={"code": got})
        except Exception as exc:
            self._mark("otp", t0)
            self._reraise_control_flow(exc)
            msg = str(exc)
            reason = (
                FailureReason.OTP_TIMEOUT
                if ("未收到" in msg or "超时" in msg)
                else FailureReason.OTP_INVALID
            )
            return StepResult(step="otp", ok=False, failure_reason=reason, error=msg)

    def submit_profile(self, profile: dict[str, str] | None = None) -> StepResult:
        t0 = time.time()
        try:
            got = self.reg.fill_profile_and_submit(
                log_callback=self.log, cancel_callback=self.cancel
            )
            self._profile = dict(got or {})
            self.log(
                f"[*] 资料已填: {self._profile.get('given_name')} {self._profile.get('family_name')}"
            )
            self._mark("profile", t0)
            return StepResult(step="profile", ok=True, data=self._profile)
        except Exception as exc:
            self._mark("profile", t0)
            self._reraise_control_flow(exc)
            return StepResult(
                step="profile",
                ok=False,
                failure_reason=FailureReason.UNKNOWN_FLOW,
                error=str(exc),
            )

    def extract_sso(self, timeout: float = 120.0) -> str:
        t0 = time.time()
        try:
            sso = self.reg.wait_for_sso_cookie(
                timeout=timeout, log_callback=self.log, cancel_callback=self.cancel
            )
        except Exception as exc:
            self._mark("sso", t0)
            self._reraise_control_flow(exc)
            raise
        self._sso = str(sso or "")
        self._mark("sso", t0)
        if not self._sso.strip():
            raise RuntimeError("sso cookie missing")
        return self._sso

    def export_cookies(self) -> list[dict[str, Any]]:
        try:
            from grok_register.export.cpa_export import export_cookies_from_page

            page = self.reg.get_page()
            if page is None:
                return []
            return export_cookies_from_page(page) or []
        except Exception as exc:
            self.log(f"[browser] export_cookies failed: {exc}")
            return []

    def supports_resend_otp(self) -> bool:
        return True

    def reset_session(self) -> None:
        self._stage_ms = {}
        try:
            if hasattr(self.reg, "prepare_browser_for_next_account"):
                # full recycle for clean retry
                self.reg.stop_browser()
                self.start_session()
            else:
                self.reg.restart_browser(log_callback=self.log)
        except Exception as exc:
            self.log(f"[browser] reset_session: {exc}")

    def close(self) -> None:
        # Register browser lifecycle is owned by TabPool / prepare_browser_for_next_account
        # for CLI; do not force-quit here.
        return None
