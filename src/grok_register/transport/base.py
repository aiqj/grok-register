"""Abstract registration transport."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from grok_register.types import CancelFn, LogFn, StepResult


class AbstractRegisterTransport(ABC):
    name: str = "abstract"

    def __init__(self, *, log: LogFn | None = None, cancel: CancelFn | None = None):
        self.log = log or (lambda _m: None)
        self.cancel = cancel or (lambda: False)

    @abstractmethod
    def start_session(self) -> None: ...

    @abstractmethod
    def begin_signup(self) -> StepResult: ...

    @abstractmethod
    def submit_email(self, email: str) -> StepResult: ...

    @abstractmethod
    def submit_otp(self, email: str, code: str) -> StepResult: ...

    @abstractmethod
    def submit_profile(self, profile: dict[str, str]) -> StepResult: ...

    @abstractmethod
    def extract_sso(self, timeout: float = 120.0) -> str: ...

    @abstractmethod
    def export_cookies(self) -> list[dict[str, Any]]: ...

    def supports_resend_otp(self) -> bool:
        return False

    def resend_otp(self, email: str) -> StepResult:
        return StepResult(
            step="otp",
            ok=False,
            failure_reason=None,
            error="resend_not_supported",
        )

    @abstractmethod
    def reset_session(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...
