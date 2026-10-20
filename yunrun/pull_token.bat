@echo off
rem Pull the latest YunRun token from the phone via ADB and write it into config.ini
rem (no login request, phone session stays alive - recommended method)
rem NOTE: keep this file ASCII-only (cmd.exe parses .bat in the ANSI codepage).
cd /d "%~dp0"
set "PY=python"
where %PY% >nul 2>nul || set "PY=py -3"
%PY% -X utf8 refresh_token_adb.py
pause
