@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".venv\Scripts\python.exe" goto run
call setup.cmd
if errorlevel 1 (
  echo Setup failed.
  pause
  exit /b 1
)
:run
".venv\Scripts\python.exe" "%~1"
set result=%errorlevel%
echo.
echo Session ended.
pause
exit /b %result%
