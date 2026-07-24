@echo off
setlocal
cd /d "%~dp0.."
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3.11 appdock.py --host 127.0.0.1 --port 8765
) else (
  python appdock.py --host 127.0.0.1 --port 8765
)
