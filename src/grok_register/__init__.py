"""Grok Register application package.

Prefer explicit imports::

    import grok_register.core as reg
    from grok_register.engine import RegistrationEngine
    from grok_register.proxy.pool import resolve_headless
"""

from __future__ import annotations

__all__ = [
    "core",
    "engine",
    "browser",
    "proxy",
    "mail",
    "export",
]


def __getattr__(name: str):
    # Lazy exports to avoid circular imports on ``import grok_register.proxy...``
    if name == "RegistrationEngine":
        from grok_register.engine import RegistrationEngine

        return RegistrationEngine
    if name == "core":
        import grok_register.core as core

        return core
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
