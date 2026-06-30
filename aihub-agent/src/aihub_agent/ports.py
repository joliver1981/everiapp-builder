import asyncio
import socket
from .config import settings


class PortPool:
    def __init__(self, start: int, end: int):
        self._pool: set[int] = set(range(start, end + 1))
        self._used: set[int] = set()
        self._lock = asyncio.Lock()

    async def allocate(self, preferred: int | None = None) -> int:
        async with self._lock:
            if preferred is not None and preferred in self._pool and preferred not in self._used:
                self._used.add(preferred)
                return preferred
            available = self._pool - self._used
            if not available:
                raise RuntimeError("No available ports in agent pool")
            port = min(available)
            self._used.add(port)
            return port

    async def release(self, port: int) -> None:
        async with self._lock:
            self._used.discard(port)

    @property
    def used(self) -> list[int]:
        return sorted(self._used)

    @property
    def total(self) -> int:
        return len(self._pool)


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Best-effort sanity check — does anything currently hold this port?"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


port_pool = PortPool(settings.app_port_range_start, settings.app_port_range_end)
