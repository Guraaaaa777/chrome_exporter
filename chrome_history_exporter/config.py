"""設定ファイル（JSON）の読み込みと既定値。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from . import paths

VALID_FORMATS = ("csv", "jsonl", "json")


class ConfigError(Exception):
    pass


@dataclass
class Config:
    # 対象ブラウザ（chrome / edge / brave）。user_data_dir 未指定時の自動検出に使う
    browser: str = "chrome"
    # 例: "C:/Users/you/AppData/Local/Google/Chrome/User Data"。null なら自動検出
    user_data_dir: str | None = None
    # 対象プロファイル名。["*"] で全プロファイル
    profiles: list[str] = field(default_factory=lambda: ["*"])

    output_dir: str | None = None
    output_format: str = "csv"
    encoding: str = "utf-8-sig"  # Excel で文字化けしないよう BOM 付き

    interval_minutes: int = 60
    initial_lookback_hours: int = 24
    skip_empty: bool = True  # 履歴 0 件の期間はファイルを作らない

    state_file: str | None = None
    log_file: str | None = None
    log_level: str = "INFO"
    log_max_bytes: int = 1_000_000
    log_backup_count: int = 3

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
                raise ConfigError(f"user_data_dir が見つかりません: {path}")
            return path
        found = paths.find_user_data_dir(self.browser)
        if found is None:
            raise ConfigError(
                f"{self.browser} のユーザーデータディレクトリを自動検出できませんでした。"
                " config.json の user_data_dir を設定してください。"
            )
        return found

    # ---- 入出力 --------------------------------------------------------
    def validate(self) -> None:
        if self.output_format not in VALID_FORMATS:
            raise ConfigError(f"output_format は {VALID_FORMATS} のいずれかにしてください: {self.output_format!r}")
        if self.interval_minutes < 1:
            raise ConfigError("interval_minutes は 1 以上にしてください")
        if self.initial_lookback_hours < 0:
            raise ConfigError("initial_lookback_hours は 0 以上にしてください")
        if not self.profiles:
            raise ConfigError("profiles を 1 つ以上指定してください")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("config_path", None)
        return data

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_config(path: Path | None = None) -> Config:
    """設定ファイルを読み込む。存在しなければ既定値を返す。"""
    config_path = Path(path).expanduser() if path else paths.default_config_path()
    if not config_path.is_file():
        if path is not None:
            raise ConfigError(f"設定ファイルが見つかりません: {config_path}")
        cfg = Config()
        cfg.config_path = config_path
        cfg.validate()
        return cfg

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"設定ファイルの JSON が不正です ({config_path}): {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"設定ファイルはオブジェクトである必要があります: {config_path}")

    known = {f for f in Config.__dataclass_fields__ if f != "config_path"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"未知の設定キー: {', '.join(sorted(unknown))}")

    cfg = Config(**raw)
    cfg.config_path = config_path
    cfg.validate()
    return cfg
