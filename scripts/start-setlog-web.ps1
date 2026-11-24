# setlog PC Web を起動して既定のブラウザで開く。
# 使い方: 右クリック → PowerShellで実行、または  pwsh -File .\scripts\start-setlog-web.ps1
param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    Write-Error 'Pythonが見つかりません。setlog-mcpをインストールした環境で実行してください。'
    exit 1
}

$url = "http://127.0.0.1:$Port/"
Write-Host "setlog PC Web: $url" -ForegroundColor Cyan
if (-not $NoBrowser) {
    Start-Process $url
}
& $python -m setlog_web --port $Port
