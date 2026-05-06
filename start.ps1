#Requires -Version 5.1
<#
.SYNOPSIS
    Session Manager 启动脚本 (PowerShell)
.DESCRIPTION
    自动检测虚拟环境 Python，检查端口占用，启动 Uvicorn 服务。
    如需固定使用特定 Python 路径，可设置环境变量：
        $env:SM_PYTHON = "C:\path\to\python.exe"
#>

$ErrorActionPreference = "Stop"

# 项目根目录（脚本所在目录）
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ProjectDir

# 查找 Python 解释器（优先级：环境变量 > .venv > venv > 系统 PATH）
function Find-Python {
    if ($env:SM_PYTHON -and (Test-Path $env:SM_PYTHON)) {
        return $env:SM_PYTHON
    }
    $candidates = @(
        Join-Path $ProjectDir ".venv\Scripts\python.exe"
        Join-Path $ProjectDir "venv\Scripts\python.exe"
        "python"
        "python3"
    )
    foreach ($c in $candidates) {
        $resolved = Get-Command $c -ErrorAction SilentlyContinue
        if ($resolved) {
            return $resolved.Source
        }
    }
    throw "未找到 Python 解释器。请安装 Python 3.11+ 或创建虚拟环境（python -m venv .venv）"
}

$PythonExe = Find-Python
Write-Host "Python: $PythonExe" -ForegroundColor DarkGray

# 检查依赖是否已安装
$hasFastAPI = & $PythonExe -c "import fastapi" 2>$null
if (-not $hasFastAPI) {
    Write-Host "依赖未安装，正在安装..." -ForegroundColor Yellow
    & $PythonExe -m pip install -r requirements.txt
}

# 检查端口占用
$Port = 7821
$conn = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if ($conn) {
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    Write-Host "警告: 端口 $Port 已被占用 (进程: $($proc.ProcessName), PID: $($conn.OwningProcess))" -ForegroundColor Red
    Write-Host "请先关闭之前的窗口，或运行: Stop-Process -Id $($conn.OwningProcess) -Force" -ForegroundColor Yellow
    exit 1
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Session Manager 启动中..." -ForegroundColor Cyan
Write-Host "  URL: http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "  按 Ctrl+C 停止服务" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan

& $PythonExe -m uvicorn main:app --host 127.0.0.1 --port $Port --reload
