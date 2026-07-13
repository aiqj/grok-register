#!/usr/bin/env python3
"""Project entrypoint — delegates to ``grok_register.cli``."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ is importable without editable install
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from grok_register.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main() or 0)
