"""Post-success side effects with per-entrypoint profiles.

Profiles (engine-internal; CLI batch applies NSFW itself when config.enable_nsfw):
  - single_cli: accounts + nsfw + grok2api + cpa (caller-driven)
  - cli_pipeline: engine-side side effects off; CLI does accounts/grok2api/nsfw/cpa
  - gui: legacy alias of single_cli (GUI removed)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .types import LogFn, RegisterResult


@dataclass
class SideEffectProfile:
    name: str
    write_accounts: bool = True
    enable_nsfw: bool = False
    grok2api: bool = True
    cpa_export: bool = False  # usually done by on_success_job for pipeline


PROFILES: dict[str, SideEffectProfile] = {
    "cli_pipeline": SideEffectProfile(name="cli_pipeline", enable_nsfw=False, cpa_export=False),
    "gui": SideEffectProfile(name="gui", enable_nsfw=True, cpa_export=True),
    "single_cli": SideEffectProfile(name="single_cli", enable_nsfw=True, cpa_export=True),
    "none": SideEffectProfile(name="none", write_accounts=False, grok2api=False),
}


@dataclass
class SideEffectRunner:
    """Run post-register side effects at most once per successful result."""

    config: dict[str, Any] = field(default_factory=dict)
    _done_keys: set[str] = field(default_factory=set)

    def run_once(
        self,
        result: RegisterResult,
        *,
        profile: str = "cli_pipeline",
        accounts_file: str | None = None,
        log: LogFn | None = None,
        hooks: dict[str, Callable[..., Any]] | None = None,
    ) -> dict[str, Any]:
        log = log or (lambda _m: None)
        hooks = hooks or {}
        if not result.ok or not str(result.sso or "").strip():
            return {"ok": False, "skipped": True, "reason": "not_success"}

        key = f"{result.email}|{result.sso[:16]}"
        if key in self._done_keys:
            log("[side_effects] already ran for this success; skip")
            return {"ok": True, "skipped": True, "reason": "already_ran"}
        self._done_keys.add(key)

        prof = PROFILES.get(profile) or PROFILES["cli_pipeline"]
        out: dict[str, Any] = {"profile": prof.name, "actions": []}

        if prof.write_accounts and accounts_file:
            line = f"{result.email}----{result.password}----{result.sso}\n"
            with open(accounts_file, "a", encoding="utf-8") as fh:
                fh.write(line)
            out["actions"].append("accounts_file")
            log(f"[side_effects] accounts -> {accounts_file}")

        if prof.grok2api and hooks.get("grok2api"):
            try:
                hooks["grok2api"](result.sso, result.email)
                out["actions"].append("grok2api")
            except Exception as exc:
                log(f"[side_effects] grok2api failed: {exc}")
                out["grok2api_error"] = str(exc)

        if prof.enable_nsfw and self.config.get("enable_nsfw", True) and hooks.get("nsfw"):
            try:
                hooks["nsfw"](result.sso)
                out["actions"].append("nsfw")
            except Exception as exc:
                log(f"[side_effects] nsfw failed: {exc}")
                out["nsfw_error"] = str(exc)

        if prof.cpa_export and hooks.get("cpa"):
            try:
                hooks["cpa"](result)
                out["actions"].append("cpa")
            except Exception as exc:
                log(f"[side_effects] cpa failed: {exc}")
                out["cpa_error"] = str(exc)

        out["ok"] = True
        return out
