"""Windows タスクスケジューラへの登録 / 解除。

schtasks の /tr オプションはコマンド文字列中の引用符の扱いが不安定なため
（Python のインストール先は "C:\\Program Files\\..." のように空白を含みやすい）、
タスク定義 XML を生成して /xml で登録する。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import paths

TASK_NAME = "ChromeHistoryExporter"
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class TaskError(Exception):
    pass


def _pythonw() -> Path:
    """コンソールウィンドウを出さない pythonw.exe を探す。無ければ python.exe。"""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.is_file() else exe


def entry_script() -> Path:
    """タスクから起動するエントリスクリプト（リポジトリ直下の run.py）。"""
    return Path(__file__).resolve().parent.parent / "run.py"


def _current_user() -> str | None:
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME")
    if not user:
        return None
    return f"{domain}\\{user}" if domain else user


def build_command(mode: str, config_path: Path | None) -> tuple[Path, str, Path]:
    """(実行ファイル, 引数, 作業ディレクトリ) を返す。"""
    script = entry_script()
    if not script.is_file():
        raise TaskError(f"エントリスクリプトが見つかりません: {script}")

    sub = "run" if mode == "resident" else "once"
    arguments = f'"{script}" {sub}'
    if config_path:
        arguments += f' --config "{Path(config_path).resolve()}"'
    return _pythonw(), arguments, script.parent


def build_task_xml(
    mode: str = "resident",
    interval_minutes: int = 60,
    config_path: Path | None = None,
    start: datetime | None = None,
    task_name: str = TASK_NAME,
) -> str:
    """タスク定義 XML を組み立てる。"""
    if mode not in ("resident", "interval"):
        raise TaskError(f"未知の mode: {mode!r}")
    if mode == "interval" and not 1 <= interval_minutes <= 44640:  # 1 分〜31 日
        raise TaskError("interval モードの間隔は 1〜44640 分にしてください")

    command, arguments, workdir = build_command(mode, config_path)
    user = _current_user()
    user_tag = f"\n      <UserId>{escape(user)}</UserId>" if user else ""

    if mode == "resident":
        # ログオン時に 1 つだけ起動し、以降はアプリ側のループで定期実行する
        trigger = f"""    <LogonTrigger>
      <Enabled>true</Enabled>{user_tag}
    </LogonTrigger>"""
        # 常駐なので実行時間の上限は設けず、落ちたら再起動させる
        execution_limit = "PT0S"
        restart = """
    <RestartOnFailure>
      <Interval>PT5M</Interval>
      <Count>3</Count>
    </RestartOnFailure>"""  # Settings の末尾に入る
    else:
        # スケジューラ側が一定間隔で 1 回ずつ実行する
        boundary = (start or datetime.now().astimezone()).replace(second=0, microsecond=0)
        trigger = f"""    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>{boundary.strftime('%Y-%m-%dT%H:%M:%S')}</StartBoundary>
      <Repetition>
        <Interval>PT{interval_minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>"""
        execution_limit = "PT1H"
        restart = ""

    principal_user = f"\n      <UserId>{escape(user)}</UserId>" if user else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{TASK_NS}">
  <RegistrationInfo>
    <Description>Chrome の閲覧履歴を定期的にファイルへエクスポートします。</Description>
    <URI>\\{escape(task_name)}</URI>
  </RegistrationInfo>
  <Triggers>
{trigger}
  </Triggers>
  <Principals>
    <Principal id="Author">{principal_user}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{execution_limit}</ExecutionTimeLimit>
    <Priority>7</Priority>{restart}
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise TaskError("schtasks が見つかりません（Windows 以外では利用できません）") from exc


def install(
    mode: str = "resident",
    interval_minutes: int = 60,
    config_path: Path | None = None,
    task_name: str = TASK_NAME,
) -> str:
    """タスクを登録し、登録した実行コマンドを返す。

    mode="resident": ログオン時に常駐プロセスを起動する（アプリ側で定期実行）。
    mode="interval": スケジューラが interval_minutes ごとに 1 回実行する。
    """
    if not paths.is_windows():
        raise TaskError("この機能は Windows でのみ利用できます")

    xml = build_task_xml(mode, interval_minutes, config_path, task_name=task_name)
    # schtasks /xml は UTF-16 (BOM 付き) の XML を期待する
    handle, temp_name = tempfile.mkstemp(suffix=".xml")
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        temp_path.write_bytes(xml.encode("utf-16"))
        result = _run(["schtasks", "/create", "/tn", task_name, "/xml", str(temp_path), "/f"])
    finally:
        temp_path.unlink(missing_ok=True)

    if result.returncode != 0:
        raise TaskError(f"タスク登録に失敗しました: {result.stderr.strip() or result.stdout.strip()}")

    command, arguments, _ = build_command(mode, config_path)
    return f'"{command}" {arguments}'


def uninstall(task_name: str = TASK_NAME) -> None:
    if not paths.is_windows():
        raise TaskError("この機能は Windows でのみ利用できます")
    result = _run(["schtasks", "/delete", "/tn", task_name, "/f"])
    if result.returncode != 0:
        raise TaskError(f"タスク削除に失敗しました: {result.stderr.strip() or result.stdout.strip()}")


def query(task_name: str = TASK_NAME) -> str | None:
    if not paths.is_windows():
        return None
    result = _run(["schtasks", "/query", "/tn", task_name, "/fo", "list"])
    return result.stdout.strip() if result.returncode == 0 else None
