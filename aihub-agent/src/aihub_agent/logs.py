from collections import deque
from pathlib import Path
from threading import Lock


class AppLogStore:
    """Per-app rolling log: in-memory ring + on-disk append.

    Subprocess stdout/stderr is fed in line-by-line by the supervisor.
    """

    def __init__(self, log_dir: Path, app_id: str, max_lines: int = 1000):
        self.app_id = app_id
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"{app_id}.log"
        self._buf: deque[str] = deque(maxlen=max_lines)
        self._lock = Lock()

    def append(self, line: str) -> None:
        line = line.rstrip("\r\n")
        with self._lock:
            self._buf.append(line)
            try:
                with self.path.open("a", encoding="utf-8", errors="replace") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def tail(self, n: int = 200) -> list[str]:
        with self._lock:
            return list(self._buf)[-n:]

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
