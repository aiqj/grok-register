"""Convert CPA xAI auth JSON to Sub2API (Wei-Shaw/sub2api) import JSON.

Official data-import payload (backend/internal/handler/admin/account_data.go):

{
  "type": "sub2api-data",
  "version": 1,
  "exported_at": "RFC3339",
  "proxies": [],
  "accounts": [ DataAccount, ... ]
}

Grok OAuth account requirements (domain.PlatformGrok + BuildAccountCredentials):
  - platform: "grok"   (NOT "xai")
  - type: "oauth"
  - expires_at (account): unix seconds
  - credentials:
      access_token, refresh_token, id_token, token_type,
      expires_at (RFC3339 string), client_id, email, base_url=https://api.x.ai/v1
"""
from __future__ import annotations

from grok_register.paths import PROJECT_ROOT

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sub2API official constants
SUB2API_DATA_TYPE = "sub2api-data"
SUB2API_DATA_VERSION = 1
PLATFORM_GROK = "grok"
ACCOUNT_TYPE_OAUTH = "oauth"
XAI_DEFAULT_BASE_URL = "https://api.x.ai/v1"
XAI_DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
XAI_REDIRECT_URI = "http://127.0.0.1:56121/callback"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = str(token or "").split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()).decode("utf-8"))
    except Exception:
        return {}


def _parse_time_to_unix_seconds(value: Any) -> int | None:
    """Normalize exp / expired / expires_at into unix seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        # Heuristic: ms timestamps are >= ~1e12
        if n > 1_000_000_000_000:
            return n // 1000
        if n > 0:
            return n
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return _parse_time_to_unix_seconds(float(text))
    try:
        text = text.replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        return None


def _expires_at_unix_seconds(access_token: str, expired: str | None = None) -> int | None:
    payload = _jwt_payload(access_token)
    sec = _parse_time_to_unix_seconds(payload.get("exp"))
    if sec is not None:
        return sec
    return _parse_time_to_unix_seconds(expired)


def _rfc3339_from_unix(sec: int | None) -> str | None:
    if sec is None:
        return None
    try:
        return datetime.fromtimestamp(int(sec), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _email_key(email: str) -> str:
    return (email or "").strip().lower().replace("@", "_").replace(".", "_")


def _first_nonempty(*values: Any) -> str:
    for v in values:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def _normalize_base_url(raw: str | None) -> str:
    """
    Sub2API Grok default base is https://api.x.ai/v1.
    CPA mint may store cli-chat-proxy.grok.com — remap to public API base for import.
    """
    text = str(raw or "").strip().rstrip("/")
    if not text:
        return XAI_DEFAULT_BASE_URL
    lower = text.lower()
    # Keep api.x.ai; rewrite CLI proxy (or bare host) to official API base.
    if "cli-chat-proxy.grok.com" in lower:
        return XAI_DEFAULT_BASE_URL
    if lower in ("https://api.x.ai", "http://api.x.ai", "https://api.x.ai/", "http://api.x.ai/"):
        return XAI_DEFAULT_BASE_URL
    if lower.startswith("https://api.x.ai/") or lower.startswith("http://api.x.ai/"):
        return text if text.endswith("/v1") or "/v1/" in text + "/" else XAI_DEFAULT_BASE_URL
    # Unknown custom base: keep only if it looks like a full URL; else default.
    if lower.startswith("http://") or lower.startswith("https://"):
        return text
    return XAI_DEFAULT_BASE_URL


def cpa_xai_to_sub2api_account(cpa: dict[str, Any], *, source: str = "cpa_xai") -> dict[str, Any]:
    access_token = str(cpa.get("access_token") or "")
    refresh_token = str(cpa.get("refresh_token") or "")
    id_token = str(cpa.get("id_token") or "")
    email = _first_nonempty(cpa.get("email"), _jwt_payload(id_token).get("email"))
    sub = _first_nonempty(cpa.get("sub"), _jwt_payload(access_token).get("sub"), _jwt_payload(id_token).get("sub"))
    expired = str(cpa.get("expired") or "")
    expires_sec = _expires_at_unix_seconds(access_token, expired)
    expires_rfc3339 = _rfc3339_from_unix(expires_sec)

    client_id = _first_nonempty(
        cpa.get("client_id"),
        _jwt_payload(access_token).get("client_id"),
        _jwt_payload(access_token).get("aud"),
        XAI_DEFAULT_CLIENT_ID,
    )
    # aud may be list
    if isinstance(client_id, list):
        client_id = _first_nonempty(*(str(x) for x in client_id), XAI_DEFAULT_CLIENT_ID)

    scope = _first_nonempty(
        cpa.get("scope"),
        _jwt_payload(access_token).get("scope"),
        "openid profile email offline_access grok-cli:access api:access",
    )
    token_type = _first_nonempty(cpa.get("token_type"), "Bearer")
    base_url = _normalize_base_url(cpa.get("base_url"))

    name = email or sub or "Grok OAuth Account"

    # Match Sub2API GrokOAuthService.BuildAccountCredentials
    credentials: dict[str, Any] = {
        "access_token": access_token,
        "token_type": token_type,
        "base_url": base_url,
        "client_id": client_id,
    }
    if refresh_token:
        credentials["refresh_token"] = refresh_token
    if id_token:
        credentials["id_token"] = id_token
    if expires_rfc3339:
        credentials["expires_at"] = expires_rfc3339
    if email:
        credentials["email"] = email
    if scope:
        credentials["scope"] = scope
    # Optional identity fields used by dashboards / refresh helpers
    if sub:
        credentials["sub"] = sub
    # Keep token endpoint for refresh tooling (not required by import, but harmless)
    credentials["token_endpoint"] = _first_nonempty(cpa.get("token_endpoint"), XAI_TOKEN_ENDPOINT)
    credentials["redirect_uri"] = _first_nonempty(cpa.get("redirect_uri"), XAI_REDIRECT_URI)

    # Do NOT put CPA-only headers / expired-ms / wrong platform markers into credentials.

    account: dict[str, Any] = {
        "name": name,
        "platform": PLATFORM_GROK,  # critical: Sub2API platform id is "grok"
        "type": ACCOUNT_TYPE_OAUTH,
        "concurrency": 1,  # Grok OAuth often restricted to concurrency 1 unless unsafe override
        "priority": 50,
        "credentials": credentials,
        "extra": {
            "email": email,
            "email_key": _email_key(email),
            "name": name,
            "auth_provider": "grok",
            "source": source,
            "last_refresh": cpa.get("last_refresh") or _now_iso(),
            "import_source": "grok-register-cpa",
        },
    }
    if expires_sec is not None:
        account["expires_at"] = int(expires_sec)  # unix seconds (DataAccount.ExpiresAt)
        account["auto_pause_on_expired"] = True

    # Preserve expires_in if present (informational only)
    if cpa.get("expires_in") is not None:
        try:
            credentials["expires_in"] = int(cpa.get("expires_in"))
        except Exception:
            pass

    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if x is not None}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    return strip(account)


def build_sub2api_document(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Sub2API admin data-import document."""
    return {
        "type": SUB2API_DATA_TYPE,
        "version": SUB2API_DATA_VERSION,
        "exported_at": _now_iso(),
        "proxies": [],
        "accounts": accounts,
    }


def _default_sub2api_dir_next_to_cpa(cpa_path: Path) -> Path:
    """Prefer exports/sub2api when CPA lives in exports/cpa (or legacy cpa_auths)."""
    parent = cpa_path.parent
    if parent.name in ("cpa", "cpa_auths"):
        return (parent.parent / "sub2api").resolve()
    return (parent.parent / "exports" / "sub2api").resolve()


def convert_cpa_file(cpa_path: str | Path, out_dir: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    cpa_path = Path(cpa_path).expanduser().resolve()
    cpa = json.loads(cpa_path.read_text(encoding="utf-8-sig"))
    account = cpa_xai_to_sub2api_account(cpa, source="cpa_xai")
    doc = build_sub2api_document([account])
    out_dir = Path(out_dir or _default_sub2api_dir_next_to_cpa(cpa_path)).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"sub2api-{cpa_path.stem}.json"
    out_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_file, doc


def rebuild_combined(cpa_dir: str | Path, out_file: str | Path) -> Path:
    cpa_dir = Path(cpa_dir).expanduser().resolve()
    accounts: list[dict[str, Any]] = []
    for p in sorted(cpa_dir.glob("xai-*.json")):
        try:
            cpa = json.loads(p.read_text(encoding="utf-8-sig"))
            accounts.append(cpa_xai_to_sub2api_account(cpa, source="cpa_xai"))
        except Exception:
            continue
    out_file = Path(out_file).expanduser().resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(
        json.dumps(build_sub2api_document(accounts), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return out_file


def export_after_cpa_result(
    result: dict[str, Any],
    config: dict[str, Any] | None = None,
    log_callback=None,
) -> dict[str, Any]:
    cfg = config or {}
    log = log_callback or (lambda m: None)
    if not cfg.get("sub2api_export_enabled", True):
        return {"ok": False, "skipped": True, "reason": "disabled"}
    cpa_path = result.get("path") or result.get("cpa_path")
    if not cpa_path:
        return {"ok": False, "error": "missing cpa path"}
    try:
        from grok_register.export.cpa_export import resolve_export_dirs
    except Exception:
        resolve_export_dirs = None  # type: ignore

    if resolve_export_dirs is not None:
        _root, cpa_dir, out_dir = resolve_export_dirs(cfg)
    else:
        # __file__ = grok_register/export/cpa_to_sub2api.py → parents[2] = project root
        project_root = PROJECT_ROOT
        cpa_dir = Path(cfg.get("cpa_auth_dir") or (project_root / "exports" / "cpa"))
        if not cpa_dir.is_absolute():
            cpa_dir = (project_root / cpa_dir).resolve()
        out_dir = Path(cfg.get("sub2api_export_dir") or (project_root / "exports" / "sub2api"))
        if not out_dir.is_absolute():
            out_dir = (project_root / out_dir).resolve()

    out_dir.mkdir(parents=True, exist_ok=True)
    cpa_dir.mkdir(parents=True, exist_ok=True)
    single_path, _doc = convert_cpa_file(cpa_path, out_dir=out_dir)
    combined_raw = str(cfg.get("sub2api_combined_file") or "").strip()
    if combined_raw:
        combined_path = Path(combined_raw).expanduser()
        if not combined_path.is_absolute():
            combined_path = (PROJECT_ROOT / combined_path).resolve()
    else:
        combined_path = out_dir / "sub2api-accounts.json"
    rebuild_combined(cpa_dir, combined_path)
    log(f"[sub2api] export -> {single_path}")
    log(f"[sub2api] combined -> {combined_path}")
    return {"ok": True, "path": str(single_path), "combined_path": str(combined_path)}
