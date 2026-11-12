# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Bounded, best-effort batch delivery on one background thread per signal.

Capacity counts the batch being exported. One batch is exported at a time; the
OTLP exporter owns retries within its timeout. Records only ever wait in
memory: there is no disk persistence.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from ._options import CaptureCode, QueueOptions
from ._suppression import reporting

T = TypeVar("T")


@dataclass(frozen=True)
class QueueStats:
    accepted: int
    exported: int
    dropped: int
    pending: int


class _Batch(Generic[T]):
    def __init__(self, records: list[tuple[int, T]]) -> None:
        self.records = records
        self.dropped = False


class ExportQueue(Generic[T]):
    def __init__(
        self,
        name: str,
        options: QueueOptions,
        export: Callable[[Sequence[T]], bool],
        report: Callable[[CaptureCode, int], None],
        shutdown_exporter: Callable[[], None],
    ) -> None:
        self._options = options
        self._export = export
        self._report = report
        self._shutdown_exporter = shutdown_exporter
        self._lock = threading.Condition()
        self._records: deque[tuple[int, T]] = deque()
        self._active: _Batch[T] | None = None
        self._accepted = 0
        self._exported = 0
        self._dropped = 0
        self._pending = 0
        self._force = 0
        self._closed = False
        self._stop_once = threading.Lock()
        self._stopped: threading.Event | None = None
        self._worker = threading.Thread(target=self._run, name=f"agentrail-{name}", daemon=True)
        self._worker.start()

    def diagnostics(self) -> QueueStats:
        with self._lock:
            return QueueStats(self._accepted, self._exported, self._dropped, self._pending)

    def add(self, value: T) -> bool:
        """Never waits for an export. False means the record was dropped."""
        with self._lock:
            retained = len(self._records) + (0 if self._active is None else len(self._active.records))
            code: CaptureCode | None = None
            if self._closed:
                code = "closed"
            elif retained >= self._options.max_records:
                code = "queue_full"
            if code is None:
                self._accepted += 1
                self._pending += 1
                self._records.append((self._accepted, value))
                if len(self._records) >= self._options.batch_size:
                    self._lock.notify_all()
                return True
            self._dropped += 1
        self._report(code, 1)
        return False

    def flush(self, timeout: float | None = None) -> None:
        """Attempts every record accepted when called. Delivery is not guaranteed."""
        limit = self._options.flush_timeout if timeout is None else min(timeout, self._options.flush_timeout)
        with self._lock:
            target = self._accepted
            self._force = max(self._force, target)
            self._lock.notify_all()
        self._wait(target, time.monotonic() + limit)

    def shutdown(self, timeout: float | None = None) -> None:
        """Closes admission, drains once within the shutdown deadline, then stops."""
        with self._stop_once:
            first = self._stopped is None
            if first:
                self._stopped = threading.Event()
        stopped = self._stopped
        assert stopped is not None
        if not first:
            stopped.wait(self._options.shutdown_timeout)
            return
        limit = self._options.shutdown_timeout if timeout is None else min(timeout, self._options.shutdown_timeout)
        deadline = time.monotonic() + limit
        with self._lock:
            self._closed = True
            target = self._accepted
            self._force = target
            self._lock.notify_all()
        self._wait(target, deadline)
        self._worker.join(max(0.0, deadline - time.monotonic()))
        with reporting():
            self._shutdown_exporter()
        stopped.set()

    def _outstanding(self, target: int) -> bool:
        active = self._active
        if active is not None and not active.dropped and active.records[0][0] <= target:
            return True
        return len(self._records) > 0 and self._records[0][0] <= target

    def _wait(self, target: int, deadline: float) -> None:
        count = 0
        with self._lock:
            while self._outstanding(target):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    count = self._abort(target)
                    break
                self._lock.wait(remaining)
        if count > 0:
            self._report("deadline", count)

    def _abort(self, target: int) -> int:
        count = 0
        active = self._active
        if active is not None and not active.dropped and active.records[0][0] <= target:
            active.dropped = True
            count += len(active.records)
        while self._records and self._records[0][0] <= target:
            self._records.popleft()
            count += 1
        self._dropped += count
        self._pending -= count
        self._lock.notify_all()
        return count

    def _next_batch(self) -> _Batch[T] | None:
        """Called with the lock held. None when the worker should wait or stop."""
        size = self._options.batch_size
        records = self._records
        if not records:
            return None
        if not self._closed and len(records) < size and records[0][0] > self._force:
            return None
        count = min(len(records), size)
        # A partial batch never sweeps in records accepted after the flush that forced it.
        if not self._closed and records[0][0] <= self._force:
            while count > 1 and records[count - 1][0] > self._force:
                count -= 1
        batch = _Batch([records.popleft() for _ in range(count)])
        self._active = batch
        return batch

    def _run(self) -> None:
        with reporting():
            next_tick = time.monotonic() + self._options.scheduled_delay
            while True:
                with self._lock:
                    batch = self._next_batch()
                    while batch is None:
                        if self._closed and not self._records:
                            return
                        remaining = next_tick - time.monotonic()
                        if remaining <= 0:
                            self._force = self._accepted
                            next_tick = time.monotonic() + self._options.scheduled_delay
                        else:
                            self._lock.wait(remaining)
                        batch = self._next_batch()
                values = [value for _, value in batch.records]
                try:
                    succeeded = self._export(values)
                except Exception:  # noqa: BLE001 - a failed export is reported as loss, never raised
                    succeeded = False
                with self._lock:
                    count = len(batch.records)
                    reported = not batch.dropped
                    if reported:
                        self._pending -= count
                        if succeeded:
                            self._exported += count
                        else:
                            self._dropped += count
                    self._active = None
                    self._lock.notify_all()
                if reported and not succeeded:
                    self._report("export_failed", count)
