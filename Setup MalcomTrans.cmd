@echo off
setlocal
cd /d "%~dp0"
echo MalcomTrans setup: creating .venv and installing requirements...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || goto fail
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto fail
if not exist ".env" copy ".env.example" ".env" >nul && echo Created .env - fill in your Turso values.
echo.
echo Setup complete. Start the app with "Start MalcomTrans.cmd".
pause
exit /b 0
:fail
echo Setup failed. Install Python 3.10+ from python.org and try again.
pause
exit /b 1
