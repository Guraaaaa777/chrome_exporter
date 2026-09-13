"""ログオン時の自動起動（HKCU の Run キー）と、旧コマンドライン版のタスクの後始末。"""

from __future__ import annotations

import logging
import subprocess
import sys
import winreg
from pathlib import Path

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ChromeHistoryExporter"
LEGACY_TASK_NAME = "ChromeHistoryExporter"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def launch_command() -> str:
    """ログオン時に実行するコマンド（トレイに格納した状態で起動する）。"""
    if getattr(sys, "frozen", False):  # PyInstaller でビルドした exe
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.is_file() else exe}" "{PROJECT_ROOT / "run.py"}" --minimized'


def registered_command() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def is_enabled() -> bool:
    return registered_command() is not None


def enable() -> str:
    command = launch_command()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    logger.info("ログオン時の自動起動を登録しました: %s", command)
    return command


def disable() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return
    logger.info("ログオン時の自動起動を解除しました")


# ---- 旧コマンドライン版 ------------------------------------------------------


def legacy_task_exists(task_name: str = LEGACY_TASK_NAME) -> bool:
    result = _schtasks("/query", "/tn", task_name)
    return result is not None and result.returncode == 0


def remove_legacy(stop_flag: Path, task_name: str = LEGACY_TASK_NAME) -> None:
    """旧版のタスクを削除し、動いている旧版の常駐プロセスに停止を要求する。"""
    result = _schtasks("/delete", "/tn", task_name, "/f")
    if result is None or result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() if result else "schtasks を実行できません"
        raise OSError(f"タスクを削除できませんでした: {detail}")
    # 旧版の常駐プロセスは 5 秒ごとに stop.flag を見て終了する
    stop_flag.parent.mkdir(parents=True, exist_ok=True)
    stop_flag.write_text("stop\n", encoding="utf-8")
    logger.info("旧版のタスク %r を削除し、常駐プロセスに停止を要求しました", task_name)


def _schtasks(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["schtasks", *args],
            capture_output=True,
            text=True,
            errors="replace",
            # コンソールのない exe から呼んでも黒い窓を出さない
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        logger.warning("schtasks を実行できません: %s", exc)
        return None
