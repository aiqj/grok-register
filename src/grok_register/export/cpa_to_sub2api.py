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
      expires_at (RFC3339 string), client_id, email,
      base_url (default: preserve CPA free path cli-chat-proxy)

base_url policy (sub2api_base_url_mode):
  - preserve (default): keep CPA base_url; empty → cli-chat-proxy
  - cli_chat_proxy: always https://cli-chat-proxy.grok.com/v1
  - api_xai: force https://api.x.ai/v1 (legacy)
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
# Free Build path — matches working Sub2API reference exports that can call models.
CLI_CHAT_PROXY_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
# Public API base (legacy remap target; often unsuitable for free OAuth tokens).
XAI_API_BASE_URL = "https://api.x.ai/v1"
# Backward-compatible alias (older code / docs referred to this as default).
XAI_DEFAULT_BASE_URL = CLI_CHAT_PROXY_BASE_URL
XAI_DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
XAI_REDIRECT_URI = "http://127.0.0.1:56121/callback"

# preserve | cli_chat_proxy | api_xai
SUB2API_BASE_URL_MODE_DEFAULT = "preserve"
_VALID_BASE_URL_MODES = frozenset({"preserve", "cli_chat_proxy", "api_xai"})


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


def _resolve_base_url_mode(mode: str | None) -> str:
    text = str(mode or SUB2API_BASE_URL_MODE_DEFAULT).strip().lower().replace("-", "_")
    aliases = {
        "default": "preserve",
        "keep": "preserve",
        "cpa": "preserve",
        "cli": "cli_chat_proxy",
        "cli_chat": "cli_chat_proxy",
        "cli-chat-proxy": "cli_chat_proxy",
        "free": "cli_chat_proxy",
        "api": "api_xai",
        "api.x.ai": "api_xai",
        "xai": "api_xai",
        "legacy": "api_xai",
    }
    text = aliases.get(text, text)
    if text not in _VALID_BASE_URL_MODES:
        return SUB2API_BASE_URL_MODE_DEFAULT
    return text


def _ensure_v1_suffix(url: str) -> str:
    text = str(url or "").strip().rstrip("/")
    if not text:
        return text
    if re.search(r"/v1$", text):
        return text
    return text + "/v1"


def _normalize_base_url(raw: str | None, mode: str | None = None) -> str:
    """
    Align Sub2API credentials.base_url with known-working free Grok path by default.

    Working reference exports use https://cli-chat-proxy.grok.com/v1 (not api.x.ai).
    Older builds remapped cli-chat-proxy → api.x.ai; that breaks free OAuth tokens.
    """
    resolved = _resolve_base_url_mode(mode)
    if resolved == "cli_chat_proxy":
        return CLI_CHAT_PROXY_BASE_URL
    if resolved == "api_xai":
        text = str(raw or "").strip().rstrip("/")
        if not text:
            return XAI_API_BASE_URL
        lower = text.lower()
        if "cli-chat-proxy.grok.com" in lower:
            return XAI_API_BASE_URL
        if lower in ("https://api.x.ai", "http://api.x.ai"):
            return XAI_API_BASE_URL
        if lower.startswith("https://api.x.ai/") or lower.startswith("http://api.x.ai/"):
            return text if text.endswith("/v1") or "/v1/" in text + "/" else XAI_API_BASE_URL
        if lower.startswith("http://") or lower.startswith("https://"):
            return text
        return XAI_API_BASE_URL

    # preserve (default): keep CPA / mint base_url; empty → free Build path
    text = str(raw or "").strip().rstrip("/")
    if not text:
        return CLI_CHAT_PROXY_BASE_URL
    lower = text.lower()
    if "cli-chat-proxy.grok.com" in lower:
        return _ensure_v1_suffix(text) if not re.search(r"/v1$", text) else text
    if lower in ("https://api.x.ai", "http://api.x.ai"):
        return XAI_API_BASE_URL
    if lower.startswith("https://api.x.ai/") or lower.startswith("http://api.x.ai/"):
        return text if re.search(r"/v1$", text) or "/v1/" in text + "/" else XAI_API_BASE_URL
    if lower.startswith("http://") or lower.startswith("https://"):
        return text
    return CLI_CHAT_PROXY_BASE_URL


def _display_name_from_tokens(id_token: str, email: str, sub: str) -> str:
    """Prefer profile name (reference exports use short display names like \"To\")."""
    idp = _jwt_payload(id_token)
    given = str(idp.get("given_name") or "").strip()
    family = str(idp.get("family_name") or "").strip()
    if given and family:
        return f"{given}{family}" if not given.isascii() else f"{given} {family}".strip()
    if given:
        return given
    if family:
        return family
    name = str(idp.get("name") or "").strip()
    if name:
        return name
    return email or sub or "Grok OAuth Account"


def _token_version_ms(access_token: str, cpa: dict[str, Any] | None = None) -> int:
    """Match working Sub2API exports: credentials._token_version as unix ms."""
    cpa = cpa or {}
    # Prefer access token iat (ms), else exp, else wall clock.
    at = _jwt_payload(access_token)
    for key in ("iat", "exp"):
        sec = _parse_time_to_unix_seconds(at.get(key))
        if sec is not None:
            return int(sec) * 1000
    for key in ("last_refresh", "expired", "expires_at"):
        sec = _parse_time_to_unix_seconds(cpa.get(key))
        if sec is not None:
            return int(sec) * 1000
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def cpa_xai_to_sub2api_account(
    cpa: dict[str, Any],
    *,
    source: str = "cpa_xai",
    base_url_mode: str | None = None,
) -> dict[str, Any]:
    """Convert CPA xAI auth to Sub2API account aligned with working admin exports.

    Field set mirrors known-good Sub2API re-exports (platform=grok oauth):
      credentials: _token_version, access_token, base_url, client_id, email,
                   expires_at, id_token, refresh_token, scope, token_type
      account: name, platform, type, credentials, extra{email}, concurrency,
               priority, rate_multiplier, auto_pause_on_expired

    Intentionally omits CPA-only / non-reference fields (headers, token_endpoint,
    redirect_uri, sub, expires_in, import metadata) so import shape matches the
    reference that can call models.
    """
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
    base_url = _normalize_base_url(cpa.get("base_url"), mode=base_url_mode)

    name = _display_name_from_tokens(id_token, email, sub)

    # Credential key order / set aligned with working Sub2API export reference.
    credentials: dict[str, Any] = {
        "_token_version": _token_version_ms(access_token, cpa),
        "access_token": access_token,
        "base_url": base_url,
        "client_id": client_id,
    }
    if email:
        credentials["email"] = email
    if expires_rfc3339:
        credentials["expires_at"] = expires_rfc3339
    if id_token:
        credentials["id_token"] = id_token
    if refresh_token:
        credentials["refresh_token"] = refresh_token
    if scope:
        credentials["scope"] = scope
    credentials["token_type"] = token_type

    # Do NOT put CPA-only headers / token_endpoint / redirect_uri / sub / expires_in.

    account: dict[str, Any] = {
        "name": name,
        "platform": PLATFORM_GROK,  # critical: Sub2API platform id is "grok"
        "type": ACCOUNT_TYPE_OAUTH,
        "credentials": credentials,
        # Reference working export: extra only carries email (runtime snapshots omitted)
        "extra": {"email": email} if email else {},
        "concurrency": 1,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }
    # Optional account-level unix expires (DataAccount); reference re-export often omits it.
    # Keep it — helps auto_pause; harmless for import.
    if expires_sec is not None:
        account["expires_at"] = int(expires_sec)

    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if x is not None and x != ""}
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


def convert_cpa_file(
    cpa_path: str | Path,
    out_dir: str | Path | None = None,
    *,
    base_url_mode: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    cpa_path = Path(cpa_path).expanduser().resolve()
    cpa = json.loads(cpa_path.read_text(encoding="utf-8-sig"))
    account = cpa_xai_to_sub2api_account(cpa, source="cpa_xai", base_url_mode=base_url_mode)
    doc = build_sub2api_document([account])
    out_dir = Path(out_dir or _default_sub2api_dir_next_to_cpa(cpa_path)).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"sub2api-{cpa_path.stem}.json"
    out_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_file, doc


def rebuild_combined(
    cpa_dir: str | Path,
    out_file: str | Path,
    *,
    base_url_mode: str | None = None,
) -> Path:
    cpa_dir = Path(cpa_dir).expanduser().resolve()
    accounts: list[dict[str, Any]] = []
    for p in sorted(cpa_dir.glob("xai-*.json")):
        try:
            cpa = json.loads(p.read_text(encoding="utf-8-sig"))
            accounts.append(
                cpa_xai_to_sub2api_account(cpa, source="cpa_xai", base_url_mode=base_url_mode)
            )
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
    base_url_mode = _resolve_base_url_mode(cfg.get("sub2api_base_url_mode"))
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
    single_path, doc = convert_cpa_file(
        cpa_path, out_dir=out_dir, base_url_mode=base_url_mode
    )
    combined_raw = str(cfg.get("sub2api_combined_file") or "").strip()
    if combined_raw:
        combined_path = Path(combined_raw).expanduser()
        if not combined_path.is_absolute():
            combined_path = (PROJECT_ROOT / combined_path).resolve()
    else:
        combined_path = out_dir / "sub2api-accounts.json"
    rebuild_combined(cpa_dir, combined_path, base_url_mode=base_url_mode)
    exported_base = ""
    token_report = None
    try:
        accounts = (doc or {}).get("accounts") or []
        if accounts:
            exported_base = str((accounts[0].get("credentials") or {}).get("base_url") or "")
            try:
                from grok_register.core import analyze_sub2api_oauth_account

                token_report = analyze_sub2api_oauth_account(accounts[0])
            except Exception:
                token_report = None
    except Exception:
        exported_base = ""
    log(f"[sub2api] export -> {single_path}")
    if exported_base:
        log(f"[sub2api] base_url={exported_base} (mode={base_url_mode})")
    if token_report:
        label = token_report.get("email") or token_report.get("name") or "?"
        exp_s = token_report.get("access_exp_iso") or "-"
        ttl = token_report.get("ttl_sec")
        ttl_s = f"{ttl}s" if ttl is not None else "?"
        if token_report.get("remint_recommended"):
            log(
                f"[sub2api] token UNHEALTHY {label} exp={exp_s} ttl={ttl_s} "
                f"risks={','.join(token_report.get('risks') or [])} "
                f"→ remint before upload"
            )
        else:
            log(f"[sub2api] token ok {label} exp={exp_s} ttl={ttl_s}")
    log(f"[sub2api] combined -> {combined_path}")
    return {
        "ok": True,
        "path": str(single_path),
        "combined_path": str(combined_path),
        "base_url": exported_base,
        "base_url_mode": base_url_mode,
        "token_health": token_report,
    }
