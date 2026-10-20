@echo off
rem YunRun guided admin console: school setup -> token -> history -> calibrate -> lock -> face -> run
rem Same server also serves: running console http://127.0.0.1:8437/ui  and  viewer http://127.0.0.1:8437/
rem NOTE: keep this file ASCII-only. cmd.exe parses .bat in the ANSI codepage (GBK on zh-CN)
rem       and UTF-8 Chinese comments break into garbage commands.
cd /d "%~dp0"
setlocal

set "PY=python"
where %PY% >nul 2>nul || set "PY=py -3"

set "ROOT=%~dp0.."
set "PYTHONPATH=%~dp0src;%ROOT%;%ROOT%\runner;%ROOT%\tools"

echo [admin] interpreter: %PY%
%PY% -X utf8 -m gps_track_lab serve --host 127.0.0.1 --port 8437 --open-browser --open-admin
if errorlevel 1 (
  echo.
  echo [admin] start failed. Two usual causes:
  echo   1^) port 8437 already in use - close the old window, or run:
  echo        python ..\..\launcher.py --port 8438
  echo   2^) missing deps - run this in the yunrun folder:
  echo        pip install -r requirements.txt
  pause
)
endlocal
