"""このPC（Windows）での既定パス。"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "ChromeHistoryExporter"


def local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def app_data_dir() -> Path:
    """設定・状態・ログを置くフォルダ（旧コマンドライン版と同じなので続きから動く）。"""
    return local_appdata() / APP_NAME


def default_output_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents" / "ChromeHistory"


def default_config_path() -> Path:
    return app_data_dir() / "config.json"


def find_user_data_dir() -> Path | None:
    """Chrome のユーザーデータフォルダを探す。見つからなければ None。"""
    path = local_appdata() / "Google" / "Chrome" / "User Data"
    return path if path.is_dir() else None
