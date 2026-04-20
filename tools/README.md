# Tools

This directory contains developer-only helper scripts.

These files are not part of the normal user workflow.

Current files:

- `verify_python_syntax.py`: test-only syntax check helper. It scans project Python files and skips `.git` / `.venv` / `.runtime`.
- `verify-python-syntax.cmd`: Windows wrapper for `verify_python_syntax.py`.
- `manage-chatbridge-stack.ps1`: developer-only process management helper for starting/stopping the backend without the desktop UI.
- `smoke_weixin_bridge.py`: developer-only smoke validation for bridge commands, task lookup, and localized multiline output.

Recommended validation commands:

```powershell
python tools/verify_python_syntax.py
python -m unittest discover -s tests -p "test_*.py" -v
python tools/smoke_weixin_bridge.py
```

Do not treat the files in this directory as product entry points.
