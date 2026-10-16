@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
if not exist logs mkdir logs
for /f "tokens=*" %%a in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TIMESTAMP=%%a
set LOGFILE=logs\run_!TIMESTAMP!.log
echo [%date% %time%] Starting PersonalAgent Run... >> "!LOGFILE!"
call .venv\Scripts\python.exe main.py >> "!LOGFILE!" 2>&1
set EXITCODE=%errorlevel%
echo [%date% %time%] Execution completed with code %EXITCODE% >> "!LOGFILE!"
exit /b %EXITCODE%
