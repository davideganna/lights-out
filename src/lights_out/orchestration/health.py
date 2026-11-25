"""Heartbeat: the time of the last sign of life, served by /healthz."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

# While an agent call runs, the pipeline beats this often (seconds).
AGENT_BEAT_S = 60


class Heartbeat:
    """Thread-safe: the asyncio thread writes it, the HTTP server threads read it."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._last = clock()  # a fresh process counts as alive
        self._card: str | None = None
        self._stage: str | None = None

    def beat(self) -> None:
        with self._lock:
            self._last = self._clock()

    def set_work(self, card: str | None, stage: str | None) -> None:
        with self._lock:
            self._card, self._stage = card, stage
            self._last = self._clock()

    def clear_work(self) -> None:
        self.set_work(None, None)

    def snapshot(self, stale_s: int) -> tuple[int, dict]:
        with self._lock:
            last, card, stage = self._last, self._card, self._stage
        age = int(self._clock() - last)
        ok = age <= stale_s
        return (200 if ok else 503), {
            "status": "ok" if ok else "stale",
            "last_beat": datetime.fromtimestamp(last, UTC).isoformat(timespec="seconds"),
            "age_s": age,
            "current_card": card,
            "stage": stage,
        }
