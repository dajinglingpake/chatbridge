@echo off
setlocal
cd /d "%~dp0"
echo Python executable:
python -c "import sys; print(sys.executable)"
echo Python version:
python -c "import sys; print(sys.version)"
echo.
echo Installing desktop dependencies: PySide6 psutil
python -m pip install PySide6 psutil
if errorlevel 1 (
  echo.
  echo Failed to install desktop dependencies.
  exit /b 1
)
echo.
echo Desktop dependencies installed successfully.
endlocal
