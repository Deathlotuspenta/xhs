@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === XHS Bot Web 一键启动 ===
echo [1/3] 升级 pip...
python -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [2/3] 安装项目依赖...
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [3/3] 启动 Web 管理界面（使用系统 Edge/Chrome）...
python main.py webui
if errorlevel 1 goto :fail
goto :end

:fail
echo.
echo 启动失败，请检查上方报错信息。
echo 退出码: %errorlevel%
pause

:end
echo.
echo 脚本执行完成，按任意键关闭窗口...
pause
