# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Bounded, best-effort batch delivery on one background thread per signal.

Capacity counts the batch being exported. One batch is exported at a time; the
OTLP exporter owns retries within its timeout. Records only ever wait in
memory: there is no disk persistence.

A batch closes at ``batch_size`` records or before its estimated encoded size
would pass ``BATCH_BYTES``, under the collector's 16 MiB body limit. MCP
payload text is held to ``PAYLOAD_BUDGET`` per queue, the batch in flight
included; past it a record keeps everything but its payloads.
"""

from __future__ import annotations

import os
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from ._options import CaptureCode, QueueOptions
from ._suppression import reporting

T = TypeVar("T")

EXPORT_BYTES = 16 * 1024 * 1024
"""The collector's request body limit (the contract's ``otlp.limits.bodyBytes``)."""
BATCH_BYTES = 15 * 1024 * 1024
"""Where a batch closes (the contract's ``otlp.limits.exportBatchBytes``): headroom
for the resource, framing and estimate error."""
PAYLOAD_BUDGET = 64 * 1024 * 1024
"""Payload text one queue may hold, the batch in flight included."""


def _unweighted(_value: object) -> tuple[int, int]:
    return 0, 0


@dataclass(frozen=True)
class _Record(Generic[T]):
    sequence: int
    value: T
    size: int
    payload: int


@dataclass(frozen=True)
class QueueStats:
    accepted: int
    exported: int
    dropped: int
    pending: int


class _Batch(Generic[T]):
    def __init__(self, records: list[_Record[T]]) -> None:
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
        *,
        weigh: Callable[[T], tuple[int, int]] = _unweighted,
        strip: Callable[[T], T] | None = None,
        batch_bytes: int = BATCH_BYTES,
        payload_budget: int = PAYLOAD_BUDGET,
    ) -> None:
        """``weigh`` gives a record's estimated encoded size and its payload bytes;
        ``strip`` returns it without its payloads."""
        self._options = options
        self._export = export
        self._report = report
        self._shutdown_exporter = shutdown_exporter
        self._weigh = weigh
        self._strip = strip
        self._batch_bytes = batch_bytes
        self._payload_budget = payload_budget
        self._lock = threading.Condition()
        self._records: deque[_Record[T]] = deque()
        self._active: _Batch[T] | None = None
        self._queued_bytes = 0
        self._payload = 0
        self._accepted = 0
        self._exported = 0
        self._dropped = 0
        self._pending = 0
        self._force = 0
        self._closed = False
        self._stop_once = threading.Lock()
        self._stopped: threading.Event | None = None
        self._name = name
        self._worker = threading.Thread(target=self._run, name=f"agentrail-{name}", daemon=True)
        self._worker.start()
        # A process that forks after start-up (gunicorn --preload, multiprocessing)
        # gives the child this queue without its worker thread, and possibly with
        # a lock held by a thread that no longer exists. The child starts afresh.
        if hasattr(os, "register_at_fork"):
            queue = weakref.ref(self)

            def reinitialize() -> None:
                alive = queue()
                if alive is not None:
                    alive._after_fork()  # pyright: ignore[reportPrivateUsage]

            os.register_at_fork(after_in_child=reinitialize)

    def _after_fork(self) -> None:
        """In a forked child: the parent's records stay the parent's to send."""
        self._lock = threading.Condition()
        self._records = deque()
        self._active = None
        self._queued_bytes = 0
        self._payload = 0
        self._accepted = 0
        self._exported = 0
        self._dropped = 0
        self._pending = 0
        self._force = 0
        self._stop_once = threading.Lock()
        if self._closed:
            return
        self._stopped = None
        self._worker = threading.Thread(target=self._run, name=f"agentrail-{self._name}", daemon=True)
        self._worker.start()

    def diagnostics(self) -> QueueStats:
        with self._lock:
            return QueueStats(self._accepted, self._exported, self._dropped, self._pending)

    def add(self, value: T) -> bool:
        """Never waits for an export. False means the record was dropped."""
        size, payload = self._weigh(value)
        stripped = False
        with self._lock:
            retained = len(self._records) + (0 if self._active is None else len(self._active.records))
            code: CaptureCode | None = None
            if self._closed:
                code = "closed"
            elif retained >= self._options.max_records:
                code = "queue_full"
            if code is None:
                if payload > 0 and self._payload + payload > self._payload_budget and self._strip is not None:
                    value = self._strip(value)
                    size, payload = self._weigh(value)
                    stripped = True
                self._accepted += 1
                self._pending += 1
                self._payload += payload
                self._queued_bytes += size
                self._records.append(_Record(self._accepted, value, size, payload))
                if len(self._records) >= self._options.batch_size or self._queued_bytes >= self._batch_bytes:
                    self._lock.notify_all()
            else:
                self._dropped += 1
        if code is not None:
            self._report(code, 1)
            return False
        if stripped:
            self._report("payload_budget", 1)
        return True

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
        if active is not None and not active.dropped and active.records[0].sequence <= target:
            return True
        return len(self._records) > 0 and self._records[0].sequence <= target

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
        if active is not None and not active.dropped and active.records[0].sequence <= target:
            active.dropped = True
            count += len(active.records)
        while self._records and self._records[0].sequence <= target:
            record = self._records.popleft()
            self._queued_bytes -= record.size
            self._payload -= record.payload
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
        full = len(records) >= size or self._queued_bytes >= self._batch_bytes
        if not self._closed and not full and records[0].sequence > self._force:
            return None
        count = 0
        total = 0
        # Closes before the estimate would pass the limit; a record is never split.
        while count < min(len(records), size) and (count == 0 or total + records[count].size <= self._batch_bytes):
            total += records[count].size
            count += 1
        # A partial batch never sweeps in records accepted after the flush that forced it.
        if not self._closed and records[0].sequence <= self._force:
            while count > 1 and records[count - 1].sequence > self._force:
                count -= 1
        batch = _Batch([records.popleft() for _ in range(count)])
        self._queued_bytes -= sum(record.size for record in batch.records)
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
                values = [record.value for record in batch.records]
                try:
                    succeeded = self._export(values)
                except Exception:  # noqa: BLE001 - a failed export is reported as loss, never raised
                    succeeded = False
                with self._lock:
                    count = len(batch.records)
                    self._payload -= sum(record.payload for record in batch.records)
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
