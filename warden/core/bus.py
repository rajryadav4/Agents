"""Tiny thread-safe pub/sub used to stream events to the dashboard (SSE)."""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any, Callable


class EventBus:
    def __init__(self, history: int = 500) -> None:
        self._lock = threading.Lock()
        self._async_subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._sync_subs: list[Callable[[dict[str, Any]], None]] = []
        self.recent: deque[dict[str, Any]] = deque(maxlen=history)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.recent.append(event)
            subs = list(self._async_subs)
            sync_subs = list(self._sync_subs)
        for loop, queue in subs:
            try:
                loop.call_soon_threadsafe(_put_nowait, queue, event)
            except RuntimeError:  # loop closed
                self.unsubscribe(queue)
        for fn in sync_subs:
            fn(event)

    def subscribe_async(self) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._async_subs.append((loop, queue))
        return queue

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._sync_subs.append(fn)

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._async_subs = [(l, q) for (l, q) in self._async_subs if q is not queue]

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._async_subs) + len(self._sync_subs)


def _put_nowait(queue: asyncio.Queue, event: dict[str, Any]) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(event)
