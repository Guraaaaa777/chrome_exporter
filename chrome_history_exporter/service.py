"""エクスポートの実行と、画面から操作する定期実行スケジューラ。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import chrome, exporter
from .config import Config, ConfigError
from .lockfile import AlreadyRunning, ProcessLock
from .state import State
from .timeutil import floor_to_minute, now_local

logger = logging.getLogger(__name__)

# 別プロセスのエクスポート完了を待つ最大秒数
_EXPORT_LOCK_TIMEOUT_SECONDS = 60
# スリープから復帰したときも予定時刻を過ぎていればすぐ動けるよう、この秒数ごとに時計を見直す
_MAX_WAIT_SECONDS = 30

# Scheduler から on_event で届く通知の種類
BUSY = "busy"  # 書き出しを始めた（payload なし）
DONE = "done"  # 書き出しが終わった（payload: ExportResult）
FAILED = "failed"  # 失敗した（payload: エラーメッセージ）
SCHEDULE = "schedule"  # 自動エクスポートの開始・停止や次回時刻が変わった（payload: 次回時刻 or None）


@dataclass
class ExportResult:
    start: datetime
    end: datetime
    record_count: int
    file_path: Path | None
    skipped_reason: str | None = None


def export_lock_path(config: Config) -> Path:
    return config.state_path.with_name("export.lock")


def run_once(config: Config, now: datetime | None = None, force: bool = False) -> ExportResult:
    """前回の続きから現在までの履歴を 1 ファイルに書き出す。

    状態ファイルの読み込みから更新までを export.lock で排他するので、別のプロセスが
    同時に動いても期間が競合しない（後から来た方は先の完了を待つ）。
    """
    config.validate()
    user_data_dir = config.resolve_user_data_dir()
    lock = ProcessLock(export_lock_path(config))
    try:
        lock.acquire(timeout=_EXPORT_LOCK_TIMEOUT_SECONDS)
    except AlreadyRunning as exc:
        raise AlreadyRunning(f"別のプロセスがエクスポート中のため実行できませんでした（ロック: {lock.path}）") from exc
    try:
        return _export(config, user_data_dir, now, force)
    finally:
        lock.release()


def _export(config: Config, user_data_dir: Path, now: datetime | None, force: bool) -> ExportResult:
    # 状態の読み込みと期間の決定はロック取得後に行う（待っている間に進んでいる可能性がある）
    state = State(config.state_path)

    end = floor_to_minute(now or now_local())
    start = state.last_export_end
    if start is None:
        start = end - timedelta(hours=config.initial_lookback_hours)
        logger.info("初回実行: 過去 %d 時間分を対象にします", config.initial_lookback_hours)
    start = floor_to_minute(start.astimezone())

    if start >= end:
        logger.debug("対象期間がありません (start=%s, end=%s)", start, end)
        return ExportResult(start, end, 0, None, skipped_reason="前回から 1 分経っていないため出力なし")

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


class Scheduler:
    """interval_minutes ごとに run_once を呼ぶ常駐スレッド。

    画面のスレッドからは start / stop / run_now / reschedule / shutdown を呼ぶだけでよい。
    結果は on_event(kind, payload) で届くが、このスレッドから呼ばれるので
    コールバック側では画面を直接触らず、キューに積むこと。
    """

    def __init__(
        self,
        get_config: Callable[[], Config],
        on_event: Callable[[str, object], None],
    ) -> None:
        self._get_config = get_config
        self._on_event = on_event
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._shutdown = False
        self._enabled = False
        self._busy = False
        self._manual_force: bool | None = None  # None: 手動の要求なし
        self._next_run: datetime | None = None
        self._last_finished: datetime | None = None
        self._thread = threading.Thread(target=self._loop, name="export-scheduler", daemon=True)
        self._thread.start()

    # ---- 状態 ----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def next_run(self) -> datetime | None:
        return self._next_run if self._enabled else None

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    # ---- 操作 ----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._enabled or self._shutdown:
                return
            self._enabled = True
            # 始めたらまず前回の続きを書き出す
            self._next_run = datetime.now().astimezone()
        logger.info("自動エクスポートを開始しました（間隔: %d 分）", self._get_config().interval_minutes)
        self._changed()

    def stop(self) -> None:
        with self._lock:
            if not self._enabled:
                return
            self._enabled = False
            self._next_run = None
        logger.info("自動エクスポートを停止しました")
        self._changed()

    def run_now(self, force: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._manual_force = force or bool(self._manual_force)
        self._wake.set()

    def reschedule(self) -> None:
        """実行間隔を変えたときに次回時刻を計算し直す。"""
        with self._lock:
            if not self._enabled or self._busy:
                return  # 書き出し中なら、終わった時点で新しい間隔が使われる
            base = self._last_finished or datetime.now().astimezone()
            self._next_run = base + timedelta(minutes=self._get_config().interval_minutes)
        self._changed()

    def shutdown(self, wait: float | None = 0) -> None:
        """スレッドを終わらせる。書き出し中ならその完了を待ってから終わる。"""
        with self._lock:
            self._shutdown = True
            self._enabled = False
            self._next_run = None
        self._wake.set()
        if wait != 0:
            self._thread.join(wait)

    # ---- スレッド側 ----------------------------------------------------
    def _changed(self) -> None:
        self._on_event(SCHEDULE, self.next_run)
        self._wake.set()

    def _loop(self) -> None:
        while True:
            with self._lock:
                if self._shutdown:
                    return
                now = datetime.now().astimezone()
                force = self._manual_force
                due = self._enabled and self._next_run is not None and now >= self._next_run
                if force is not None or due:
                    self._manual_force = None
                    self._busy = True
                    wait = None
                else:
                    wait = float(_MAX_WAIT_SECONDS)
                    if self._enabled and self._next_run is not None:
                        wait = min(wait, max(0.0, (self._next_run - now).total_seconds()))
            if wait is None:
                self._execute(force=bool(force))
            else:
                # 要求はフラグとして持っているので、clear で起床を取りこぼしても次の周回で拾える
                self._wake.wait(wait)
                self._wake.clear()

    def _execute(self, force: bool) -> None:
        self._on_event(BUSY, None)
        try:
            result = run_once(self._get_config(), force=force)
        except (ConfigError, chrome.HistoryReadError, AlreadyRunning, OSError) as exc:
            logger.error("エクスポートに失敗しました: %s", exc)
            self._on_event(FAILED, str(exc))
        except Exception as exc:  # 1 回の失敗でスレッドを止めない
            logger.exception("エクスポート中に予期しないエラーが起きました")
            self._on_event(FAILED, f"予期しないエラー: {exc}")
        else:
            if result.skipped_reason:
                logger.info("出力なし: %s", result.skipped_reason)
            self._on_event(DONE, result)
        finally:
            with self._lock:
                self._busy = False
                self._last_finished = datetime.now().astimezone()
                if self._enabled:
                    interval = timedelta(minutes=self._get_config().interval_minutes)
                    self._next_run = self._last_finished + interval
            self._on_event(SCHEDULE, self.next_run)
