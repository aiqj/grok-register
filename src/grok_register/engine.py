"""Browser-only registration engine."""

from __future__ import annotations

import time
from typing import Any, Callable

from .metrics import RegisterMetrics, default_metrics
from .side_effects import SideEffectRunner
from .transport.browser import BrowserTransport
from .types import CancelFn, FailureReason, LogFn, RegisterResult


class RegistrationEngine:
    """Run one browser registration (email → OTP → profile → SSO)."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        metrics: RegisterMetrics | None = None,
        reg_module: Any = None,
    ):
        self.config = config if config is not None else {}
        self.metrics = metrics or default_metrics
        self.reg_module = reg_module
        self.side_effects = SideEffectRunner(config=self.config)

    def register_one(
        self,
        *,
        log_callback: LogFn | None = None,
        cancel_callback: CancelFn | None = None,
        side_effect_profile: str = "cli_pipeline",
        accounts_file: str | None = None,
        on_success_job: Callable[[RegisterResult], None] | None = None,
        max_mail_retry: int | None = None,
        run_side_effects: bool = False,
        side_effect_hooks: dict[str, Callable[..., Any]] | None = None,
    ) -> RegisterResult:
        log = log_callback or (lambda _m: None)
        cancel = cancel_callback or (lambda: False)
        if max_mail_retry is None:
            try:
                if self.reg_module and hasattr(self.reg_module, "get_max_mail_retry"):
                    max_mail_retry = int(self.reg_module.get_max_mail_retry())
                else:
                    max_mail_retry = int(self.config.get("max_mail_retry") or 3)
            except Exception:
                max_mail_retry = 3
        max_mail_retry = max(1, min(20, int(max_mail_retry)))

        log("[engine] browser registration")
        transport = BrowserTransport(log=log, cancel=cancel, reg_module=self.reg_module)
        try:
            result = self._run_browser_pipeline(
                transport,
                log=log,
                cancel=cancel,
                max_mail_retry=max_mail_retry,
            )
            self.metrics.record_attempt(ok=bool(result.ok and result.sso))
            if result.ok and result.sso:
                if run_side_effects:
                    self.side_effects.run_once(
                        result,
                        profile=side_effect_profile,
                        accounts_file=accounts_file,
                        log=log,
                        hooks=side_effect_hooks or {},
                    )
                if on_success_job:
                    on_success_job(result)
            return result
        except Exception as exc:
            if self.reg_module is not None:
                for name in ("AccountRetryNeeded", "RegistrationCancelled"):
                    cls = getattr(self.reg_module, name, None)
                    if cls is not None and isinstance(exc, cls):
                        raise
            self.metrics.record_attempt(ok=False)
            log(f"[engine] exception: {exc}")
            return RegisterResult(
                ok=False,
                transport_used="browser",
                failure_reason=FailureReason.UNKNOWN_FLOW,
                error=str(exc),
            )
        finally:
            try:
                transport.close()
            except Exception:
                pass

    def _run_browser_pipeline(
        self,
        transport: BrowserTransport,
        *,
        log: LogFn,
        cancel: CancelFn,
        max_mail_retry: int,
    ) -> RegisterResult:
        t0 = time.time()

        def _stages() -> str:
            try:
                return str(transport.stage_summary() or "")
            except Exception:
                return ""

        transport.start_session()
        begin = transport.begin_signup()
        if not begin.ok:
            return RegisterResult(
                ok=False,
                transport_used="browser",
                failure_reason=begin.failure_reason or FailureReason.UNKNOWN_FLOW,
                error=begin.error,
                duration_ms=int((time.time() - t0) * 1000),
                stage_summary=_stages(),
            )

        email = ""
        last_err = ""
        last_reason: FailureReason | None = None
        for mail_try in range(1, max_mail_retry + 1):
            if cancel():
                return RegisterResult(
                    ok=False,
                    transport_used="browser",
                    failure_reason=FailureReason.CANCELLED,
                    error="cancelled",
                    duration_ms=int((time.time() - t0) * 1000),
                    stage_summary=_stages(),
                )
            log(f"[*] 邮箱阶段 {mail_try}/{max_mail_retry}")
            if mail_try > 1:
                log("[*] 更换邮箱/会话后重试")
                transport.reset_session()
                br = transport.begin_signup()
                if not br.ok:
                    last_err = br.error
                    last_reason = br.failure_reason
                    if mail_try < max_mail_retry:
                        continue
                    break
            log("[*] 1. 打开注册页 / 创建邮箱并提交")
            email_res = transport.submit_email()
            if not email_res.ok:
                last_err = email_res.error
                last_reason = email_res.failure_reason
                if email_res.failure_reason == FailureReason.EMAIL_DOMAIN_REJECTED:
                    log(f"[!] 邮箱域名被拒绝，换邮重试: {email_res.error}")
                    continue
                if email_res.failure_reason in (
                    FailureReason.OTP_TIMEOUT,
                    FailureReason.UNKNOWN_FLOW,
                ) and mail_try < max_mail_retry:
                    log(f"[!] 邮箱提交失败，换邮重试: {email_res.error}")
                    continue
                if mail_try < max_mail_retry:
                    continue
                break
            email = str(email_res.data.get("email") or "")
            log("[*] 2. 拉取验证码")
            otp_res = transport.submit_otp(email)
            if not otp_res.ok:
                last_err = otp_res.error
                last_reason = otp_res.failure_reason
                if otp_res.failure_reason in (
                    FailureReason.OTP_TIMEOUT,
                    FailureReason.OTP_INVALID,
                ) and mail_try < max_mail_retry:
                    log(f"[!] 本邮箱未取到验证码，换邮重试: {otp_res.error}")
                    continue
                break
            log("[*] 3. 填写资料")
            prof_res = transport.submit_profile()
            if not prof_res.ok:
                return RegisterResult(
                    ok=False,
                    email=email,
                    transport_used="browser",
                    failure_reason=prof_res.failure_reason,
                    error=prof_res.error,
                    duration_ms=int((time.time() - t0) * 1000),
                    highest_mutating_step="otp_verified",
                    stage_summary=_stages(),
                )
            log("[*] 4. 等待 sso cookie")
            try:
                sso = transport.extract_sso()
            except Exception as exc:
                if self.reg_module is not None:
                    for name in ("AccountRetryNeeded", "RegistrationCancelled"):
                        cls = getattr(self.reg_module, name, None)
                        if cls is not None and isinstance(exc, cls):
                            raise
                return RegisterResult(
                    ok=False,
                    email=email,
                    password=str(prof_res.data.get("password") or ""),
                    profile=dict(prof_res.data or {}),
                    transport_used="browser",
                    failure_reason=FailureReason.SSO_MISSING,
                    error=str(exc),
                    duration_ms=int((time.time() - t0) * 1000),
                    highest_mutating_step="profile_submitted",
                    stage_summary=_stages(),
                )
            cookies = transport.export_cookies()
            if cookies:
                log(f"[*] 导出 cookie {len(cookies)} 条")
            stages = _stages()
            total_ms = int((time.time() - t0) * 1000)
            if stages:
                log(f"[*] timing: {stages} total={total_ms}ms")
            return RegisterResult(
                ok=True,
                email=email,
                password=str(prof_res.data.get("password") or ""),
                sso=sso,
                profile=dict(prof_res.data or {}),
                transport_used="browser",
                cookies=cookies,
                duration_ms=total_ms,
                highest_mutating_step="profile_submitted",
                stage_summary=stages,
            )

        return RegisterResult(
            ok=False,
            email=email,
            transport_used="browser",
            failure_reason=last_reason or FailureReason.UNKNOWN_FLOW,
            error=last_err or "browser pipeline failed",
            duration_ms=int((time.time() - t0) * 1000),
            stage_summary=_stages(),
        )
