"""Chrome の History データベース（SQLite）から閲覧履歴を読み出す。"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .timeutil import from_webkit, to_webkit

logger = logging.getLogger(__name__)

# プロファイルとして扱わないディレクトリ
_EXCLUDED_PROFILE_DIRS = {"System Profile", "Guest Profile"}

# History 本体と同時にコピーが必要なサイドカーファイル
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

# visits.transition の下位 8bit（コアタイプ）
_TRANSITION_CORE = {
    0: "link",
    1: "typed",
    2: "auto_bookmark",
    3: "auto_subframe",
    4: "manual_subframe",
    5: "generated",
    6: "start_page",
    7: "form_submit",
    8: "reload",
    9: "keyword",
    10: "keyword_generated",
}

_QUERY = """
SELECT v.id, v.visit_time, v.visit_duration, v.transition,
       u.url, u.title, u.visit_count
FROM visits AS v
JOIN urls AS u ON u.id = v.url
WHERE v.visit_time >= ? AND v.visit_time < ?
ORDER BY v.visit_time, v.id
"""


class HistoryReadError(Exception):
    pass


@dataclass(frozen=True)
class VisitRecord:
    profile: str
    visit_id: int
    visit_time: datetime
    url: str
    title: str
    transition: str
    visit_duration_sec: float
    visit_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "visit_time": self.visit_time.isoformat(),
            "profile": self.profile,
            "title": self.title,
            "url": self.url,
            "transition": self.transition,
            "visit_duration_sec": self.visit_duration_sec,
            "visit_count": self.visit_count,
            "visit_id": self.visit_id,
        }


FIELD_NAMES = [
    "visit_time",
    "profile",
    "title",
    "url",
    "transition",
    "visit_duration_sec",
    "visit_count",
    "visit_id",
]


def transition_name(transition: int) -> str:
    return _TRANSITION_CORE.get(int(transition) & 0xFF, "other")


def list_profiles(user_data_dir: Path, wanted: list[str] | None = None) -> list[tuple[str, Path]]:
    """(プロファイル名, History ファイルのパス) を返す。

    wanted が None か ["*"] を含む場合は History を持つ全プロファイルが対象。
    """
    all_profiles: list[tuple[str, Path]] = []
    for child in sorted(user_data_dir.iterdir()):
        if not child.is_dir() or child.name in _EXCLUDED_PROFILE_DIRS:
            continue
        history = child / "History"
        if history.is_file():
            all_profiles.append((child.name, history))

    if wanted is None or "*" in wanted:
        return all_profiles

    selected = []
    available = {name for name, _ in all_profiles}
    for name in wanted:
        if name not in available:
            logger.warning("プロファイル %r が見つかりません（利用可能: %s）", name, ", ".join(sorted(available)) or "なし")
            continue
        selected.extend((n, p) for n, p in all_profiles if n == name)
    return selected


def profile_display_names(user_data_dir: Path) -> dict[str, str]:
    """Local State から {プロファイルのフォルダ名: Chrome 上の表示名} を読む。読めなければ空。"""
    try:
        data = json.loads((user_data_dir / "Local State").read_text(encoding="utf-8-sig"))
        cache = data["profile"]["info_cache"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.debug("プロファイルの表示名を読めません: %s", exc)
        return {}
    if not isinstance(cache, dict):
        return {}
    return {
        folder: info["name"]
        for folder, info in cache.items()
        if isinstance(info, dict) and isinstance(info.get("name"), str)
    }


def _copy_history(history_path: Path, dest_dir: Path) -> Path:
    """ロックされていても読めるよう History を一時ディレクトリへコピーする。"""
    dest = dest_dir / "History"
    shutil.copy2(history_path, dest)
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = history_path.with_name(history_path.name + suffix)
        if sidecar.is_file():
            try:
                shutil.copy2(sidecar, dest_dir / sidecar.name)
            except OSError as exc:  # -shm はコピーできないことがあるが致命的ではない
                logger.debug("サイドカー %s のコピーに失敗: %s", sidecar.name, exc)
    return dest


def read_visits(
    profile: str,
    history_path: Path,
    start: datetime,
    end: datetime,
) -> list[VisitRecord]:
    """[start, end) の訪問履歴を読み出す。"""
    start_us, end_us = to_webkit(start), to_webkit(end)
    tz = start.tzinfo

    with tempfile.TemporaryDirectory(prefix="chrome-history-") as tmp:
        try:
            copied = _copy_history(history_path, Path(tmp))
        except OSError as exc:
            raise HistoryReadError(f"{profile}: History のコピーに失敗しました: {exc}") from exc

        try:
            conn = sqlite3.connect(copied)
            try:
                rows = conn.execute(_QUERY, (start_us, end_us)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise HistoryReadError(f"{profile}: History の読み取りに失敗しました: {exc}") from exc

    return [
        VisitRecord(
            profile=profile,
            visit_id=row[0],
            visit_time=from_webkit(row[1], tz),
            url=row[4] or "",
            title=row[5] or "",
            transition=transition_name(row[3]),
            visit_duration_sec=round((row[2] or 0) / 1_000_000, 3),
            visit_count=row[6] or 0,
        )
        for row in rows
    ]


def collect_visits(
    user_data_dir: Path,
    profiles: list[str] | None,
    start: datetime,
    end: datetime,
) -> list[VisitRecord]:
    """対象プロファイルすべてから履歴を集めて時刻順に並べる。"""
    targets = list_profiles(user_data_dir, profiles)
    if not targets:
        raise HistoryReadError(f"対象プロファイルが見つかりません: {user_data_dir}")

    records: list[VisitRecord] = []
    for name, history_path in targets:
        records.extend(read_visits(name, history_path, start, end))
    records.sort(key=lambda r: (r.visit_time, r.profile, r.visit_id))
    return records
