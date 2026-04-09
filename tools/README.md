# Tools

This directory contains developer-only helper scripts.

These files are not part of the normal user workflow.

Current files:

- `verify-python-syntax.cmd`: test-only syntax check helper. It compiles the main Python files.
- `manage-codex-wechat-stack.ps1`: developer-only process management helper for starting/stopping the backend without the desktop UI.
- `smoke_weixin_bridge.py`: developer-only smoke validation for bridge commands, task lookup, and localized multiline output.

Recommended validation commands:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tools/smoke_weixin_bridge.py
```

Do not treat the files in this directory as product entry points.
