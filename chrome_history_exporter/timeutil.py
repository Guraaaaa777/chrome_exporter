"""Chrome (WebKit) タイムスタンプと Python datetime の相互変換。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 1601-01-01(UTC) から 1970-01-01(UTC) までの秒数
WEBKIT_EPOCH_DELTA_SECONDS = 11_644_473_600
WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

FILENAME_TIME_FORMAT = "%Y%m%d%H%M"


def to_webkit(dt: datetime) -> int:
    """aware datetime -> Chrome の 1601年起点マイクロ秒。"""
    if dt.tzinfo is None:
        raise ValueError("naive datetime は変換できません")
    return int(round((dt.timestamp() + WEBKIT_EPOCH_DELTA_SECONDS) * 1_000_000))


def from_webkit(value: int, tz: timezone | None = None) -> datetime:
    """Chrome のマイクロ秒 -> aware datetime（既定はローカルタイム）。"""
    dt = WEBKIT_EPOCH + timedelta(microseconds=int(value))
    return dt.astimezone(tz) if tz is not None else dt.astimezone()


def now_local() -> datetime:
    """秒以下を切り捨てたローカルの現在時刻（aware）。"""
    return datetime.now().astimezone().replace(microsecond=0)


def floor_to_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def format_stamp(dt: datetime) -> str:
    """ファイル名用の YYYYMMDDhhmm。"""
    return dt.strftime(FILENAME_TIME_FORMAT)


def parse_stamp(text: str) -> datetime:
    """YYYYMMDDhhmm をローカルタイムの aware datetime として解釈する。"""
    return datetime.strptime(text, FILENAME_TIME_FORMAT).astimezone()
