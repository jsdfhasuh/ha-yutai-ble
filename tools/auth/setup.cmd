@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
where py >nul 2>nul
if errorlevel 1 goto python
py -3 -m venv .venv
if errorlevel 1 exit /b 1
goto install
:python
python -m venv .venv
if errorlevel 1 (
  echo Install Python 3.11 or newer, then try again.
  exit /b 1
)
:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
exit /b %errorlevel%
