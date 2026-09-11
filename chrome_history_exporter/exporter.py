"""履歴レコードをファイルへ書き出す。

ファイル名は期間を表す YYYYMMDDhhmm-YYYYMMDDhhmm 形式。
例: 202609111200-202609111300.csv
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .chrome import FIELD_NAMES, VisitRecord
from .timeutil import format_stamp

logger = logging.getLogger(__name__)

_EXTENSIONS = {"csv": ".csv", "jsonl": ".jsonl", "json": ".json"}


def build_filename(start: datetime, end: datetime, output_format: str = "csv") -> str:
    """期間からファイル名を組み立てる（YYYYMMDDhhmm-YYYYMMDDhhmm.拡張子）。"""
    try:
        ext = _EXTENSIONS[output_format]
    except KeyError:
        raise ValueError(f"未知の output_format: {output_format!r}") from None
    return f"{format_stamp(start)}-{format_stamp(end)}{ext}"


def _unique_path(path: Path) -> Path:
    """同名ファイルがある場合は -1, -2 ... を付けて衝突を避ける。"""
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"ファイル名の候補が尽きました: {path}")


def _write_csv(path: Path, records: Sequence[VisitRecord], encoding: str) -> None:
    with path.open("w", encoding=encoding, newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=FIELD_NAMES)
        writer.writeheader()
        for record in records:
            writer.writerow(record.as_dict())


def _write_jsonl(path: Path, records: Sequence[VisitRecord], encoding: str) -> None:
    with path.open("w", encoding=encoding, newline="\n") as fp:
        for record in records:
            fp.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")


def _write_json(
    path: Path, records: Sequence[VisitRecord], encoding: str, start: datetime, end: datetime
) -> None:
    payload = {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "count": len(records),
        "visits": [record.as_dict() for record in records],
    }
    with path.open("w", encoding=encoding, newline="\n") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")


def export(
    records: Sequence[VisitRecord],
    start: datetime,
    end: datetime,
    output_dir: Path,
    output_format: str = "csv",
    encoding: str = "utf-8-sig",
) -> Path:
    """レコードを 1 ファイルに書き出し、書き出したパスを返す。

    書き込み中の中途半端なファイルを他プロセスに見せないよう、一時ファイルへ
    書いてから最終名へ移動する。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_path(output_dir / build_filename(start, end, output_format))
    tmp = target.with_name(target.name + ".tmp")

    try:
        if output_format == "csv":
            _write_csv(tmp, records, encoding)
        elif output_format == "jsonl":
            _write_jsonl(tmp, records, encoding)
        else:
            _write_json(tmp, records, encoding, start, end)
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    logger.info("%d 件を書き出しました: %s", len(records), target)
    return target
