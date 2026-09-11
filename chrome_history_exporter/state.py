"""前回エクスポートの終端時刻を保存して、期間の取りこぼし・重複を防ぐ。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class State:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, object] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("状態ファイルを読めませんでした（初期化します）: %s", exc)
            return
        if isinstance(data, dict):
            self._data = data

    @property
    def last_export_end(self) -> datetime | None:
        value = self._data.get("last_export_end")
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.warning("last_export_end の形式が不正です: %r", value)
            return None

    @property
    def last_file(self) -> str | None:
        value = self._data.get("last_file")
        return value if isinstance(value, str) else None

    def record_export(self, end: datetime, file_path: Path | None, record_count: int) -> None:
        self._data.update(
            {
                "last_export_end": end.isoformat(),
                "last_run_at": datetime.now().astimezone().isoformat(),
                "last_file": str(file_path) if file_path else None,
                "last_record_count": record_count,
            }
        )
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def as_dict(self) -> dict[str, object]:
        return dict(self._data)
