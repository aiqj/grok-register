"""Mail providers and verification codes (facade over registerlib.core)."""
from grok_register.core import (
    get_email_provider,
    get_email_and_token,
    get_oai_code,
    extract_verification_code,
    tempmail_lol_get_email_and_token,
    tempmail_lol_get_oai_code,
    get_domains,
    create_account,
    get_token,
    get_messages,
    cloudflare_get_domains,
    yyds_get_email_and_token,
    yyds_get_oai_code,
    get_max_mail_retry,
    get_code_poll_timeout,
    get_code_poll_interval,
)
