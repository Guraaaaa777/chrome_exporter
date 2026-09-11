@echo off
rem 現在の設定と前回実行の状況を表示する
setlocal
cd /d "%~dp0.."
python run.py status
pause
