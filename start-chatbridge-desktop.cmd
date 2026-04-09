@echo off
setlocal
cd /d "%~dp0"
python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('nicegui') else 1)" >nul 2>nul
if errorlevel 1 (
  echo Installing UI dependency: nicegui
  python -m pip install nicegui
)
start "" pythonw "%~dp0ui_main.py" --native
