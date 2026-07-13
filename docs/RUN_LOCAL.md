# grok-register local run notes

Configured project path:

```text
D:\Feishu\grok-register
```

Safe verification only:

```powershell
cd D:\Feishu\grok-register
.\verify_config_safe.ps1
```

Manual one-account CLI launch:

```powershell
cd D:\Feishu\grok-register
.\run_cli_manual_once.ps1
```

The current `config.json` is set to `register_count = 1`, `email_provider = yyds`, and remote grok2api write is enabled. The script does not type `start` automatically; type `start` yourself in the CLI prompt to begin the project’s own browser-based flow.
