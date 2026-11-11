"""Run forever: poll each source on its own interval, back off when one fails."""

from __future__ import annotations

import logging
import random
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .engine import Engine
from .sources import Source

log = logging.getLogger("dealhawk.scheduler")
MAX_BACKOFF = 3600


class Scheduler:
    def __init__(self, engine: Engine, sources: list[Source], workers: int = 8,
                 clock=time.monotonic, sleep=None):
        self.engine = engine
        self.sources = sources
        self.workers = workers
        self.clock = clock
        self._stop = threading.Event()
        self.sleep = sleep or (lambda s: self._stop.wait(s))
        # stagger first runs a little so we don't hit everything in the same second
        now = clock()
        self.next_run = {s.name: now + i * 0.5 for i, s in enumerate(sources)}
        self.failures = {s.name: 0 for s in sources}

    def stop(self, *_):
        log.info("stopping…")
        self._stop.set()

    def due(self) -> list[Source]:
        now = self.clock()
        return [s for s in self.sources if self.next_run[s.name] <= now]

    def _reschedule(self, s: Source, ok: bool) -> None:
        if ok:
            self.failures[s.name] = 0
            delay = s.interval
        else:
            self.failures[s.name] += 1
            delay = min(s.interval * 2 ** self.failures[s.name], MAX_BACKOFF)
        self.next_run[s.name] = self.clock() + delay * random.uniform(0.9, 1.1)

    def _status(self, s: Source, ok: bool, **extra) -> None:
        try:
            self.engine.store.set_source_status(s.name, {
                "name": s.name, "type": s.type, "ok": ok, "last_run": time.time(), "interval": s.interval,
                "failures": self.failures[s.name], **extra})
        except Exception as e:  # pragma: no cover - status is best-effort
            log.debug("could not save status for %s: %s", s.name, e)

    def tick(self, pool: ThreadPoolExecutor) -> int:
        """Run every due source once; returns number of alerts sent."""
        due = self.due()
        if not due:
            return 0
        futures = {pool.submit(s.fetch): s for s in due}
        alerts = 0
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                offers = fut.result()
            except Exception as e:
                self._reschedule(s, ok=False)
                from .tracking import explain
                log.warning("%s failed (%d in a row): %s", s.name, self.failures[s.name], e)
                self._status(s, ok=False, error=explain(e)[:300])
                continue
            report = self.engine.process(offers)   # processed on this thread: one writer
            alerts += len(report.alerted)
            self._reschedule(s, ok=True)
            self._status(s, ok=True, offers=report.offers, matched=report.matched, alerted=len(report.alerted))
            log.info("%s: %d offers, %d matched, %d alerted", s.name, report.offers, report.matched,
                     len(report.alerted))
        return alerts

    def run(self, max_ticks: int | None = None) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
        log.info("watching %d sources: %s", len(self.sources), ", ".join(s.name for s in self.sources))
        ticks = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while not self._stop.is_set():
                self.tick(pool)
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                wait = max(1.0, min(self.next_run.values()) - self.clock()) if self.next_run else 60
                self.sleep(wait)
