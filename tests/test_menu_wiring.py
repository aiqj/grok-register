"""Smoke tests for interactive menu entry wiring (no stdin loop)."""

from __future__ import annotations

import sys
from unittest.mock import patch


def test_bare_argv_enters_menu():
    from grok_register import cli as cli_mod

    with patch.object(sys, "argv", ["register_cli.py"]), patch(
        "grok_register.menu.run_interactive_menu", return_value=42
    ) as menu:
        code = cli_mod.main()
    assert code == 42
    assert menu.called


def test_menu_flag_enters_menu():
    from grok_register import cli as cli_mod

    with patch.object(sys, "argv", ["register_cli.py", "--menu"]), patch(
        "grok_register.menu.run_interactive_menu", return_value=0
    ) as menu:
        code = cli_mod.main()
    assert code == 0
    assert menu.called


def test_count_flag_does_not_enter_menu():
    """Regression: action args must not open the interactive menu."""
    from grok_register import cli as cli_mod

    with patch.object(sys, "argv", ["register_cli.py", "--cpa-list"]), patch(
        "grok_register.menu.run_interactive_menu"
    ) as menu, patch("grok_register.core.load_config"), patch(
        "grok_register.core.list_cpa_auth_files_on_cloud",
        return_value={"ok": True, "files": []},
    ), patch(
        "grok_register.proxy.pool.refresh_proxy_cache"
    ), patch(
        "grok_register.proxy.pool.print_startup_report"
    ):
        # may still hit other code; just ensure menu not called
        try:
            cli_mod.main()
        except SystemExit:
            pass
        except Exception:
            pass
    assert not menu.called
