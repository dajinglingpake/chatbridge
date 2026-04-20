@echo off
setlocal
set "ROOT=%~dp0..\\"
python "%ROOT%tools/verify_python_syntax.py"
endlocal
