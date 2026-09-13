"""unittest で実行するテスト: python -m unittest discover -s tests"""

from __future__ import annotations

import csv
import json
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chrome_history_exporter import autostart, chrome, exporter, service  # noqa: E402
from chrome_history_exporter.config import Config, ConfigError, load_config  # noqa: E402
from chrome_history_exporter.lockfile import AlreadyRunning, ProcessLock  # noqa: E402
from chrome_history_exporter.state import State  # noqa: E402
from chrome_history_exporter.timeutil import from_webkit, to_webkit  # noqa: E402


def make_history_db(path: Path, visits: list[tuple[str, str, datetime, int]]) -> None:
    """テスト用の最小構成 History データベースを作る。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT,
                           visit_count INTEGER DEFAULT 0, typed_count INTEGER DEFAULT 0,
                           last_visit_time INTEGER, hidden INTEGER DEFAULT 0);
        CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER,
                             from_visit INTEGER, transition INTEGER DEFAULT 0,
                             segment_id INTEGER, visit_duration INTEGER DEFAULT 0);
        """
    )
    for index, (url, title, when, transition) in enumerate(visits, start=1):
        webkit = to_webkit(when)
        conn.execute(
            "INSERT INTO urls VALUES (?,?,?,?,?,?,?)", (index, url, title, 1, 0, webkit, 0)
        )
        conn.execute(
            "INSERT INTO visits VALUES (?,?,?,?,?,?,?)",
            (index, index, webkit, 0, transition, None, 5_000_000),
        )
    conn.commit()
    conn.close()


def temp_config(root: Path, **overrides) -> Config:
    params = {
        "user_data_dir": str(root / "User Data"),
        "output_dir": str(root / "out"),
        "state_file": str(root / "state.json"),
        "log_file": str(root / "log.txt"),
        "initial_lookback_hours": 1,
        "encoding": "utf-8",
    }
    params.update(overrides)
    return Config(**params)


class TimeUtilTest(unittest.TestCase):
    def test_webkit_roundtrip(self):
        original = datetime(2026, 9, 11, 12, 34, 56).astimezone()
        self.assertEqual(from_webkit(to_webkit(original)), original)

    def test_known_webkit_value(self):
        # 13,000,000,000,000,000 マイクロ秒 = 2012-12-14 23:06:40 UTC
        self.assertEqual(
            from_webkit(13_000_000_000_000_000, timezone.utc),
            datetime(2012, 12, 14, 23, 6, 40, tzinfo=timezone.utc),
        )
        self.assertEqual(
            to_webkit(datetime(2012, 12, 14, 23, 6, 40, tzinfo=timezone.utc)),
            13_000_000_000_000_000,
        )

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            to_webkit(datetime(2026, 9, 11, 12, 0))


class FilenameTest(unittest.TestCase):
    def test_format(self):
        start = datetime(2026, 9, 11, 12, 0).astimezone()
        end = datetime(2026, 9, 11, 13, 30).astimezone()
        self.assertEqual(
            exporter.build_filename(start, end, "csv"), "202609111200-202609111330.csv"
        )
        self.assertEqual(
            exporter.build_filename(start, end, "jsonl"), "202609111200-202609111330.jsonl"
        )

    def test_midnight_and_year_boundary(self):
        start = datetime(2025, 12, 31, 23, 0).astimezone()
        end = datetime(2026, 1, 1, 0, 0).astimezone()
        self.assertEqual(
            exporter.build_filename(start, end, "csv"), "202512312300-202601010000.csv"
        )

    def test_unknown_format(self):
        now = datetime.now().astimezone()
        with self.assertRaises(ValueError):
            exporter.build_filename(now, now, "xml")


class ExportFlowTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.user_data = self.root / "User Data"
        self.output = self.root / "out"
        self.now = datetime(2026, 9, 11, 13, 0).astimezone()
        make_history_db(
            self.user_data / "Default" / "History",
            [
                ("https://example.com/a", "A ページ", self.now - timedelta(minutes=30), 0),
                ("https://example.com/b", "B ページ", self.now - timedelta(minutes=10), 1),
                ("https://old.example.com", "古い", self.now - timedelta(days=5), 0),
            ],
        )
        make_history_db(
            self.user_data / "Profile 1" / "History",
            [("https://work.example.com", "仕事", self.now - timedelta(minutes=20), 0)],
        )

    def tearDown(self):
        self._tmp.cleanup()

    def config(self, **overrides) -> Config:
        return temp_config(self.root, **overrides)

    def test_list_profiles(self):
        names = [name for name, _ in chrome.list_profiles(self.user_data, None)]
        self.assertEqual(names, ["Default", "Profile 1"])
        names = [name for name, _ in chrome.list_profiles(self.user_data, ["Profile 1"])]
        self.assertEqual(names, ["Profile 1"])

    def test_profile_display_names(self):
        self.assertEqual(chrome.profile_display_names(self.user_data), {})  # Local State なし
        local_state = {"profile": {"info_cache": {"Default": {"name": "個人"}, "Profile 1": {"name": "仕事"}}}}
        (self.user_data / "Local State").write_text(json.dumps(local_state), encoding="utf-8")
        self.assertEqual(
            chrome.profile_display_names(self.user_data), {"Default": "個人", "Profile 1": "仕事"}
        )

    def test_run_once_writes_expected_file(self):
        result = service.run_once(self.config(), now=self.now)
        self.assertIsNotNone(result.file_path)
        self.assertEqual(result.file_path.name, "202609111200-202609111300.csv")
        self.assertEqual(result.record_count, 3)  # 5 日前の履歴は対象外

        with result.file_path.open(encoding="utf-8", newline="") as fp:
            rows = list(csv.DictReader(fp))
        self.assertEqual([row["title"] for row in rows], ["A ページ", "仕事", "B ページ"])
        self.assertEqual(rows[0]["profile"], "Default")
        self.assertEqual(rows[1]["profile"], "Profile 1")
        self.assertEqual(rows[1]["transition"], "link")
        self.assertEqual(rows[2]["transition"], "typed")
        self.assertEqual(float(rows[0]["visit_duration_sec"]), 5.0)

    def test_second_run_continues_from_previous_end(self):
        cfg = self.config()
        first = service.run_once(cfg, now=self.now)
        self.assertIsNotNone(first.file_path)

        later = self.now + timedelta(hours=1)
        second = service.run_once(cfg, now=later)
        # 期間は前回の終端から継続し、履歴が無いのでファイルは作られない
        self.assertIsNone(second.file_path)
        self.assertEqual(second.start.strftime("%Y%m%d%H%M"), "202609111300")
        self.assertEqual(State(cfg.state_path).last_export_end.strftime("%H%M"), "1400")

        # 新しい履歴を追加すると次回に取り込まれる
        make_history_db(
            self.user_data / "Default" / "History",
            [("https://example.com/c", "C ページ", later + timedelta(minutes=10), 0)],
        )
        third = service.run_once(cfg, now=later + timedelta(hours=1))
        self.assertEqual(third.file_path.name, "202609111400-202609111500.csv")
        self.assertEqual(third.record_count, 1)

    def test_force_writes_empty_file(self):
        cfg = self.config(profiles=["Profile 1"], initial_lookback_hours=0)
        result = service.run_once(cfg, now=self.now, force=True)
        self.assertIsNone(result.file_path)  # 期間 0 分なので出力なし

        cfg2 = self.config(profiles=["Default"], initial_lookback_hours=1,
                           state_file=str(self.root / "state2.json"))
        cfg2.skip_empty = False
        result2 = service.run_once(cfg2, now=self.now + timedelta(hours=5))
        self.assertIsNotNone(result2.file_path)
        self.assertEqual(result2.record_count, 0)

    def test_jsonl_and_json_output(self):
        cfg = self.config(output_format="jsonl")
        result = service.run_once(cfg, now=self.now)
        lines = result.file_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0])["url"], "https://example.com/a")

        cfg_json = self.config(
            output_format="json", state_file=str(self.root / "state-json.json")
        )
        result_json = service.run_once(cfg_json, now=self.now)
        payload = json.loads(result_json.file_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["count"], 3)
        self.assertEqual(len(payload["visits"]), 3)

    def test_filename_collision_gets_suffix(self):
        cfg = self.config()
        first = service.run_once(cfg, now=self.now)
        # 状態をリセットして同じ期間をもう一度書き出す
        Path(cfg.state_path).unlink()
        second = service.run_once(cfg, now=self.now)
        self.assertNotEqual(first.file_path, second.file_path)
        self.assertEqual(second.file_path.name, "202609111200-202609111300-1.csv")

    def test_locked_database_is_still_readable(self):
        # Chrome が開いているのを模して排他ロックを掛けたまま読み出す
        history = self.user_data / "Default" / "History"
        conn = sqlite3.connect(history)
        conn.execute("BEGIN EXCLUSIVE")
        try:
            records = chrome.collect_visits(
                self.user_data, ["Default"], self.now - timedelta(hours=1), self.now
            )
        finally:
            conn.rollback()
            conn.close()
        self.assertEqual(len(records), 2)

    def test_run_once_waits_for_other_export(self):
        cfg = self.config()
        other = ProcessLock(service.export_lock_path(cfg))
        other.acquire()
        # 別プロセスがエクスポート中は、タイムアウトまで待って諦める（状態は進めない）
        with mock.patch.object(service, "_EXPORT_LOCK_TIMEOUT_SECONDS", 0.2):
            try:
                with self.assertRaises(AlreadyRunning):
                    service.run_once(cfg, now=self.now)
            finally:
                other.release()
        self.assertIsNone(State(cfg.state_path).last_export_end)

        # 待っている間に解放されれば、その後に実行される
        other.acquire()
        threading.Timer(0.3, other.release).start()
        result = service.run_once(cfg, now=self.now)
        self.assertEqual(result.record_count, 3)

    def test_missing_user_data_dir(self):
        cfg = self.config(user_data_dir=str(self.root / "nope"))
        with self.assertRaises(ConfigError):
            service.run_once(cfg, now=self.now)


class SchedulerTest(unittest.TestCase):
    """実際の現在時刻で動かし、画面へ届く通知と次回時刻を確かめる。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.scheduler: service.Scheduler | None = None

    def tearDown(self):
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=5)
        self._tmp.cleanup()

    def start_scheduler(self, cfg: Config) -> service.Scheduler:
        self.scheduler = service.Scheduler(lambda: cfg, lambda kind, payload: self.events.put((kind, payload)))
        return self.scheduler

    def wait_for(self, kind: str, timeout: float = 10) -> object:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                got, payload = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if got == kind:
                return payload
        self.fail(f"{kind} が届きませんでした")

    def wait_until(self, condition, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("条件を満たしませんでした")
            time.sleep(0.02)

    def test_run_now_then_auto(self):
        now = datetime.now().astimezone()
        make_history_db(
            self.root / "User Data" / "Default" / "History",
            [("https://example.com/now", "いま", now - timedelta(minutes=5), 0)],
        )
        scheduler = self.start_scheduler(temp_config(self.root, interval_minutes=30))

        scheduler.run_now()
        result = self.wait_for(service.DONE)
        self.assertEqual(result.record_count, 1)
        self.assertIsNotNone(result.file_path)
        self.assertFalse(scheduler.enabled)
        self.assertIsNone(scheduler.next_run)

        scheduler.start()  # 開始するとすぐ前回の続きを書き出す
        self.wait_for(service.BUSY)
        self.wait_for(service.DONE)
        self.wait_until(lambda: not scheduler.busy)
        remaining = (scheduler.next_run - datetime.now().astimezone()).total_seconds()
        self.assertAlmostEqual(remaining, 30 * 60, delta=10)

        scheduler.stop()
        self.assertFalse(scheduler.enabled)
        self.assertIsNone(scheduler.next_run)

    def test_failure_is_reported_and_thread_survives(self):
        scheduler = self.start_scheduler(temp_config(self.root, user_data_dir=str(self.root / "nope")))
        scheduler.run_now()
        message = self.wait_for(service.FAILED)
        self.assertIn("見つかりません", message)
        self.assertTrue(scheduler.alive)

    def test_shutdown_stops_thread(self):
        scheduler = self.start_scheduler(temp_config(self.root))
        scheduler.start()
        scheduler.shutdown(wait=5)
        self.assertFalse(scheduler.alive)
        self.assertFalse(scheduler.enabled)


class ConfigTest(unittest.TestCase):
    def test_load_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"interval_minutes": 15, "output_format": "jsonl"}), encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(cfg.interval_minutes, 15)
            self.assertEqual(cfg.output_format, "jsonl")
            self.assertEqual(cfg.config_path, path)

            for broken in ({"output_format": "xml"}, {"interval_minutes": 0},
                           {"interval_minutes": "60"}, {"profiles": []}, {"encoding": "nope"}):
                path.write_text(json.dumps(broken), encoding="utf-8")
                with self.assertRaises(ConfigError, msg=broken):
                    load_config(path)

            path.write_text("{ broken", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_legacy_cli_config_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            legacy = {
                "browser": "chrome", "user_data_dir": None, "profiles": ["*"], "output_dir": None,
                "output_format": "csv", "encoding": "utf-8-sig", "interval_minutes": 60,
                "initial_lookback_hours": 24, "skip_empty": True, "state_file": None,
                "log_file": None, "log_level": "INFO", "log_max_bytes": 1000000, "log_backup_count": 3,
                "someday_key": 1,
            }
            path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertLogs("chrome_history_exporter.config", "WARNING"):
                cfg = load_config(path)
            self.assertEqual(cfg.interval_minutes, 60)
            self.assertTrue(cfg.auto_export_on_launch)
            self.assertTrue(cfg.close_to_tray)

    def test_save_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "config.json"
            cfg = Config(profiles=["Default"], interval_minutes=30, close_to_tray=False, config_path=path)
            cfg.save()
            loaded = load_config(path)
            self.assertEqual(loaded, cfg)
            self.assertFalse((path.with_name("config.json.tmp")).exists())

    def test_missing_config_gives_defaults(self):
        path = Path(tempfile.gettempdir()) / "nonexistent-dir-for-test" / "config.json"
        cfg = load_config(path)
        self.assertEqual(cfg, Config())
        self.assertEqual(cfg.config_path, path)


class AutostartTest(unittest.TestCase):
    """レジストリには触らず、登録するコマンドだけ確かめる。"""

    def test_command_for_script(self):
        command = autostart.launch_command()
        self.assertIn("run.py", command)
        self.assertTrue(command.endswith(" --minimized"))

    def test_command_for_exe(self):
        exe = r"C:\Apps\Chrome History\ChromeHistoryExporter.exe"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", exe):
            self.assertEqual(autostart.launch_command(), f'"{exe}" --minimized')


class ProcessLockTest(unittest.TestCase):
    def test_second_acquire_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            first = ProcessLock(path)
            first.acquire()
            try:
                with self.assertRaises(AlreadyRunning):
                    ProcessLock(path).acquire()
            finally:
                first.release()
            # 解放後は取り直せる
            with ProcessLock(path):
                pass

    def test_acquire_with_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.lock"
            first = ProcessLock(path)
            first.acquire()
            try:
                started = time.monotonic()
                with self.assertRaises(AlreadyRunning):
                    ProcessLock(path).acquire(timeout=0.3)
                self.assertGreaterEqual(time.monotonic() - started, 0.3)
            finally:
                first.release()


class GuiSmokeTest(unittest.TestCase):
    """画面を組み立てて、フォームの読み書きが設定と一致することを確かめる。"""

    def test_build_and_read_form(self):
        try:
            import tkinter

            tkinter.Tk().destroy()
            from chrome_history_exporter.gui import App
        except Exception as exc:  # 画面の無い環境では飛ばす
            self.skipTest(f"tkinter を使えません: {exc}")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_history_db(root / "User Data" / "Default" / "History", [])
            local_state = {"profile": {"info_cache": {"Default": {"name": "テスト"}}}}
            (root / "User Data" / "Local State").write_text(json.dumps(local_state), encoding="utf-8")
            (root / "out").mkdir()
            (root / "out" / "202609111200-202609111300.csv").write_text("x", encoding="utf-8")
            (root / "out" / "memo.txt").write_text("x", encoding="utf-8")

            cfg = temp_config(root, profiles=["Default"], interval_minutes=30, auto_export_on_launch=False)
            cfg.config_path = root / "config.json"
            app = App(cfg, tray=False)
            try:
                app.root.update()
                self.assertEqual(app._read_form(), cfg)
                self.assertIn("Default", app.profile_vars)
                self.assertEqual(len(app.files_tree.get_children()), 1)  # 出力ファイル以外は出さない

                app.interval_var.set("abc")
                with self.assertRaises(ConfigError):
                    app._read_form()
            finally:
                app.scheduler.shutdown(wait=5)
                app._finish_quit()


if __name__ == "__main__":
    unittest.main()
