$ErrorActionPreference = "Stop"

try {
    Write-Host "=== XHS Bot Web 一键启动 ===" -ForegroundColor Cyan

    Write-Host "[1/3] 升级 pip..." -ForegroundColor Yellow
    python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip 升级失败，退出码: $LASTEXITCODE" }

    Write-Host "[2/3] 安装项目依赖..." -ForegroundColor Yellow
    python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，退出码: $LASTEXITCODE" }

    Write-Host "[3/3] 启动 Web 管理界面（使用系统 Edge/Chrome）..." -ForegroundColor Yellow
    python main.py webui
    if ($LASTEXITCODE -ne 0) { throw "项目启动失败，退出码: $LASTEXITCODE" }
}
catch {
    Write-Host ""
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
}
finally {
    Write-Host ""
    Read-Host "脚本执行结束，按回车关闭窗口"
}
