@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: Session Manager start script (CMD/Batch)
:: To use specific Python, set env var:
::     set SM_PYTHON=C:\path\to\python.exe

cd /d "%~dp0"
set "PROJECT_DIR=%cd%"
set "PORT=7821"

if defined SM_PYTHON (
    if exist "%SM_PYTHON%" (
        set "PYTHON_EXE=%SM_PYTHON%"
        goto :found_python
    )
)

if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
    goto :found_python
)
if exist "%PROJECT_DIR%\venv\Scripts\python.exe" (
    set "PYTHON_EXE=%PROJECT_DIR%\venv\Scripts\python.exe"
    goto :found_python
)

for %%P in (python python3) do (
    %%P --version >nul 2>&1 && (
        set "PYTHON_EXE=%%P"
        goto :found_python
    )
)

echo [ERROR] Python not found. Please install Python 3.11+ or create venv.
echo         python -m venv .venv
pause
exit /b 1

:found_python
echo [INFO] Python: %PYTHON_EXE%

%PYTHON_EXE% -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing dependencies...
    %PYTHON_EXE% -m pip install -r requirements.txt
)

netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul
if %errorlevel% == 0 (
    echo [WARN] Port %PORT% is already in use!
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
        echo         taskkill /PID %%a /F
        goto :port_found
    )
    :port_found
    pause
    exit /b 1
)

echo ========================================
echo   Session Manager starting...
echo   URL: http://127.0.0.1:%PORT%
echo   Press Ctrl+C to stop
echo ========================================

%PYTHON_EXE% -m uvicorn main:app --host 127.0.0.1 --port %PORT% --reload
pause