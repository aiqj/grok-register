"""Browser lifecycle and options (facade over registerlib.core)."""
from grok_register.core import (
    create_browser_options,
    start_browser,
    stop_browser,
    restart_browser,
    get_page,
    get_browser_obj,
    set_page_context,
    clear_page_context,
    prepare_browser_for_next_account,
    apply_page_stealth,
    TabPool,
    configure_perf,
    PERF_FLAGS,
    cleanup_runtime_memory,
)
