"""起動処理。"""

from __future__ import annotations

import argparse
import ctypes
import logging
import sys
from pathlib import Path

from . import APP_TITLE, __version__
from .config import Config, ConfigError, load_config
from .logging_setup import setup_logging

LOGGER = logging.getLogger(__name__)

MUTEX_NAME = "Local\\chrome_history_exporter_single_instance"
APP_USER_MODEL_ID = "ChromeHistoryExporter"
ERROR_ALREADY_EXISTS = 183
MB_ICONINFORMATION = 0x40
_mutex = None  # プロセスが終わるまで握っておく


def acquire_single_instance() -> bool:
    """二重起動を防ぐ（同じ期間を 2 つのプロセスで書き出さないように）。"""
    global _mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def prepare_windows() -> None:
    """高 DPI の画面でぼやけないようにし、タスクバーに独自のアイコンを出す。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ChromeHistoryExporter",
        description="Chrome の閲覧履歴を定期的にファイルへ書き出します。",
    )
    parser.add_argument("--minimized", action="store_true", help="トレイに格納した状態で起動する")
    parser.add_argument("--config", type=Path, help="設定ファイル(JSON)のパス")
    # exe にはコンソールが無く、argparse のエラー表示で落ちるため未知の引数は無視する
    args, unknown = parser.parse_known_args(argv)

    startup_warning = None
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        config = Config(config_path=args.config.expanduser() if args.config else None)
        startup_warning = f"設定ファイルを読めなかったため、既定値で起動しました。\n{exc}"

    if not acquire_single_instance():
        ctypes.windll.user32.MessageBoxW(
            None,
            "既に起動しています。タスクトレイのアイコンから画面を開いてください。",
            APP_TITLE,
            MB_ICONINFORMATION,
        )
        return 1

    setup_logging(config.log_path)
    prepare_windows()
    LOGGER.info("起動しました（バージョン %s）", __version__)
    if unknown:
        LOGGER.info("未知の引数を無視しました: %s", " ".join(unknown))
    if startup_warning:
        LOGGER.warning("%s", startup_warning)

    from .gui import App  # tkinter は画面を出すときだけ読み込む

    try:
        App(config, minimized=args.minimized, startup_warning=startup_warning).run()
    except Exception:
        LOGGER.exception("予期しないエラーで終了しました")
        raise
    LOGGER.info("終了しました")
    return 0
