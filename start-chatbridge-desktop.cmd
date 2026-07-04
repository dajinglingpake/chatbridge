@echo off
setlocal
cd /d "%~dp0"
start "" pythonw "%~dp0main.py" --host 0.0.0.0 --native
