"""Shared types for browser registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


class FailureReason(str, Enum):
    CF_CHALLENGE = "cf_challenge"
    TURNSTILE_REQUIRED = "turnstile_required"
    EMAIL_DOMAIN_REJECTED = "email_domain_rejected"
    OTP_INVALID = "otp_invalid"
    OTP_TIMEOUT = "otp_timeout"
    SSO_MISSING = "sso_missing"
    UNKNOWN_FLOW = "unknown_flow"
    NETWORK = "network"
    CANCELLED = "cancelled"
    BROWSER_START = "browser_start"


@dataclass
class StepResult:
    step: str  # init|email|otp|profile|sso
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    failure_reason: FailureReason | None = None
    error: str = ""


@dataclass
class RegisterResult:
    ok: bool
    email: str = ""
    password: str = ""
    sso: str = ""
    profile: dict[str, Any] = field(default_factory=dict)
    transport_used: str = "browser"
    failure_reason: FailureReason | None = None
    error: str = ""
    cookies: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    highest_mutating_step: str = "none"  # none|email_submitted|otp_verified|profile_submitted
    stage_summary: str = ""
