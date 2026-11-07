# 学院社会实践材料平台 - 本地快速调试脚本
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\mainbody"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  学院社会实践材料平台 - 本地开发服务器" -ForegroundColor Cyan
Write-Host "  支持 Python 业务逻辑与 HTML/CSS 实时热更新" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Error "未找到虚拟环境 .venv\Scripts\python.exe"
}

Write-Host "[*] 启动服务中..." -ForegroundColor Green
Write-Host "[*] 本地浏览器访问: http://127.0.0.1:8080/" -ForegroundColor Yellow
Write-Host "[*] 退出请按 Ctrl + C" -ForegroundColor Gray
Write-Host ""

.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8080
