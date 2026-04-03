@echo off
setlocal
cd /d "%~dp0"
python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('PySide6') and importlib.util.find_spec('psutil') else 1)" >nul 2>nul
if errorlevel 1 (
  echo Installing desktop dependencies: PySide6 psutil
  python -m pip install PySide6 psutil
)
start "" pythonw "%~dp0main.py"
