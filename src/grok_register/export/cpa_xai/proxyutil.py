"""CPA mint proxy helpers.

Thread-local pin + resolve_proxy for concurrent mint workers.
Chromium formatting / log labels are shared with root ``proxy_pool``
to avoid divergent behavior.
"""

from __future__ import annotations

import os
import sys
import threading
import urllib.request
from typing import Any

# Prefer shared implementations (host:port strip auth, socks5, redaction).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from grok_register.proxy.pool import (  # noqa: E402
    proxy_for_chromium,
    proxy_has_userinfo,
    proxy_log_label,
)

_thread = threading.local()
_MISSING = object()

__all__ = [
    "build_opener",
    "clear_runtime_proxy",
    "get_runtime_proxy",
    "proxy_for_chromium",
    "proxy_has_userinfo",
    "proxy_log_label",
    "resolve_proxy",
    "set_runtime_proxy",
]


def set_runtime_proxy(proxy: str | None) -> None:
    """Pin proxy for the *current thread*. Empty string pins direct (no proxy)."""
    _thread.proxy = (proxy or "").strip()
    _thread.proxy_pinned = True


def clear_runtime_proxy() -> None:
    _thread.proxy = None
    _thread.proxy_pinned = False


def get_runtime_proxy() -> str | None:
    if not getattr(_thread, "proxy_pinned", False):
        return None
    return getattr(_thread, "proxy", None) or ""


def _use_system_proxy() -> bool:
    raw = os.environ.get("USE_SYSTEM_PROXY")
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")


def resolve_proxy(explicit: str | None | object = _MISSING) -> str:
    """Resolve proxy URL. Empty string = direct (optional).

    - If *explicit* is a string (including ``""``), it wins as-is.
    - If *explicit* is None or omitted: thread pin → optional system → direct.
    - System shell proxy only when USE_SYSTEM_PROXY=1.
    """
    if explicit is not _MISSING and explicit is not None:
        return str(explicit).strip()

    if getattr(_thread, "proxy_pinned", False):
        return (getattr(_thread, "proxy", None) or "").strip()

    if _use_system_proxy():
        for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
            val = (os.environ.get(key) or "").strip()
            if val:
                return val
    return ""


class _DirectProxyHandler(urllib.request.ProxyHandler):
    """Skip all proxies — must override default getproxies()-based ProxyHandler.

    Plain ``ProxyHandler({})`` is a no-op on some Python versions (no http_open
    methods → handler never registers), so the default system ProxyHandler can
    still win, or defaults get skipped incorrectly. We register a real handler
    whose ``proxy_open`` always declines.
    """

    def __init__(self) -> None:
        # Dummy entries so handler methods exist and build_opener replaces default.
        super().__init__({"http": "http://127.0.0.1:9", "https": "http://127.0.0.1:9"})
        self.proxies = {}

    def proxy_open(self, req, proxy, type):  # noqa: A002
        return None  # force direct connection


def build_opener(proxy: str | None | object = _MISSING) -> urllib.request.OpenerDirector:
    """Build urllib opener for CPA OAuth HTTP.

    Empty proxy uses ``_DirectProxyHandler`` so the default
    ``urllib.request.getproxies()`` (e.g. macOS system proxy) is not applied.
    """
    p = resolve_proxy(proxy)
    if p:
        handler: urllib.request.BaseHandler = urllib.request.ProxyHandler(
            {"http": p, "https": p}
        )
    else:
        handler = _DirectProxyHandler()
    return urllib.request.build_opener(handler)
