import json
import os
import pathlib
import re

import requests

_ROOT = pathlib.Path(__file__).resolve().parent.parent
p = _ROOT / "config.json"
c = json.loads(p.read_text(encoding="utf-8-sig"))


def mask(v):
    v = str(v or "")
    return v[:4] + "..." + v[-4:] if len(v) > 8 else ("***" if v else "")


def split_keys(value):
    raw = str(value or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,;\s]+", raw)
    seen = set()
    keys = []
    for part in parts:
        key = part.strip().strip('"').strip("'")
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


print("config:", p.resolve())
for k in [
    "email_provider",
    "register_count",
    "max_mail_retry",
    "code_poll_timeout",
    "cpa_export_enabled",
    "cpa_auth_dir",
    "sub2api_export_enabled",
    "sub2api_combined_file",
    "grok2api_remote_base",
]:
    print(f"{k}:", c.get(k))

provider = str(c.get("email_provider") or "").strip().lower()
print("yyds_api_key:", mask(c.get("yyds_api_key")))

if provider == "yyds":
    r = requests.get(
        "https://maliapi.215.im/v1/domains",
        headers={"X-API-Key": c.get("yyds_api_key")},
        timeout=20,
    )
    print("YYDS HTTP:", r.status_code, "success:", r.json().get("success"))
elif provider in ("tempmail_lol", "tempmail.lol", "tempmail", "tempmaillol"):
    keys = split_keys(c.get("tempmail_lol_api_keys"))
    if not keys:
        keys = split_keys(c.get("tempmail_lol_api_key"))
    if not keys:
        keys = split_keys(os.environ.get("TEMPMAIL_LOL_API_KEYS", ""))
    if not keys:
        keys = split_keys(os.environ.get("TEMPMAIL_LOL_API_KEY", ""))
    base = (
        str(c.get("tempmail_lol_api_base") or "").strip().rstrip("/")
        or os.environ.get("TEMPMAIL_LOL_API_BASE", "").strip().rstrip("/")
        or "https://api.tempmail.lol/v2"
    )
    print("tempmail_lol_api_base:", base)
    print("tempmail_lol key pool size:", len(keys) if keys else 0, "(0 = free tier)")
    if keys:
        print("tempmail_lol_api_key:", mask(keys[0]))
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if keys:
        headers["Authorization"] = f"Bearer {keys[0]}"
    r = requests.post(
        f"{base}/inbox/create",
        json={"domain": None, "prefix": None},
        headers=headers,
        timeout=20,
    )
    print("TempMail.lol HTTP:", r.status_code)
    if r.status_code < 400:
        data = r.json() if r.text else {}
        print(
            "TempMail.lol create ok:",
            bool(data.get("address") and data.get("token")),
            "address:",
            data.get("address"),
        )
    else:
        print("TempMail.lol body:", (r.text or "")[:200])
else:
    print("email provider probe skipped for:", provider or "(empty)")

base = str(c.get("grok2api_remote_base") or "").rstrip("/")
app_key = c.get("grok2api_remote_app_key")
if c.get("grok2api_auto_add_remote") and base and app_key:
    rr = requests.get(base + "/admin/api/tokens", params={"app_key": app_key}, timeout=15)
    print("grok2api HTTP:", rr.status_code)
print("VERIFY OK")
