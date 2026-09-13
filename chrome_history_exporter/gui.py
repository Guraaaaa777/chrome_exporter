"""メイン画面（tkinter）。"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import queue
import re
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from tkinter.scrolledtext import ScrolledText

from . import APP_TITLE, __version__, autostart, chrome, paths
from .config import VALID_FORMATS, Config, ConfigError
from .logging_setup import FORMAT
from .service import BUSY, DONE, FAILED, SCHEDULE, ExportResult, Scheduler
from .state import State
from .timeutil import parse_stamp
from .tray import TrayIcon, make_icon_image

LOGGER = logging.getLogger(__name__)

FONT_FAMILY = "Yu Gothic UI"
POLL_MS = 200
MAX_LOG_LINES = 1000
LOG_TAIL_LINES = 200  # 起動時にログファイルから読み込む行数
MAX_FILES = 500  # 一覧に出す出力ファイルの数
TRAY = "tray"  # トレイからの操作を events に積むときの kind
LOG = "log"  # ログを events に積むときの kind

OK_COLOR = "#2e7d32"
ERROR_COLOR = "#c62828"
SUB_COLOR = "#666666"

# 状態ごとの (丸の色, 見出し)
STATES = {
    "idle": ("#9e9e9e", "停止中"),
    "active": ("#1a73e8", "自動エクスポート中"),
    "busy": ("#f0a020", "書き出し中…"),
}

FORMAT_LABELS = {
    "csv": "CSV（Excel で開ける）",
    "jsonl": "JSON Lines（1 行に 1 件）",
    "json": "JSON（1 ファイルにまとめる）",
}
INTERVAL_CHOICES = ("15", "30", "60", "120", "180", "360", "720", "1440")

# 202609111200-202609111300.csv / 202609111200-202609111300-1.csv
_OUTPUT_NAME = re.compile(r"^(\d{12})-(\d{12})(?:-\d+)?\.(?:csv|jsonl|json)$")


class QueueLogHandler(logging.Handler):
    """ログを画面のキューへ流す（どのスレッドから呼ばれてもよい）。"""

    def __init__(self, events: queue.Queue[tuple[str, object]]) -> None:
        super().__init__(logging.INFO)
        self.events = events
        self.setFormatter(logging.Formatter(FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.put((LOG, (record.levelno, self.format(record))))
        except Exception:
            self.handleError(record)


class App:
    def __init__(
        self,
        config: Config,
        minimized: bool = False,
        tray: bool = True,
        startup_warning: str | None = None,
    ) -> None:
        self.config = config
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.quitting = False
        self.closed = False
        self.last_export_text = ""
        self.profile_vars: dict[str, tuple[tk.BooleanVar, ttk.Checkbutton]] = {}
        self._tray_hint_shown = False

        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE}  v{__version__}")
        self.root.geometry("920x680")
        self.root.minsize(760, 560)
        self._setup_style()
        self._icon_photo = self._set_window_icon()
        self._build()
        self._load_into_form()
        self._load_log_tail()

        self._log_handler = QueueLogHandler(self.events)
        logging.getLogger().addHandler(self._log_handler)

        self.scheduler = Scheduler(lambda: self.config, self._post)
        self.tray = TrayIcon(
            on_show=lambda: self._post(TRAY, "show"),
            on_export=lambda: self._post(TRAY, "export"),
            on_toggle=lambda: self._post(TRAY, "toggle"),
            on_quit=lambda: self._post(TRAY, "quit"),
        )
        if tray:
            self.tray.start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if minimized and self.tray.available:
            self.root.withdraw()

        self._refresh_last_export()
        self._refresh_files()
        self._update_status()
        if startup_warning:
            self.root.after(300, lambda: self._show_error(startup_warning))
        self.root.after(800, self._check_legacy)
        if config.auto_export_on_launch:
            self.root.after(1500, self.start_auto)
        self.root.after(POLL_MS, self._poll)

    def run(self) -> None:
        self.root.mainloop()

    # -- 組み立て --------------------------------------------------------------

    def _setup_style(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=FONT_FAMILY, size=10)
            except tk.TclError:
                pass
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Status.TLabel", font=(FONT_FAMILY, 13, "bold"))
        style.configure("Sub.TLabel", foreground=SUB_COLOR)
        style.configure("Toggle.TButton", font=(FONT_FAMILY, 10, "bold"), padding=(14, 8))
        style.configure("Action.TButton", padding=(14, 8))
        linespace = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=int(linespace * 1.6))

    def _set_window_icon(self):
        try:
            from PIL import ImageTk

            photo = ImageTk.PhotoImage(make_icon_image(True), master=self.root)
            self.root.iconphoto(True, photo)
            return photo
        except Exception as exc:
            LOGGER.debug("ウィンドウのアイコンを設定できません: %s", exc)
            return None

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        # 状態と操作ボタン
        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        background = ttk.Style(self.root).lookup("TFrame", "background") or self.root.cget("bg")
        self.indicator = tk.Canvas(
            top, width=20, height=20, highlightthickness=0, background=background
        )
        self.dot = self.indicator.create_oval(3, 3, 18, 18, fill=STATES["idle"][0], outline="")
        self.indicator.grid(row=0, column=0, padx=(0, 8))
        self.status_var = tk.StringVar(value=STATES["idle"][1])
        ttk.Label(top, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        self.detail_var = tk.StringVar()
        ttk.Label(top, textvariable=self.detail_var, style="Sub.TLabel").grid(
            row=1, column=1, sticky="w"
        )
        buttons = ttk.Frame(top)
        buttons.grid(row=0, column=2, rowspan=2, sticky="e")
        self.export_button = ttk.Button(
            buttons, text="今すぐエクスポート", style="Action.TButton", command=self.export_now
        )
        self.export_button.pack(side="left", padx=(0, 8))
        self.toggle_button = ttk.Button(
            buttons, style="Toggle.TButton", width=22, command=self.toggle_auto
        )
        self.toggle_button.pack(side="left")

        # 直近の操作の結果
        self.message_label = ttk.Label(outer, text="", foreground=SUB_COLOR)
        self.message_label.grid(row=1, column=0, sticky="w", pady=(8, 0))

        # 旧コマンドライン版のタスクが残っているときだけ出す（_check_legacy で grid する）
        banner_bg = "#fff4e5"
        self.legacy_frame = tk.Frame(outer, background=banner_bg, padx=10, pady=6)
        self.legacy_frame.columnconfigure(0, weight=1)
        tk.Label(
            self.legacy_frame,
            text=(
                "旧バージョン（コマンドライン版）のタスク「ChromeHistoryExporter」が登録されています。"
                "二重に動かないよう削除してください。"
            ),
            background=banner_bg,
            foreground="#8a4b00",
            anchor="w",
            justify="left",
            wraplength=620,
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(self.legacy_frame, text="旧タスクを削除", command=self._remove_legacy).grid(
            row=0, column=1, padx=(8, 0)
        )

        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        self.notebook.add(self._build_files_tab(self.notebook), text="  出力ファイル  ")
        self.notebook.add(self._build_settings_tab(self.notebook), text="  設定  ")
        self.notebook.add(self._build_log_tab(self.notebook), text="  ログ  ")

    def _build_files_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=10)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        tree = ttk.Treeview(
            frame, columns=("period", "size", "name"), show="headings", selectmode="browse"
        )
        tree.heading("period", text="期間")
        tree.heading("size", text="サイズ")
        tree.heading("name", text="ファイル名")
        tree.column("period", width=280, anchor="w")
        tree.column("size", width=100, anchor="e", stretch=False)
        tree.column("name", width=320, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        tree.bind("<Double-1>", lambda _event: self._open_selected_file())
        tree.bind("<Return>", lambda _event: self._open_selected_file())
        self.files_tree = tree

        bottom = ttk.Frame(frame)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)
        self.files_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.files_var, style="Sub.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(bottom, text="再読込", command=self._refresh_files).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(bottom, text="ファイルを開く", command=self._open_selected_file).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Button(bottom, text="フォルダを開く", command=self._open_output_dir).grid(
            row=0, column=3, padx=(8, 0)
        )
        return frame

    def _build_settings_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=12)
        frame.columnconfigure(1, weight=1)
        pad = {"padx": 4, "pady": 5}

        ttk.Label(frame, text="Chrome のデータ").grid(row=0, column=0, sticky="w", **pad)
        self.user_data_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.user_data_var).grid(
            row=0, column=1, columnspan=2, sticky="ew", **pad
        )
        ttk.Button(frame, text="参照…", width=8, command=self._browse_user_data).grid(
            row=0, column=3, sticky="ew", **pad
        )

        ttk.Label(frame, text="プロファイル").grid(row=1, column=0, sticky="nw", **pad)
        self.profiles_frame = ttk.Frame(frame)
        self.profiles_frame.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        self.all_profiles_var = tk.BooleanVar(value=True)
        ttk.Button(frame, text="再検出", width=8, command=self._reload_profiles).grid(
            row=1, column=3, sticky="new", **pad
        )

        ttk.Separator(frame).grid(row=2, column=0, columnspan=4, sticky="ew", pady=8)

        ttk.Label(frame, text="出力先").grid(row=3, column=0, sticky="w", **pad)
        self.output_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.output_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", **pad
        )
        ttk.Button(frame, text="参照…", width=8, command=self._browse_output).grid(
            row=3, column=3, sticky="ew", **pad
        )

        ttk.Label(frame, text="出力形式").grid(row=4, column=0, sticky="w", **pad)
        self.format_var = tk.StringVar()
        ttk.Combobox(
            frame,
            textvariable=self.format_var,
            values=[FORMAT_LABELS[name] for name in VALID_FORMATS],
            state="readonly",
            width=28,
        ).grid(row=4, column=1, sticky="w", **pad)

        ttk.Label(frame, text="実行間隔").grid(row=5, column=0, sticky="w", **pad)
        interval = ttk.Frame(frame)
        interval.grid(row=5, column=1, sticky="w", **pad)
        self.interval_var = tk.StringVar()
        ttk.Combobox(interval, textvariable=self.interval_var, values=INTERVAL_CHOICES, width=8).pack(
            side="left"
        )
        ttk.Label(interval, text="分ごと").pack(side="left", padx=(6, 0))

        ttk.Label(frame, text="初回の対象").grid(row=6, column=0, sticky="w", **pad)
        lookback = ttk.Frame(frame)
        lookback.grid(row=6, column=1, sticky="w", **pad)
        ttk.Label(lookback, text="過去").pack(side="left", padx=(0, 6))
        self.lookback_var = tk.StringVar()
        ttk.Spinbox(lookback, textvariable=self.lookback_var, from_=0, to=8760, width=8).pack(side="left")
        ttk.Label(lookback, text="時間分（前回の記録が無いときだけ使います）", style="Sub.TLabel").pack(
            side="left", padx=(6, 0)
        )

        self.skip_empty_var = tk.BooleanVar()
        ttk.Checkbutton(
            frame, text="履歴が 0 件の期間はファイルを作らない", variable=self.skip_empty_var
        ).grid(row=7, column=1, columnspan=3, sticky="w", **pad)

        ttk.Separator(frame).grid(row=8, column=0, columnspan=4, sticky="ew", pady=8)

        ttk.Label(frame, text="動作").grid(row=9, column=0, sticky="nw", **pad)
        behavior = ttk.Frame(frame)
        behavior.grid(row=9, column=1, columnspan=3, sticky="w", **pad)
        self.auto_launch_var = tk.BooleanVar()
        self.close_to_tray_var = tk.BooleanVar()
        self.autostart_var = tk.BooleanVar()
        ttk.Checkbutton(
            behavior, text="アプリを起動したら自動エクスポートを始める", variable=self.auto_launch_var
        ).pack(anchor="w")
        ttk.Checkbutton(
            behavior, text="× で閉じたらタスクトレイに格納する", variable=self.close_to_tray_var
        ).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(
            behavior,
            text="Windows にサインインしたら自動で起動する（すぐ反映）",
            variable=self.autostart_var,
            command=self._on_autostart_changed,
        ).pack(anchor="w", pady=(4, 0))

        actions = ttk.Frame(frame)
        actions.grid(row=10, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        ttk.Button(actions, text="設定を保存", style="Action.TButton", command=self._save_settings).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(actions, text="元に戻す", command=self._load_into_form).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        self.config_path_var = tk.StringVar()
        ttk.Label(actions, textvariable=self.config_path_var, style="Sub.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        return frame

    def _build_log_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=10)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log_text = ScrolledText(
            frame,
            wrap="char",
            state="disabled",
            font=(FONT_FAMILY, 9),
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=4,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("error", foreground=ERROR_COLOR)
        self.log_text.tag_configure("warning", foreground="#b26a00")

        bottom = ttk.Frame(frame)
        bottom.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, text=f"ログファイル: {self.config.log_path}", style="Sub.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(bottom, text="ログファイルを開く", command=self._open_log_file).grid(
            row=0, column=1, padx=(8, 0)
        )
        return frame

    # -- 設定とフォーム ----------------------------------------------------------

    def _load_into_form(self) -> None:
        c = self.config
        detected = paths.find_user_data_dir()
        self.user_data_var.set(c.user_data_dir or (str(detected) if detected else ""))
        self.output_var.set(str(c.output_path))
        self.format_var.set(FORMAT_LABELS.get(c.output_format, c.output_format))
        self.interval_var.set(str(c.interval_minutes))
        self.lookback_var.set(str(c.initial_lookback_hours))
        self.skip_empty_var.set(c.skip_empty)
        self.auto_launch_var.set(c.auto_export_on_launch)
        self.close_to_tray_var.set(c.close_to_tray)
        self.autostart_var.set(autostart.is_enabled())
        self.config_path_var.set(f"設定ファイル: {c.config_path or paths.default_config_path()}")
        self._reload_profiles(list(c.profiles))

    def _read_form(self) -> Config:
        label = self.format_var.get()
        output_format = next((k for k, v in FORMAT_LABELS.items() if v == label), label)
        user_data = self.user_data_var.get().strip()
        if user_data and not Path(user_data).expanduser().is_dir():
            raise ConfigError(f"Chrome のデータフォルダが見つかりません: {user_data}")
        config = dataclasses.replace(
            self.config,
            user_data_dir=user_data or None,
            profiles=self._selected_profiles(),
            output_dir=self.output_var.get().strip() or None,
            output_format=output_format,
            interval_minutes=_parse_int(self.interval_var.get(), "実行間隔"),
            initial_lookback_hours=_parse_int(self.lookback_var.get(), "初回の対象"),
            skip_empty=self.skip_empty_var.get(),
            auto_export_on_launch=self.auto_launch_var.get(),
            close_to_tray=self.close_to_tray_var.get(),
        )
        config.validate()
        return config

    def _save_settings(self) -> None:
        try:
            config = self._read_form()
            config.save()
        except ConfigError as exc:
            messagebox.showerror(APP_TITLE, f"設定を確認してください。\n{exc}", parent=self.root)
            return
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"設定を保存できませんでした。\n{exc}", parent=self.root)
            return
        interval_changed = config.interval_minutes != self.config.interval_minutes
        self.config = config
        LOGGER.info("設定を保存しました: %s", config.config_path)
        if interval_changed:
            self.scheduler.reschedule()
        self._refresh_last_export()
        self._refresh_files()
        self._set_message("設定を保存しました", OK_COLOR)

    def _selected_profiles(self) -> list[str]:
        if self.all_profiles_var.get():
            return ["*"]
        return [name for name, (var, _) in self.profile_vars.items() if var.get()]

    def _reload_profiles(self, selected: list[str] | None = None) -> None:
        if selected is None:
            selected = self._selected_profiles()
        for child in self.profiles_frame.winfo_children():
            child.destroy()
        self.profile_vars = {}

        self.all_profiles_var.set("*" in selected)
        ttk.Checkbutton(
            self.profiles_frame,
            text="すべて（後から増えたプロファイルも含む）",
            variable=self.all_profiles_var,
            command=self._update_profile_checks,
        ).grid(row=0, column=0, sticky="w")

        found: list[str] = []
        names: dict[str, str] = {}
        user_data = self.user_data_var.get().strip()
        if user_data and Path(user_data).expanduser().is_dir():
            folder = Path(user_data).expanduser()
            try:
                found = [name for name, _ in chrome.list_profiles(folder, None)]
            except OSError as exc:
                LOGGER.warning("プロファイルを検出できません: %s", exc)
            names = chrome.profile_display_names(folder)

        missing = [name for name in selected if name != "*" and name not in found]
        for row, name in enumerate(found + missing, start=1):
            label = names.get(name)
            text = f"{label}（{name}）" if label and label != name else name
            if name in missing:
                text += "　※見つかりません"
            var = tk.BooleanVar(value=name in selected)
            check = ttk.Checkbutton(self.profiles_frame, text=text, variable=var)
            check.grid(row=row, column=0, sticky="w", padx=(22, 0), pady=(2, 0))
            self.profile_vars[name] = (var, check)
        if not found and not missing:
            ttk.Label(
                self.profiles_frame, text="プロファイルが見つかりません", style="Sub.TLabel"
            ).grid(row=1, column=0, sticky="w", padx=(22, 0))
        self._update_profile_checks()

    def _update_profile_checks(self) -> None:
        state = "disabled" if self.all_profiles_var.get() else "normal"
        for _, check in self.profile_vars.values():
            check.configure(state=state)

    def _browse_user_data(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root,
            initialdir=self.user_data_var.get() or None,
            title="Chrome の User Data フォルダ",
        )
        if chosen:
            self.user_data_var.set(str(Path(chosen)))
            self._reload_profiles()

    def _browse_output(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root, initialdir=self.output_var.get() or None, title="出力先のフォルダ"
        )
        if chosen:
            self.output_var.set(str(Path(chosen)))

    def _on_autostart_changed(self) -> None:
        try:
            if self.autostart_var.get():
                autostart.enable()
            else:
                autostart.disable()
        except OSError as exc:
            self.autostart_var.set(autostart.is_enabled())
            messagebox.showerror(APP_TITLE, f"自動起動の設定に失敗しました。\n{exc}", parent=self.root)

    # -- 旧コマンドライン版 --------------------------------------------------------

    def _check_legacy(self) -> None:
        if not self.closed and autostart.legacy_task_exists():
            self.legacy_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))

    def _remove_legacy(self) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            "旧バージョンのタスク「ChromeHistoryExporter」をタスクスケジューラから削除し、"
            "動いている旧バージョンに停止を要求します。\n\nよろしいですか？",
            parent=self.root,
        ):
            return
        try:
            # 旧版は既定の場所の stop.flag を見ている
            autostart.remove_legacy(paths.app_data_dir() / "stop.flag")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self.root)
            return
        self.legacy_frame.grid_remove()
        messagebox.showinfo(
            APP_TITLE,
            "旧タスクを削除しました。動いていた旧バージョンは数秒で終了します。",
            parent=self.root,
        )

    # -- エクスポート ------------------------------------------------------------

    def export_now(self) -> None:
        if not self.quitting:
            self.scheduler.run_now()

    def start_auto(self) -> None:
        if not self.quitting:
            self.scheduler.start()

    def toggle_auto(self) -> None:
        if self.scheduler.enabled:
            self.scheduler.stop()
        else:
            self.start_auto()

    # -- 別スレッドからの通知 --------------------------------------------------

    def _post(self, kind: str, payload: object = None) -> None:
        """スケジューラ・トレイ・ログのスレッドから呼ばれる。画面の操作は _poll で行う。"""
        self.events.put((kind, payload))

    def _poll(self) -> None:
        while not self.closed:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle(kind, payload)
        if self.closed:
            return
        self._update_status()
        if not self.closed:
            self.root.after(POLL_MS, self._poll)

    def _handle(self, kind: str, payload: object) -> None:
        if kind == LOG:
            level, line = payload  # type: ignore[misc]
            self._append_log(line, level)
        elif kind == BUSY:
            self._set_message("書き出しています…", SUB_COLOR)
        elif kind == DONE:
            self._on_export_done(payload)  # type: ignore[arg-type]
        elif kind == FAILED:
            self._set_message(f"エラー: {payload}", ERROR_COLOR)
            if self.root.state() == "withdrawn":
                self.tray.notify(f"エクスポートに失敗しました: {payload}")
        elif kind == SCHEDULE:
            self.tray.set_active(self.scheduler.enabled)
        elif kind == TRAY:
            if payload == "show":
                self.show_window()
            elif payload == "export":
                self.export_now()
            elif payload == "toggle":
                self.toggle_auto()
            elif payload == "quit":
                self.quit()

    def _on_export_done(self, result: ExportResult) -> None:
        period = _format_period(result.start, result.end)
        if result.file_path:
            self._set_message(
                f"{period} の {result.record_count} 件を {result.file_path.name} に書き出しました",
                OK_COLOR,
            )
        else:
            self._set_message(f"{period}: {result.skipped_reason}", SUB_COLOR)
        self._refresh_last_export()
        self._refresh_files()

    def _update_status(self) -> None:
        if self.quitting:
            if not self.scheduler.alive:
                self._finish_quit()
            return
        scheduler = self.scheduler
        key = "busy" if scheduler.busy else "active" if scheduler.enabled else "idle"
        color, title = STATES[key]
        self.indicator.itemconfigure(self.dot, fill=color)
        self.status_var.set(title)

        parts = []
        next_run = scheduler.next_run
        if key == "active" and next_run is not None:
            seconds = (next_run - datetime.now().astimezone()).total_seconds()
            parts.append(f"次回 {next_run:%H:%M}（あと {max(0, math.ceil(seconds / 60))} 分）")
        elif key == "idle":
            parts.append("自動エクスポートは止まっています")
        parts.append(self.last_export_text)
        self.detail_var.set("　・　".join(part for part in parts if part))

        self.toggle_button.configure(
            text="■ 自動エクスポートを停止" if scheduler.enabled else "▶ 自動エクスポートを開始"
        )
        self.export_button.configure(state="disabled" if scheduler.busy else "normal")

    def _refresh_last_export(self) -> None:
        state = State(self.config.state_path)
        end = state.last_export_end
        if end is None:
            self.last_export_text = "まだ書き出していません"
            return
        count = state.as_dict().get("last_record_count")
        text = f"{end.astimezone():%m/%d %H:%M} まで書き出し済み"
        if isinstance(count, int):
            text += f"（直近 {count} 件）"
        self.last_export_text = text

    def _set_message(self, text: str, color: str) -> None:
        self.message_label.configure(text=text, foreground=color)

    def _show_error(self, message: str) -> None:
        if self.root.state() == "withdrawn":
            self.tray.notify(message)
        else:
            messagebox.showerror(APP_TITLE, message, parent=self.root)

    # -- ログ ----------------------------------------------------------------------

    def _load_log_tail(self) -> None:
        try:
            with self.config.log_path.open(encoding="utf-8", errors="replace") as fp:
                tail = deque(fp, maxlen=LOG_TAIL_LINES)
        except OSError:
            return
        for line in tail:
            if " ERROR " in line:
                level = logging.ERROR
            elif " WARNING " in line:
                level = logging.WARNING
            else:
                level = logging.INFO
            self._append_log(line.rstrip("\n"), level)

    def _append_log(self, line: str, level: int) -> None:
        tag = "error" if level >= logging.ERROR else "warning" if level >= logging.WARNING else ""
        text = self.log_text
        at_bottom = text.yview()[1] >= 0.999
        text.configure(state="normal")
        text.insert("end", line + "\n", tag)
        lines = int(text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            text.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        text.configure(state="disabled")
        if at_bottom:
            text.see("end")

    # -- ファイル ----------------------------------------------------------------

    def _refresh_files(self) -> None:
        tree = self.files_tree
        selected = tree.selection()
        tree.delete(*tree.get_children())
        folder = self.config.output_path
        try:
            files = sorted(
                (p for p in folder.iterdir() if p.is_file() and _OUTPUT_NAME.match(p.name)),
                key=lambda p: p.name,
                reverse=True,  # 名前が開始時刻で始まるので、新しい順になる
            )
        except OSError:
            files = []
        for path in files[:MAX_FILES]:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            tree.insert("", "end", iid=str(path), values=(_period_label(path.name), _format_size(size), path.name))
        if selected and tree.exists(selected[0]):
            tree.selection_set(selected[0])
        shown = f"（新しい {MAX_FILES} 件を表示）" if len(files) > MAX_FILES else ""
        self.files_var.set(f"{len(files)} ファイル{shown}　{folder}")

    def _open_selected_file(self) -> None:
        selection = self.files_tree.selection()
        if selection and Path(selection[0]).exists():
            _open_path(Path(selection[0]))

    def _open_output_dir(self) -> None:
        folder = self.config.output_path
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._show_error(f"フォルダを作れません: {exc}")
            return
        _open_path(folder)

    def _open_log_file(self) -> None:
        if self.config.log_path.exists():
            _open_path(self.config.log_path)

    # -- ウィンドウ ----------------------------------------------------------------

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self) -> None:
        if self.config.close_to_tray and self.tray.available:
            self.root.withdraw()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.notify("トレイで動作を続けます。終了はアイコンの右クリックから。")
            return
        self.quit()

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        # 書き出し中ならその完了を待つ（スレッドが終わったら _update_status が閉じる）
        self.scheduler.shutdown()
        self.status_var.set("終了しています…")
        self.export_button.configure(state="disabled")
        self.toggle_button.configure(state="disabled")

    def _finish_quit(self) -> None:
        if self.closed:
            return
        self.closed = True
        logging.getLogger().removeHandler(self._log_handler)
        self.tray.stop()
        self.root.destroy()


def _parse_int(text: str, label: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise ConfigError(f"{label}は整数で入力してください: {text!r}") from None


def _format_period(start: datetime, end: datetime) -> str:
    start, end = start.astimezone(), end.astimezone()
    if start.date() == end.date():
        return f"{start:%Y/%m/%d %H:%M} 〜 {end:%H:%M}"
    return f"{start:%Y/%m/%d %H:%M} 〜 {end:%m/%d %H:%M}"


def _period_label(name: str) -> str:
    match = _OUTPUT_NAME.match(name)
    if not match:
        return name
    try:
        return _format_period(parse_stamp(match[1]), parse_stamp(match[2]))
    except ValueError:
        return name


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:,.1f} KB"


def _open_path(path: Path) -> None:
    os.startfile(str(path))  # type: ignore[attr-defined]
