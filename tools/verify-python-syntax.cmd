@echo off
setlocal
set "ROOT=%~dp0..\\"
python -m py_compile ^
  "%ROOT%codex_wechat_bootstrap.py" ^
  "%ROOT%localization.py" ^
  "%ROOT%main.py" ^
  "%ROOT%ui_main.py" ^
  "%ROOT%web_main.py" ^
  "%ROOT%codex_wechat_runtime.py" ^
  "%ROOT%multi_codex_hub.py" ^
  "%ROOT%weixin_hub_bridge.py"
endlocal
