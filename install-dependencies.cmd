@echo off
setlocal
cd /d "%~dp0"
echo Python executable:
python -c "import sys; print(sys.executable)"
echo Python version:
python -c "import sys; print(sys.version)"
echo.
echo Installing Python dependencies from requirements.txt
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Failed to install Python dependencies.
  exit /b 1
)
echo.
echo Python dependencies installed successfully.
endlocal
