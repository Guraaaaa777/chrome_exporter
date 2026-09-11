"""ログ出力の設定（ローテーション付きファイル + 任意でコンソール）。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import Config

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(config: Config, console: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    formatter = logging.Formatter(_FORMAT)

    try:
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            config.log_path,
            maxBytes=config.log_max_bytes,
            backupCount=config.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # ログが書けなくても本処理は続行する
        print(f"ログファイルを開けませんでした: {exc}", file=sys.stderr)

    # pythonw.exe では stdout/stderr が None になるためコンソール出力は付けない
    if console and sys.stderr is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)
