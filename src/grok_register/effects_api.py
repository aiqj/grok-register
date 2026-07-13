"""Post-register side effects (facade over registerlib.core)."""
from grok_register.core import (
    enable_nsfw_for_token,
    add_token_to_grok2api_pools,
    run_cpa_and_sub2api_export,
    upload_cpa_auth_file_to_cloud,
    upload_cpa_auth_dir_to_cloud,
    list_cpa_auth_files_on_cloud,
    delete_cpa_auth_file_on_cloud,
    delete_cpa_auth_files_on_cloud,
    delete_all_cpa_auth_files_on_cloud,
    match_cpa_auth_files,
    upload_sub2api_data_file_to_cloud,
    upload_sub2api_dir_to_cloud,
    mark_used,
    mark_error,
    save_cookies_snapshot,
)
