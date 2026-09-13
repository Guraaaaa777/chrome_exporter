"""タスクトレイのアイコン（pystray が使えなければ何もしない）。"""

from __future__ import annotations

import logging
from typing import Callable

from . import APP_TITLE

LOGGER = logging.getLogger(__name__)


def make_icon_image(active: bool, size: int = 64):
    """丸の中に時計の絵。自動エクスポート中は青、停止中は灰色。"""
    from PIL import Image, ImageDraw

    s = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    background = (26, 115, 232, 255) if active else (112, 117, 122, 255)
    draw.ellipse((2 * s, 2 * s, 62 * s, 62 * s), fill=background)
    white = (255, 255, 255, 255)
    width = max(1, round(5 * s))
    draw.ellipse((13 * s, 13 * s, 51 * s, 51 * s), outline=white, width=width)
    draw.line((32 * s, 32 * s, 32 * s, 20 * s), fill=white, width=width)
    draw.line((32 * s, 32 * s, 41 * s, 38 * s), fill=white, width=width)
    return image


class TrayIcon:
    """トレイのアイコンとメニュー。

    メニューの操作は pystray のスレッドで呼ばれるので、コールバック側では
    画面を直接触らず、キューに積むだけにすること。
    """

    def __init__(
        self,
        on_show: Callable[[], None],
        on_export: Callable[[], None],
        on_toggle: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._on_show = on_show
        self._on_export = on_export
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        self._active = False
        self._icon = None

    @property
    def available(self) -> bool:
        return self._icon is not None

    def start(self) -> None:
        try:
            import pystray
        except Exception as exc:
            LOGGER.info("pystray が使えないため、トレイアイコンは出しません: %s", exc)
            return
        menu = pystray.Menu(
            pystray.MenuItem("画面を開く", lambda icon, item: self._on_show(), default=True),
            pystray.MenuItem("今すぐエクスポート", lambda icon, item: self._on_export()),
            pystray.MenuItem(
                lambda item: "自動エクスポートを停止" if self._active else "自動エクスポートを開始",
                lambda icon, item: self._on_toggle(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("終了", lambda icon, item: self._on_quit()),
        )
        try:
            icon = pystray.Icon("chrome_history_exporter", make_icon_image(False), APP_TITLE, menu)
            icon.run_detached()
        except Exception as exc:
            LOGGER.warning("トレイアイコンを出せません: %s", exc)
            return
        self._icon = icon

    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        if self._icon is None:
            return
        try:
            self._icon.icon = make_icon_image(active)
            self._icon.title = f"{APP_TITLE}（自動エクスポート中）" if active else APP_TITLE
            self._icon.update_menu()
        except Exception as exc:
            LOGGER.debug("トレイアイコンを更新できません: %s", exc)

    def notify(self, message: str) -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message, APP_TITLE)
        except Exception as exc:
            LOGGER.debug("通知を出せません: %s", exc)

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception as exc:
                LOGGER.debug("トレイアイコンを消せません: %s", exc)
