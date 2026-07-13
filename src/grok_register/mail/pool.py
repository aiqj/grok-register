"""Background mailbox pre-create pool to hide TempMail/API latency.

When enabled, a daemon thread keeps N inboxes ready. Registration threads
acquire() a ready (email, token) instead of blocking on create API mid-flow.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Optional

LogFn = Callable[[str], None]

_lock = threading.Lock()
_pool: Optional["MailboxPool"] = None


class MailboxPool:
    def __init__(
        self,
        create_fn: Callable[[], tuple[str, str]],
        *,
        size: int = 3,
        log: LogFn | None = None,
    ):
        self._create = create_fn
        self._size = max(1, min(int(size or 3), 10))
        self._log = log or (lambda _m: None)
        self._q: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=self._size + 2)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._created = 0
        self._acquired = 0
        self._create_fail = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="mailbox-pool", daemon=True
        )
        self._thread.start()
        self._log(f"[*] 邮箱预创建池启动 target={self._size}")

    def stop(self, wait: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=wait)
        # drain
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._log(
            f"[*] 邮箱预创建池停止 created={self._created} acquired={self._acquired} fail={self._create_fail}"
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._q.qsize() >= self._size:
                    time.sleep(0.4)
                    continue
                email, token = self._create()
                if email and token:
                    try:
                        self._q.put((email, token), timeout=2)
                        self._created += 1
                    except queue.Full:
                        pass
                else:
                    self._create_fail += 1
                    time.sleep(0.5)
            except Exception as exc:
                self._create_fail += 1
                self._log(f"[!] 邮箱预创建失败: {exc}")
                time.sleep(1.0)

    def acquire(self, timeout: float = 45.0) -> tuple[str, str]:
        """Get a ready mailbox; may block until create or timeout."""
        deadline = time.time() + max(1.0, float(timeout))
        while time.time() < deadline:
            try:
                item = self._q.get(timeout=0.5)
                self._acquired += 1
                return item
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
        raise TimeoutError(f"mailbox pool empty after {timeout}s")

    def stats(self) -> dict[str, Any]:
        return {
            "size": self._size,
            "ready": self._q.qsize(),
            "created": self._created,
            "acquired": self._acquired,
            "fail": self._create_fail,
            "alive": bool(self._thread and self._thread.is_alive()),
        }


def start_mail_pool(
    create_fn: Callable[[], tuple[str, str]],
    *,
    size: int = 3,
    log: LogFn | None = None,
) -> MailboxPool:
    global _pool
    with _lock:
        if _pool is not None:
            try:
                _pool.stop(wait=0.5)
            except Exception:
                pass
        _pool = MailboxPool(create_fn, size=size, log=log)
        _pool.start()
        return _pool


def stop_mail_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            try:
                _pool.stop()
            except Exception:
                pass
            _pool = None


def get_mail_pool() -> MailboxPool | None:
    with _lock:
        return _pool


def acquire_mailbox(timeout: float = 45.0) -> tuple[str, str] | None:
    """Return precreated mailbox or None if pool inactive/empty+timeout."""
    p = get_mail_pool()
    if p is None or not p.stats().get("alive"):
        return None
    try:
        return p.acquire(timeout=timeout)
    except Exception:
        return None
