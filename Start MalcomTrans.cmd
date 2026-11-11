@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: preparing the app...
    call "%~dp0Setup MalcomTrans.cmd" || exit /b 1
)
echo Daily Traking MalcomTrans
echo Open http://127.0.0.1:5000 in your browser. Ctrl+C stops the app.
start "" http://127.0.0.1:5000
".venv\Scripts\python.exe" "%~dp0app.py"
if errorlevel 1 pause
exit /b
