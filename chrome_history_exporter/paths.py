"""OS ごとの既定パス解決。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ChromeHistoryExporter"

# ブラウザ名 -> ユーザーデータディレクトリの候補（Windows は LOCALAPPDATA 相対）
_BROWSER_DIRS = {
    "chrome": {
        "win32": ["Google/Chrome/User Data"],
        "linux": [".config/google-chrome", ".config/chromium"],
        "darwin": ["Library/Application Support/Google/Chrome"],
    },
    "edge": {
        "win32": ["Microsoft/Edge/User Data"],
        "linux": [".config/microsoft-edge"],
        "darwin": ["Library/Application Support/Microsoft Edge"],
    },
    "brave": {
        "win32": ["BraveSoftware/Brave-Browser/User Data"],
        "linux": [".config/BraveSoftware/Brave-Browser"],
        "darwin": ["Library/Application Support/BraveSoftware/Brave-Browser"],
    },
}


def is_windows() -> bool:
    return sys.platform.startswith("win")


def app_data_dir() -> Path:
    """設定・状態・ログを置くディレクトリ。"""
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


def default_output_dir() -> Path:
    if is_windows():
        return Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "ChromeHistory"
    return Path.home() / "ChromeHistory"


def default_config_path() -> Path:
    return app_data_dir() / "config.json"


def _platform_key() -> str:
    if is_windows():
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def find_user_data_dir(browser: str = "chrome") -> Path | None:
    """ブラウザのユーザーデータディレクトリを自動検出する。見つからなければ None。"""
    candidates = _BROWSER_DIRS.get(browser.lower())
    if not candidates:
        return None
    key = _platform_key()
    if key == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path.home()
    for rel in candidates.get(key, []):
        path = base / rel
        if path.is_dir():
            return path
    return None
