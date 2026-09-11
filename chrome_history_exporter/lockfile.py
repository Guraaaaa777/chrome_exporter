"""多重起動を防ぐためのプロセスロック（Windows / POSIX 両対応）。"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType


class AlreadyRunning(Exception):
    pass


class ProcessLock:
    """OS のファイルロックを使った単一インスタンス制御。

    ロックはプロセス終了時（強制終了を含む）に OS が自動解放するため、
    残骸ファイルで起動不能になることがない。
    """

    def __init__(self, path: Path):
        self.path = path
        self._fp = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt

                self._fp.seek(0)
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            if os.name == "nt":
                import msvcrt

                self._fp.seek(0)
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
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
