@echo off
rem 常駐プロセスに停止を要求する
setlocal
cd /d "%~dp0.."
python run.py stop
pause
