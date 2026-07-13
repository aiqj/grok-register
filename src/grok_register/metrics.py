"""Lightweight registration metrics (browser path only)."""

from __future__ import annotations

import threading
from typing import Any


class RegisterMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def inc(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counts[key] = int(self._counts.get(key, 0)) + int(n)

    def get(self, key: str) -> int:
        with self._lock:
            return int(self._counts.get(key, 0))

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def record_attempt(self, *, ok: bool) -> None:
        self.inc("register:attempt")
        self.inc(f"register:{'success' if ok else 'fail'}")


default_metrics = RegisterMetrics()
