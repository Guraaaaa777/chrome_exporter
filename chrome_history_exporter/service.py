"""エクスポートの実行と、バックグラウンドでの定期実行ループ。"""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import chrome, exporter
from .config import Config
from .state import State
from .timeutil import floor_to_minute, now_local

logger = logging.getLogger(__name__)

# 停止フラグを確認する間隔（秒）
_POLL_SECONDS = 5


@dataclass
class ExportResult:
    start: datetime
    end: datetime
    record_count: int
    file_path: Path | None
    skipped_reason: str | None = None


def stop_flag_path(config: Config) -> Path:
    return config.state_path.with_name("stop.flag")


def run_once(config: Config, now: datetime | None = None, force: bool = False) -> ExportResult:
    """前回の続きから現在までの履歴を 1 ファイルに書き出す。"""
    config.validate()
    user_data_dir = config.resolve_user_data_dir()
    state = State(config.state_path)

    end = floor_to_minute(now or now_local())
    start = state.last_export_end
    if start is None:
        start = end - timedelta(hours=config.initial_lookback_hours)
        logger.info("初回実行: 過去 %d 時間分を対象にします", config.initial_lookback_hours)
    start = floor_to_minute(start.astimezone())

    if start >= end:
        logger.debug("対象期間がありません (start=%s, end=%s)", start, end)
        return ExportResult(start, end, 0, None, skipped_reason="期間なし")

    records = chrome.collect_visits(user_data_dir, config.profiles, start, end)
    logger.info("%s 〜 %s: %d 件", start, end, len(records))

    if not records and config.skip_empty and not force:
        state.record_export(end, None, 0)
        return ExportResult(start, end, 0, None, skipped_reason="履歴 0 件のため出力なし")

    path = exporter.export(
        records, start, end, config.output_path, config.output_format, config.encoding
    )
    state.record_export(end, path, len(records))
    return ExportResult(start, end, len(records), path)


def run_forever(config: Config, stop_event: threading.Event | None = None) -> None:
    """interval_minutes 間隔でエクスポートし続ける（常駐モード）。"""
    config.validate()
    stop = stop_event or threading.Event()
    _install_signal_handlers(stop)
    flag = stop_flag_path(config)
    flag.unlink(missing_ok=True)

    interval = timedelta(minutes=config.interval_minutes)
    logger.info(
        "常駐モードを開始しました（間隔: %d 分, 出力先: %s）",
        config.interval_minutes,
        config.output_path,
    )

    while not stop.is_set():
        try:
            result = run_once(config)
            if result.skipped_reason:
                logger.info("スキップ: %s", result.skipped_reason)
        except Exception:  # 1 回の失敗で常駐を止めない
            logger.exception("エクスポートに失敗しました。次回の実行で再試行します")

        if _wait(stop, interval, flag):
            break

    flag.unlink(missing_ok=True)
    logger.info("常駐モードを終了しました")


def _wait(stop: threading.Event, interval: timedelta, flag: Path) -> bool:
    """次回実行まで待つ。停止要求を受けたら True を返す。"""
    remaining = interval.total_seconds()
    while remaining > 0:
        if stop.wait(min(_POLL_SECONDS, remaining)):
            return True
        if flag.exists():
            logger.info("停止フラグを検出しました: %s", flag)
            return True
        remaining -= _POLL_SECONDS
    return stop.is_set()


def _install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum, _frame):  # noqa: ANN001
        logger.info("シグナル %s を受信しました。終了します", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # メインスレッド以外では登録できない
            pass
