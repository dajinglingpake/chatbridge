@echo off
setlocal
cd /d "%~dp0"
start "" pythonw "%~dp0main.py" --native
