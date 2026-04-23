# Tools

This directory contains helper scripts for both developers and external integrations.

Current files:

- `verify_python_syntax.py`: test-only syntax check helper. It scans project Python files and skips `.git` / `.venv` / `.runtime`.
- `verify-python-syntax.cmd`: Windows wrapper for `verify_python_syntax.py`.
- `hub_bridge_processes.ps1`: developer-only helper for starting/stopping the backend without the desktop UI.
- `smoke_weixin_bridge.py`: developer-only smoke validation for bridge commands, task lookup, and localized multiline output.
- `smoke_sender_sessions.py`: developer-only smoke validation for the sender-session path. It simulates an incoming WeChat message, runs the real Hub/MCP chain, captures async replies, and restores runtime state files afterwards.
- `run_product_acceptance.py`: product-facing acceptance runner. It chains syntax checks, key unit tests, bridge smoke, and sender-session smoke scenarios into one executable checklist.
- `run_live_acceptance.py`: real WeChat acceptance helper. It checks runtime readiness, prints recent async events, can send a test notice, and outputs a manual live-validation checklist.
- `operations_server.py`: product-facing MCP stdio server for session/task/agent operations.

Recommended validation commands:

```powershell
python tools/verify_python_syntax.py
python -m unittest discover -s tests -p "test_*.py" -v
python tools/smoke_weixin_bridge.py
python tools/smoke_sender_sessions.py --prompt "列出所有会话"
python tools/smoke_sender_sessions.py --seed-history --prompt "列出所有会话"
python tools/run_product_acceptance.py
python tools/run_live_acceptance.py --send-notice
```

MCP entry point:

```powershell
python tools/operations_server.py
```
