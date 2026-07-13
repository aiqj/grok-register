# Clean package notes

This package intentionally excludes local/private runtime data:
- config.json and config backups
- accounts_*.txt
- mail_credentials.txt
- exports/cpa/
- exports/sub2api/
- .venv/
- __pycache__/
- runtime_crash.log

Before use, copy config.example.json to config.json and fill in your own keys/endpoints.
