"""Signup page flow (facade over registerlib.core)."""
from grok_register.core import (
    open_signup_page,
    click_email_signup_button,
    fill_email_and_submit,
    fill_code_and_submit,
    fill_profile_and_submit,
    wait_for_sso_cookie,
    has_profile_form,
    getTurnstileToken,
    build_profile,
    CloudflareBlockedError,
    ProxyOrNetworkPageError,
    raise_if_cloudflare_block,
    page_looks_like_cloudflare,
)
