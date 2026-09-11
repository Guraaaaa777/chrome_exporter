@echo off
rem タスクスケジューラから登録を削除する
setlocal
cd /d "%~dp0.."
python run.py uninstall %*
pause
