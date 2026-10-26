"""Time source for leases and backoff. Tests inject FakeClock so lapses need no sleeping."""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Seconds since the Unix epoch."""
        ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class FakeClock:
    """A clock that only moves when told to. Thread-safe."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> float:
        with self._lock:
            self._now += seconds
            return self._now
