"""Project / package path helpers (src layout aware)."""
from __future__ import annotations

from pathlib import Path

# .../src/grok_register
PACKAGE_DIR = Path(__file__).resolve().parent


def project_root() -> Path:
    """Repository root (parent of ``src/`` in src layout)."""
    parent = PACKAGE_DIR.parent
    if parent.name == "src":
        return parent.parent
    # flat-layout fallback
    return parent


PROJECT_ROOT = project_root()
