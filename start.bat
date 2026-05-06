@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: Session Manager 启动脚本 (CMD/Batch)
:: 如需固定使用特定 Python 路径，请设置环境变量：
::     set SM_PYTHON=C:\path\to\python.exe

cd /d "%~dp0"
set "PROJECT_DIR=%cd%"
set "PORT=7821"

:: 查找 Python 解释器
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

echo [错误] 未找到 Python 解释器。请安装 Python 3.11+ 或创建虚拟环境。
echo        python -m venv .venv
pause
exit /b 1

:found_python
echo [信息] Python: %PYTHON_EXE%

:: 检查依赖
%PYTHON_EXE% -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo [信息] 依赖未安装，正在安装...
    %PYTHON_EXE% -m pip install -r requirements.txt
)

:: 检查端口占用
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul
if %errorlevel% == 0 (
    echo [警告] 端口 %PORT% 已被占用！
    echo        请先关闭之前的窗口，或运行以下命令结束进程：
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
        echo        taskkill /PID %%a /F
        goto :port_found
    )
    :port_found
    pause
    exit /b 1
)

echo ========================================
echo   Session Manager 启动中...
echo   URL: http://127.0.0.1:%PORT%
echo   按 Ctrl+C 停止服务
echo ========================================

%PYTHON_EXE% -m uvicorn main:app --host 127.0.0.1 --port %PORT% --reload
pause
