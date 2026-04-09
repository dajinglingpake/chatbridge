@echo off
setlocal
set "ROOT=%~dp0..\\"
python -m py_compile ^
  "%ROOT%localization.py" ^
  "%ROOT%main.py" ^
  "%ROOT%ui_main.py" ^
  "%ROOT%agent_hub.py" ^
  "%ROOT%runtime_stack.py" ^
  "%ROOT%weixin_hub_bridge.py"
endlocal
