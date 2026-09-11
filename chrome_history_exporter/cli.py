"""コマンドラインインターフェース。"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

from . import chrome, paths, windows_task
from .config import Config, ConfigError, load_config
from .lockfile import AlreadyRunning, ProcessLock
from .logging_setup import setup_logging
from .service import run_forever, run_once, stop_flag_path
from .state import State
from .timeutil import floor_to_minute, now_local

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chrome-history-exporter",
        description="Chrome の閲覧履歴を定期的にファイルへ書き出します。",
    )
    parser.add_argument("--config", type=Path, help="設定ファイル(JSON)のパス")
    parser.add_argument("--log-level", help="ログレベル（DEBUG/INFO/WARNING/ERROR）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_once = sub.add_parser("once", help="1 回だけエクスポートする")
    p_once.add_argument("--force", action="store_true", help="履歴 0 件でもファイルを作る")
    p_once.add_argument("--output-dir", type=Path, help="出力先を一時的に上書きする")

    sub.add_parser("run", help="常駐して定期的にエクスポートする")
    sub.add_parser("stop", help="常駐プロセスに停止を要求する")
    sub.add_parser("status", help="設定と前回実行の状況を表示する")
    sub.add_parser("profiles", help="検出された Chrome プロファイルを一覧表示する")

    p_init = sub.add_parser("init-config", help="設定ファイルのひな形を作る")
    p_init.add_argument("--force", action="store_true", help="既存ファイルを上書きする")

    p_install = sub.add_parser("install", help="Windows タスクスケジューラに登録する")
    p_install.add_argument(
        "--mode",
        choices=("resident", "interval"),
        default="resident",
        help="resident: ログオン時に常駐起動 / interval: スケジューラが定期実行",
    )
    p_install.add_argument("--task-name", default=windows_task.TASK_NAME, help="タスク名")

    p_uninstall = sub.add_parser("uninstall", help="タスクスケジューラから削除する")
    p_uninstall.add_argument("--task-name", default=windows_task.TASK_NAME, help="タスク名")

    return parser


def _load(args: argparse.Namespace) -> Config:
    if args.command == "init-config" and args.config and not Path(args.config).expanduser().is_file():
        # これから作るファイルなので、存在しなくてもエラーにしない
        config = Config()
        config.config_path = Path(args.config).expanduser()
    else:
        config = load_config(args.config)
    if args.log_level:
        config.log_level = args.log_level
    return config


# ---- 各コマンド --------------------------------------------------------


def cmd_once(config: Config, args: argparse.Namespace) -> int:
    if args.output_dir:
        config.output_dir = str(args.output_dir)
    result = run_once(config, force=args.force)
    if result.file_path:
        print(f"出力しました: {result.file_path} ({result.record_count} 件)")
    else:
        print(f"出力なし: {result.skipped_reason}")
    return 0


def cmd_run(config: Config) -> int:
    lock = ProcessLock(config.state_path.with_name("run.lock"))
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        logger.error("%s", exc)
        print(str(exc), file=sys.stderr)
        return 1
    try:
        run_forever(config)
    finally:
        lock.release()
    return 0


def cmd_stop(config: Config) -> int:
    flag = stop_flag_path(config)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("stop\n", encoding="utf-8")
    print(f"停止を要求しました（最大 5 秒ほどで終了します）: {flag}")
    return 0


def cmd_status(config: Config) -> int:
    state = State(config.state_path)
    try:
        user_data_dir: object = config.resolve_user_data_dir()
    except ConfigError as exc:
        user_data_dir = f"(未検出) {exc}"

    last_end = state.last_export_end
    next_start = floor_to_minute(last_end.astimezone()) if last_end else None
    lines = [
        f"設定ファイル      : {config.config_path} {'(未作成・既定値)' if config.config_path and not config.config_path.is_file() else ''}",
        f"ユーザーデータ    : {user_data_dir}",
        f"対象プロファイル  : {', '.join(config.profiles)}",
        f"出力先            : {config.output_path}",
        f"出力形式          : {config.output_format}",
        f"実行間隔          : {config.interval_minutes} 分",
        f"状態ファイル      : {config.state_path}",
        f"ログ              : {config.log_path}",
        f"前回の期間終端    : {last_end.isoformat() if last_end else '(未実行)'}",
        f"前回の出力ファイル: {state.last_file or '(なし)'}",
        f"前回の件数        : {state.as_dict().get('last_record_count', '(なし)')}",
        f"次回の対象開始    : {next_start.isoformat() if next_start else f'現在時刻の {config.initial_lookback_hours} 時間前'}",
    ]
    if paths.is_windows():
        lines.append(f"タスク登録        : {'あり' if windows_task.query() else 'なし'}")
    print("\n".join(lines))
    return 0


def cmd_profiles(config: Config) -> int:
    user_data_dir = config.resolve_user_data_dir()
    found = chrome.list_profiles(user_data_dir, None)
    print(f"ユーザーデータ: {user_data_dir}")
    if not found:
        print("プロファイルが見つかりませんでした。")
        return 1
    selected = {name for name, _ in chrome.list_profiles(user_data_dir, config.profiles)}
    for name, history in found:
        mark = "*" if name in selected else " "
        print(f" {mark} {name}  ({history})")
    print("\n* = 現在の設定で対象になっているプロファイル")
    return 0


def cmd_init_config(config: Config, args: argparse.Namespace) -> int:
    target = config.config_path or paths.default_config_path()
    if target.is_file() and not args.force:
        print(f"既に存在します（上書きするには --force）: {target}")
        return 1
    template = Config()
    try:
        template.user_data_dir = str(template.resolve_user_data_dir())
    except ConfigError:
        pass  # 自動検出できない場合は null のままにする
    template.output_dir = str(paths.default_output_dir())
    template.save(target)
    print(f"設定ファイルを作成しました: {target}")
    return 0


def cmd_install(config: Config, args: argparse.Namespace) -> int:
    command = windows_task.install(
        mode=args.mode,
        interval_minutes=config.interval_minutes,
        config_path=config.config_path if config.config_path and config.config_path.is_file() else None,
        task_name=args.task_name,
    )
    print(f"タスク '{args.task_name}' を登録しました（mode={args.mode}）")
    print(f"実行コマンド: {command}")
    if args.mode == "resident":
        print("次回ログオン時に自動起動します。今すぐ開始するには: schtasks /run /tn " + args.task_name)
    else:
        print(f"{config.interval_minutes} 分ごとに実行されます。")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    windows_task.uninstall(args.task_name)
    print(f"タスク '{args.task_name}' を削除しました")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 2

    # 常駐時はコンソールが無い場合があるためファイルログを主にする
    setup_logging(config, console=args.command != "run" or sys.stderr is not None)

    try:
        if args.command == "once":
            return cmd_once(config, args)
        if args.command == "run":
            return cmd_run(config)
        if args.command == "stop":
            return cmd_stop(config)
        if args.command == "status":
            return cmd_status(config)
        if args.command == "profiles":
            return cmd_profiles(config)
        if args.command == "init-config":
            return cmd_init_config(config, args)
        if args.command == "install":
            return cmd_install(config, args)
        if args.command == "uninstall":
            return cmd_uninstall(args)
    except KeyboardInterrupt:
        print("中断しました", file=sys.stderr)
        return 130
    except (ConfigError, chrome.HistoryReadError, windows_task.TaskError) as exc:
        logger.error("%s", exc)
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("予期しないエラー")
        print(f"予期しないエラー: {exc}", file=sys.stderr)
        return 1

    return 0
