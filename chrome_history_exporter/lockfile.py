"""同じ状態ファイルを複数のプロセスで同時に更新しないためのファイルロック。"""

from __future__ import annotations

import msvcrt
import os
import time
from pathlib import Path
from types import TracebackType

# ロック待ちの再試行間隔（秒）
_RETRY_SECONDS = 0.1


class AlreadyRunning(Exception):
    pass


class ProcessLock:
    """Windows のファイルロック（msvcrt.locking）を使った排他制御。

    ロックはプロセス終了時（強制終了を含む）に OS が自動解放するため、
    残骸ファイルで動かなくなることがない。
    """

    def __init__(self, path: Path):
        self.path = path
        self._fp = None

    def acquire(self, timeout: float = 0) -> None:
        """ロックを取得する。取れなければ timeout 秒まで待ち、それでも駄目なら AlreadyRunning。"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._try_acquire()
                return
            except AlreadyRunning:
                if time.monotonic() >= deadline:
                    raise
            time.sleep(_RETRY_SECONDS)

    def _try_acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("a+")
        try:
            self._fp.seek(0)
            msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self._fp.close()
            self._fp = None
            raise AlreadyRunning(f"既に別のプロセスが動作しています（ロック: {self.path}）") from exc

        self._fp.seek(0)
        self._fp.truncate()
        self._fp.write(str(os.getpid()))
        self._fp.flush()

    def release(self) -> None:
        if self._fp is None:
            return
        try:
            self._fp.seek(0)
            msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self._fp.close()
            self._fp = None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.release()
