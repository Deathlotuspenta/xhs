@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === XHS Bot Web 一键启动 ===
echo [1/4] 升级 pip...
python -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [2/4] 安装项目依赖...
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [3/4] 安装 Playwright Chromium...
python -m playwright install chromium
if errorlevel 1 goto :fail

echo [4/4] 启动项目...
python main.py
goto :end

:fail
echo.
echo 启动失败，请检查上方报错信息。
pause

:end
