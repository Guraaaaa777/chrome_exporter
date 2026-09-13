"""設定ファイル（JSON）の読み書きと既定値。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths

logger = logging.getLogger(__name__)

VALID_FORMATS = ("csv", "jsonl", "json")

# 旧コマンドライン版の設定にあり、今は使わないキー（読み込み時に黙って無視する）
_LEGACY_KEYS = frozenset({"browser", "log_level", "log_max_bytes", "log_backup_count"})


class ConfigError(Exception):
    pass


@dataclass
class Config:
    # 例: "C:/Users/you/AppData/Local/Google/Chrome/User Data"。null なら自動検出
    user_data_dir: str | None = None
    # 対象プロファイルのフォルダ名（"Default", "Profile 1" …）。["*"] で全プロファイル
    profiles: list[str] = field(default_factory=lambda: ["*"])

    output_dir: str | None = None
    output_format: str = "csv"
    encoding: str = "utf-8-sig"  # Excel で文字化けしないよう BOM 付き

    interval_minutes: int = 60
    initial_lookback_hours: int = 24
    skip_empty: bool = True  # 履歴 0 件の期間はファイルを作らない

    auto_export_on_launch: bool = True  # 起動したら自動エクスポートを始める
    close_to_tray: bool = True  # × で閉じたらトレイに格納する

    state_file: str | None = None
    log_file: str | None = None

    # 実行時に解決される値（設定ファイルには書かない）
    config_path: Path | None = field(default=None, repr=False, compare=False)

    # ---- 解決済みパス --------------------------------------------------
    @property
    def output_path(self) -> Path:
        return Path(self.output_dir).expanduser() if self.output_dir else paths.default_output_dir()

    @property
    def state_path(self) -> Path:
        if self.state_file:
            return Path(self.state_file).expanduser()
        return paths.app_data_dir() / "state.json"

    @property
    def log_path(self) -> Path:
        if self.log_file:
            return Path(self.log_file).expanduser()
        return paths.app_data_dir() / "exporter.log"

    def resolve_user_data_dir(self) -> Path:
        if self.user_data_dir:
            path = Path(self.user_data_dir).expanduser()
            if not path.is_dir():
                raise ConfigError(f"Chrome のデータフォルダが見つかりません: {path}")
            return path
        found = paths.find_user_data_dir()
        if found is None:
            raise ConfigError("Chrome のデータフォルダを自動検出できませんでした。設定タブで指定してください。")
        return found

    # ---- 入出力 --------------------------------------------------------
    def validate(self) -> None:
        if self.output_format not in VALID_FORMATS:
            raise ConfigError(f"出力形式は {VALID_FORMATS} のいずれかにしてください: {self.output_format!r}")
        if not _is_int(self.interval_minutes) or self.interval_minutes < 1:
            raise ConfigError("実行間隔は 1 分以上の整数にしてください")
        if not _is_int(self.initial_lookback_hours) or self.initial_lookback_hours < 0:
            raise ConfigError("初回にさかのぼる時間は 0 以上の整数にしてください")
        if (
            not isinstance(self.profiles, list)
            or not self.profiles
            or not all(isinstance(name, str) for name in self.profiles)
        ):
            raise ConfigError("対象プロファイルを 1 つ以上選んでください")
        try:
            "".encode(self.encoding)
        except (LookupError, TypeError):
            raise ConfigError(f"未知の文字コードです: {self.encoding!r}") from None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("config_path", None)
        return data

    def save(self, path: Path | None = None) -> None:
        target = path or self.config_path or paths.default_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(target)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_config(path: Path | None = None) -> Config:
    """設定ファイルを読み込む。まだ無ければ既定値を返す（画面で保存すると作られる）。"""
    config_path = Path(path).expanduser() if path else paths.default_config_path()
    if not config_path.is_file():
        return Config(config_path=config_path)

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"設定ファイルを読めません ({config_path}): {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"設定ファイルはオブジェクトである必要があります: {config_path}")

    known = {name for name in Config.__dataclass_fields__ if name != "config_path"}
    unknown = set(raw) - known - _LEGACY_KEYS
    if unknown:
        logger.warning("未知の設定キーを無視します: %s", ", ".join(sorted(unknown)))

    cfg = Config(**{key: value for key, value in raw.items() if key in known})
    cfg.config_path = config_path
    cfg.validate()
    return cfg
