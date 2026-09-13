#!/usr/bin/env python3
"""画面を起動するエントリスクリプト（PyInstaller のビルドもこのファイルを入口にする）。

どのカレントディレクトリから起動されてもパッケージを解決できるように、
このファイルの場所を sys.path へ追加してから起動する。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chrome_history_exporter.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
