# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Bounded delivery: capacity, loss reporting, flush and shutdown."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from agentrail_python import CaptureCode, MilestoneContext, QueueOptions, create_processors
from agentrail_python._queue import ExportQueue
from support import collector, options, recording


class Gate:
    """An exporter that blocks until released."""

    def __init__(self, result: bool = True) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.batches: list[list[int]] = []
        self.result = result

    def __call__(self, batch: Sequence[int]) -> bool:
        self.started.set()
        self.release.wait(10)
        self.batches.append(list(batch))
        return self.result


def queue(gate: Gate, codes: list[tuple[CaptureCode, int]], **limits: float) -> ExportQueue[int]:
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
    gate, codes = Gate(), list[tuple[CaptureCode, int]]()
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


# Fails if someone lets flush wait past its deadline, or forgets to count and
# report what the deadline cost.
def test_flush_deadline_drops_and_reports() -> None:
    gate, codes = Gate(), list[tuple[CaptureCode, int]]()
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
    gate, codes = Gate(result=False), list[tuple[CaptureCode, int]]()
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
