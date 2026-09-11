@echo off
rem 今すぐ 1 回だけエクスポートする
setlocal
cd /d "%~dp0.."
python run.py once %*
pause
