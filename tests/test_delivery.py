# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Bounded delivery: capacity, loss reporting, flush and shutdown."""

from __future__ import annotations

import gc
import os
import threading
import time
import weakref
from collections.abc import Sequence
from typing import Generic, TypeVar

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from agentrail_python import CaptureCode, MilestoneContext, QueueOptions, create_processors
from agentrail_python._queue import BATCH_BYTES, EXPORT_BYTES, ExportQueue
from support import BODY_BYTES, collector, options, recording

T = TypeVar("T")


class Gate(Generic[T]):
    """An exporter that blocks until released."""

    def __init__(self, result: bool = True) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.batches: list[list[T]] = []
        self.result = result

    def __call__(self, batch: Sequence[T]) -> bool:
        self.started.set()
        self.release.wait(10)
        self.batches.append(list(batch))
        return self.result


def queue(gate: Gate[int], codes: list[tuple[CaptureCode, int]], **limits: float) -> ExportQueue[int]:
    settings = QueueOptions(
        max_records=3,
        batch_size=2,
        scheduled_delay=3600,
        export_timeout=limits.get("export_timeout", 1),
        flush_timeout=limits.get("flush_timeout", 1),
        shutdown_timeout=1,
    )
    return ExportQueue("test", settings, gate, lambda code, count: codes.append((code, count)), lambda: None)


# Fails if someone stops counting the batch being exported against capacity,
# or drops records without reporting them.
def test_capacity_includes_the_batch_in_flight() -> None:
    gate, codes = Gate[int](), list[tuple[CaptureCode, int]]()
    q = queue(gate, codes)
    assert q.add(1) and q.add(2)
    assert gate.started.wait(5)
    assert q.add(3)
    assert not q.add(4)
    assert codes == [("queue_full", 1)]
    gate.release.set()
    q.flush()
    assert gate.batches == [[1, 2], [3]]
    stats = q.diagnostics()
    assert (stats.accepted, stats.exported, stats.dropped, stats.pending) == (3, 3, 1, 0)


# Fails if someone closes batches by count alone, lets a full batch by size wait
# for the next flush, or moves the limit off the collector's body limit.
def test_batches_close_before_the_byte_limit() -> None:
    assert EXPORT_BYTES == BODY_BYTES and BATCH_BYTES < EXPORT_BYTES
    gate, codes = Gate[int](), list[tuple[CaptureCode, int]]()
    gate.release.set()
    settings = QueueOptions(max_records=10, batch_size=5, scheduled_delay=3600, export_timeout=1, flush_timeout=1)
    q: ExportQueue[int] = ExportQueue(
        "test",
        settings,
        gate,
        lambda code, count: codes.append((code, count)),
        lambda: None,
        weigh=lambda size: (size, 0),
        batch_bytes=100,
    )
    q.add(60)
    q.add(50)
    assert gate.started.wait(5)
    q.add(30)
    q.flush()
    assert gate.batches == [[60], [50, 30]]
    assert codes == []


# Fails if someone stops counting the batch in flight against the payload
# budget, drops the whole record instead of its payloads, or does not report it.
def test_payload_budget_includes_the_batch_in_flight() -> None:
    gate, codes = Gate[tuple[str, int]](), list[tuple[CaptureCode, int]]()
    settings = QueueOptions(max_records=10, batch_size=1, scheduled_delay=3600, export_timeout=1, flush_timeout=1)
    q: ExportQueue[tuple[str, int]] = ExportQueue(
        "test",
        settings,
        gate,
        lambda code, count: codes.append((code, count)),
        lambda: None,
        weigh=lambda record: (record[1], record[1]),
        strip=lambda record: (record[0], 0),
        payload_budget=100,
    )
    assert q.add(("a", 60))
    assert gate.started.wait(5)
    assert q.add(("b", 60))
    assert codes == [("payload_budget", 1)]
    gate.release.set()
    q.flush()
    assert q.add(("c", 60))
    q.flush()
    assert [list(batch) for batch in gate.batches] == [[("a", 60)], [("b", 0)], [("c", 60)]]
    assert codes == [("payload_budget", 1)]


# Fails if the idle worker keeps the last exported batch alive (up to a
# batch of payload text held while nothing is sent).
def test_exported_batch_is_released() -> None:
    class Record:
        pass

    settings = QueueOptions(max_records=10, batch_size=5, scheduled_delay=3600, export_timeout=1, flush_timeout=1)
    q: ExportQueue[Record] = ExportQueue("test", settings, lambda batch: True, lambda code, count: None, lambda: None)
    record = Record()
    alive = weakref.ref(record)
    q.add(record)
    q.flush()
    del record
    gc.collect()
    assert alive() is None
    q.shutdown()


# Fails if someone lets flush wait past its deadline, or forgets to count and
# report what the deadline cost.
def test_flush_deadline_drops_and_reports() -> None:
    gate, codes = Gate[int](), list[tuple[CaptureCode, int]]()
    q = queue(gate, codes, flush_timeout=1.0, export_timeout=0.5)
    q.add(1)
    q.add(2)
    q.add(3)
    started = time.monotonic()
    q.flush(0.2)
    assert time.monotonic() - started < 1
    assert codes == [("deadline", 3)]
    gate.release.set()
    q.shutdown()
    stats = q.diagnostics()
    assert (stats.exported, stats.dropped, stats.pending) == (0, 3, 0)


# Fails if someone counts a rejected export as delivered, or accepts records
# after shutdown without reporting them.
def test_export_failure_and_closed() -> None:
    gate, codes = Gate[int](result=False), list[tuple[CaptureCode, int]]()
    gate.release.set()
    q = queue(gate, codes)
    q.add(1)
    q.flush()
    q.shutdown()
    assert not q.add(2)
    assert codes == [("export_failed", 1), ("closed", 1)]
    assert q.diagnostics().dropped == 2


# Fails if someone reports a collector rejection as delivered end to end.
def test_collector_rejection_is_reported() -> None:
    with collector(status=400) as state:
        errors: list[str] = []
        processors = create_processors(options(state.url, on_error=lambda error: errors.append(error.code)))
        provider = TracerProvider(sampler=processors.sampler(ALWAYS_ON))
        provider.add_span_processor(processors.span_processor)
        client = processors.bind(provider)
        client.milestone("x", MilestoneContext(surface="custom", kind="k"))
        client.flush()
        client.shutdown()
        assert errors == ["export_failed"]
        assert client.diagnostics().spans.dropped == 1


# Fails if someone makes shutdown wait without bound, or not drain what was accepted.
def test_shutdown_drains_once() -> None:
    with recording() as rec:
        rec.client.milestone("a", MilestoneContext(surface="custom", kind="k"))
        rec.client.shutdown()
        rec.client.shutdown()
        assert [span["name"] for span in rec.collector.spans()] == ["a"]
        assert rec.client.diagnostics().spans.exported == 1


# Fails if someone stops restarting delivery in a forked worker (gunicorn
# --preload), so its records are never sent, or lets the child send records
# the parent accepted before the fork.
@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.filterwarnings("ignore:.*fork.*:DeprecationWarning")
def test_forked_worker_delivers_its_own_records() -> None:
    step = MilestoneContext(surface="custom", kind="k")
    with recording() as rec:
        rec.client.milestone("before-fork", step)
        pid = os.fork()
        if pid == 0:
            code = 1
            # macOS crashes a forked child that asks the system for proxy
            # settings, as requests does; the environment answers instead.
            os.environ["NO_PROXY"] = "*"
            try:
                rec.client.milestone("in-child", step)
                rec.client.flush()
                stats = rec.client.diagnostics().spans
                code = 0 if (stats.exported, stats.pending) == (1, 0) else 2
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        names = sorted(span["name"] for span in rec.spans())
        assert names == ["before-fork", "in-child"]


# Fails if someone holds the exporters' HTTP sessions from the fork hook, so a
# shut-down SDK's sessions stay alive for the rest of the process.
def test_fork_hook_does_not_keep_sessions_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    import gc
    import weakref

    import requests

    from agentrail_python import _processors

    created: list[weakref.ref[requests.Session]] = []

    class Session(requests.Session):
        def __init__(self) -> None:
            super().__init__()
            created.append(weakref.ref(self))

    monkeypatch.setattr(_processors.requests, "Session", Session)
    with collector() as state:
        processors = create_processors(options(state.url))
        provider = TracerProvider(sampler=processors.sampler(ALWAYS_ON))
        provider.add_span_processor(processors.span_processor)
        client = processors.bind(provider)
        client.shutdown()
        provider.shutdown()
        del client, provider, processors
    gc.collect()
    assert len(created) == 2
    assert all(reference() is None for reference in created)
