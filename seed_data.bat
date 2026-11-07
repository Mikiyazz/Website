@echo off
chcp 65001 >nul
echo 正在重置/填充初始测试账号与模拟申报数据...
cd /d "%~dp0mainbody"
.\.venv\Scripts\python.exe scripts\seed_demo_data.py
echo.
echo 初始化完成！
pause
