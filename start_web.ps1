$ErrorActionPreference = "Stop"

Write-Host "=== XHS Bot Web 一键启动 ===" -ForegroundColor Cyan

Write-Host "[1/4] 升级 pip..." -ForegroundColor Yellow
python -m pip install --upgrade pip

Write-Host "[2/4] 安装项目依赖..." -ForegroundColor Yellow
python -m pip install -r requirements.txt

Write-Host "[3/4] 安装 Playwright Chromium..." -ForegroundColor Yellow
python -m playwright install chromium

Write-Host "[4/4] 启动项目..." -ForegroundColor Yellow
python main.py
