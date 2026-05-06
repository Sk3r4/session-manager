#Requires -Version 5.1
<#
.SYNOPSIS
    Session Manager start script (PowerShell)
.DESCRIPTION
    Auto-detect venv Python, check port, start Uvicorn.
    To use specific Python, set env var:
        $env:SM_PYTHON = "C:\path\to\python.exe"
#>

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ProjectDir

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
    throw "Python not found. Please install Python 3.11+ or create venv (python -m venv .venv)"
}

$PythonExe = Find-Python
Write-Host "Python: $PythonExe" -ForegroundColor DarkGray

$hasFastAPI = & $PythonExe -c "import fastapi" 2>$null
if (-not $hasFastAPI) {
    Write-Host "Installing dependencies..." -ForegroundColor Yellow
    & $PythonExe -m pip install -r requirements.txt
}

$Port = 7821
$conn = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if ($conn) {
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    Write-Host "Warning: Port $Port in use (process: $($proc.ProcessName), PID: $($conn.OwningProcess))" -ForegroundColor Red
    Write-Host "Close previous window or run: Stop-Process -Id $($conn.OwningProcess) -Force" -ForegroundColor Yellow
    exit 1
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Session Manager starting..." -ForegroundColor Cyan
Write-Host "  URL: http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "  Press Ctrl+C to stop" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan

& $PythonExe -m uvicorn main:app --host 127.0.0.1 --port $Port --reload