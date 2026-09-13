"""exe をビルドする: python build.py

dist\\ChromeHistoryExporter.exe（1 ファイル・コンソールなし）を作る。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from chrome_history_exporter.tray import make_icon_image  # noqa: E402

NAME = "ChromeHistoryExporter"
ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
# このPCに入っているが、アプリでは使わない大きなライブラリを巻き込まないようにする
EXCLUDES = ["numpy", "matplotlib", "PySide6", "shiboken6", "PyQt5", "PyQt6"]


def make_icon(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    make_icon_image(True, 256).save(path, sizes=ICON_SIZES)


def main() -> int:
    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        print("PyInstaller が必要です: python -m pip install -r requirements-build.txt")
        return 1

    icon = ROOT / "build" / "app.ico"
    make_icon(icon)
    args = [
        str(ROOT / "run.py"),
        "--name", NAME,
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--icon", str(icon),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
        "--hidden-import", "pystray._win32",
        "--hidden-import", "PIL._tkinter_finder",
    ]
    for module in EXCLUDES:
        args += ["--exclude-module", module]
    pyinstaller.run(args)
    print(f"ビルドしました: {ROOT / 'dist' / (NAME + '.exe')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
