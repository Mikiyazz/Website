@echo off
chcp 65001 >nul
echo ========================================================
echo   学院社会实践材料平台 - 本地快速调试服务器
echo   支持 Python 代码与前端 HTML 模板实时热重载（无需重启）
echo ========================================================
cd /d "%~dp0mainbody"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] 未找到虚拟环境，请先确认 mainbody\.venv 存在。
    pause
    exit /b 1
)

echo [*] 正在检查并同步数据库迁移...
.\.venv\Scripts\python.exe manage.py migrate --noinput
if errorlevel 1 (
    echo [ERROR] 数据库迁移失败，请检查上方报错信息。
    pause
    exit /b 1
)

echo [*] 正在启动 Django 开发服务器...
echo [*] 访问地址: http://127.0.0.1:8080/
echo [*] 按 Ctrl+C 即可停止服务。
echo.
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8080
pause
