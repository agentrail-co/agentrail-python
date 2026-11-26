# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Agentrail's own tracer provider, export queues and exporters.

Nothing here is registered globally: the application's OpenTelemetry setup,
and any other tracer it runs, never sees Agentrail's spans, and Agentrail never
sees theirs.
"""

from __future__ import annotations

import os
import weakref
from collections.abc import Sequence

import requests
from opentelemetry import context as otel_context
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExportResult
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from ._client import Client
from ._options import Options, resolve
from ._privacy import copy_span, log_weight, span_weight, without_payloads
from ._queue import ExportQueue
from ._runtime import Runtime, SpanMeta, current
from ._suppression import suppressed


class _SpanProcessor(SpanProcessor):
    def __init__(self, runtime: Runtime, queue: ExportQueue[ReadableSpan]) -> None:
        self._runtime = runtime
        self._queue = queue

    def on_start(self, span: SdkSpan, parent_context: otel_context.Context | None = None) -> None:
        if suppressed():
            return
        ctx = current() if parent_context is None else parent_context
        state = self._runtime.state(ctx)
        if state is None:
            return
        context = span.get_span_context()
        self._runtime.remember(context.trace_id, context.span_id, SpanMeta(state, self._runtime.mark(ctx)))

    def on_end(self, span: ReadableSpan) -> None:
        context = span.get_span_context()
        if context is None:
            return
        meta = self._runtime.forget(context.trace_id, context.span_id)
        if meta is not None:
            self._queue.add(copy_span(span, meta, self._runtime))

    def shutdown(self) -> None:
        self._queue.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self._queue.flush(timeout_millis / 1000)
        return True


def _export_spans(exporter: OTLPSpanExporter, spans: Sequence[ReadableSpan]) -> bool:
    return exporter.export(spans) == SpanExportResult.SUCCESS


def _export_logs(exporter: OTLPLogExporter, records: Sequence[ReadableLogRecord]) -> bool:
    return exporter.export(records) == LogRecordExportResult.SUCCESS


def build(options: Options) -> Client:
    """A client with its own tracer provider and export queues. Registers nothing globally."""
    resolved = resolve(options)
    runtime = Runtime(resolved)
    headers = {"Authorization": f"Bearer {options.collector.secret}"}
    timeout = resolved.queue.export_timeout
    span_session, log_session = requests.Session(), requests.Session()
    span_exporter = OTLPSpanExporter(
        endpoint=f"{resolved.endpoint}/v1/traces", headers=headers, timeout=timeout, session=span_session
    )
    log_exporter = OTLPLogExporter(
        endpoint=f"{resolved.endpoint}/v1/logs", headers=headers, timeout=timeout, session=log_session
    )
    # A forked child must not write to a connection the parent still uses:
    # it drops the pooled connections it inherited and opens its own.
    # Weak references: a shut-down SDK's sessions are not kept alive by the hook.
    if hasattr(os, "register_at_fork"):
        sessions = (weakref.ref(span_session), weakref.ref(log_session))

        def drop_inherited_connections() -> None:
            for reference in sessions:
                session = reference()
                if session is not None:
                    session.close()

        os.register_at_fork(after_in_child=drop_inherited_connections)
    spans: ExportQueue[ReadableSpan] = ExportQueue(
        "traces",
        resolved.queue,
        lambda batch: _export_spans(span_exporter, batch),
        lambda code, count: runtime.report(code, "traces", count),
        span_exporter.shutdown,
        weigh=span_weight,
        strip=without_payloads,
    )
    logs: ExportQueue[ReadableLogRecord] = ExportQueue(
        "logs",
        resolved.queue,
        lambda batch: _export_logs(log_exporter, batch),
        lambda code, count: runtime.report(code, "logs", count),
        log_exporter.shutdown,
        weigh=log_weight,
    )
    # Every span this provider starts is Agentrail's own, so all are sampled;
    # the client's shutdown (``init`` runs it at exit) ends the provider too.
    provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
    provider.add_span_processor(_SpanProcessor(runtime, spans))
    client = Client(runtime, provider, spans, logs)
    client._on_shutdown.append(provider.shutdown)  # pyright: ignore[reportPrivateUsage]
    return client
