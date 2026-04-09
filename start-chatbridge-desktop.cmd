@echo off
setlocal
cd /d "%~dp0"
start "" pythonw "%~dp0ui_main.py" --native
