@echo off
rem タスクスケジューラに登録する（ログオン時に常駐起動）
rem 定期実行をスケジューラ側に任せる場合: install-task.bat --mode interval
setlocal
cd /d "%~dp0.."
python run.py install %*
pause
