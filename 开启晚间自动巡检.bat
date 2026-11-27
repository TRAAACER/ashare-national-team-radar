@echo off
chcp 65001 >nul
cd /d "%~dp0"
.\.venv\Scripts\python.exe -c "from src.autostart import set_autostart; ok, msg = set_autostart(True, '22:30'); print(msg)"
pause
