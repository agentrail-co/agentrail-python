# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""OpenTelemetry pieces a customer's own providers are built with."""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from typing import cast

import requests
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)
from opentelemetry.sdk._logs import LogRecordProcessor, ReadableLogRecord, ReadWriteLogRecord
from opentelemetry.sdk._logs.export import LogRecordExportResult
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import Link, SpanKind, TraceState
from opentelemetry.util.types import Attributes

from ._client import Client
from ._handoff import OWNED_HEADERS
from ._options import ConfigurationError, Options, resolve
from ._privacy import StructuredLog, copy_log, copy_span
from ._queue import ExportQueue
from ._runtime import Runtime, SpanMeta
from ._suppression import suppressed


class _SpanProcessor(SpanProcessor):
    def __init__(self, runtime: Runtime, queue: ExportQueue[ReadableSpan]) -> None:
        self._runtime = runtime
        self._queue = queue

    def on_start(self, span: SdkSpan, parent_context: otel_context.Context | None = None) -> None:
        if suppressed():
            return
        ctx = otel_context.get_current() if parent_context is None else parent_context
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


class _LogProcessor(LogRecordProcessor):
    """Copies an OTel log record's allowlisted fields; the record itself is untouched."""

    def __init__(self, runtime: Runtime, queue: ExportQueue[ReadableLogRecord]) -> None:
        self._runtime = runtime
        self._queue = queue

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        if suppressed():
            return
        if self._runtime.options.input.logs.exclude_uncorrelated and self._runtime.state() is None:
            return
        record = log_record.log_record
        fields: dict[str, object] = {}
        if isinstance(record.body, str):
            fields["message"] = record.body
        error_type: str | None = None
        event_name = record.event_name
        for key, value in (record.attributes or {}).items():
            fields[key] = value
            if key in ("error.type", "exception.type") and isinstance(value, str):
                error_type = value
            if key == "event.name" and isinstance(value, str):
                event_name = value
        span = None
        if record.trace_id and record.span_id:
            span = trace.SpanContext(record.trace_id, record.span_id, False, trace.TraceFlags(record.trace_flags or 0))
        now = time.time_ns()
        entry = StructuredLog(
            severity_number=None if record.severity_number is None else record.severity_number.value,
            severity_text=record.severity_text,
            error_type=error_type,
            event_name=event_name,
            fields=fields,
        )
        self._queue.add(copy_log(entry, record.timestamp or now, record.observed_timestamp or now, span, self._runtime))

    def shutdown(self) -> None:
        self._queue.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self._queue.flush(timeout_millis / 1000)
        return True


class _Sampler(Sampler):
    """Promotes a dropped span inside a managed operation to record-only."""

    def __init__(self, runtime: Runtime, base: Sampler) -> None:
        self._runtime = runtime
        self._base = base

    def should_sample(
        self,
        parent_context: otel_context.Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        result = self._base.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)
        ctx = otel_context.get_current() if parent_context is None else parent_context
        if result.decision == Decision.DROP and not suppressed() and self._runtime.state(ctx) is not None:
            # The SDK starts a span with the sampler's attributes only, and a DROP
            # carries none: keep the ones the span was started with.
            returned = cast(Attributes, result.attributes)  # pyright: ignore[reportUnknownMemberType]
            merged = {**(attributes or {}), **(returned or {})}
            return SamplingResult(Decision.RECORD_ONLY, merged, result.trace_state)
        return result

    def get_description(self) -> str:
        return f"Agentrail({self._base.get_description()})"


class _Propagator(TextMapPropagator):
    """Managed work is never continued from, or injected with, an unsigned header.

    Unmanaged work goes to the customer's propagator.
    """

    def __init__(self, runtime: Runtime, base: TextMapPropagator | None) -> None:
        self._runtime = runtime
        self._base = base

    def extract(
        self,
        carrier: CarrierT,
        context: otel_context.Context | None = None,
        getter: Getter[CarrierT] = default_getter,  # type: ignore[assignment]
    ) -> otel_context.Context:
        ctx = otel_context.get_current() if context is None else context
        if self._runtime.state(ctx) is not None or self._base is None:
            return ctx
        return self._base.extract(carrier, context, getter)

    def inject(
        self,
        carrier: CarrierT,
        context: otel_context.Context | None = None,
        setter: Setter[CarrierT] = default_setter,  # type: ignore[assignment]
    ) -> None:
        ctx = otel_context.get_current() if context is None else context
        if self._runtime.state(ctx) is not None or suppressed() or self._base is None:
            return
        self._base.inject(carrier, context, setter)

    @property
    def fields(self) -> set[str]:
        return set(OWNED_HEADERS) | (set() if self._base is None else set(self._base.fields))


def _export_spans(exporter: OTLPSpanExporter, spans: Sequence[ReadableSpan]) -> bool:
    return exporter.export(spans) == SpanExportResult.SUCCESS


def _export_logs(exporter: OTLPLogExporter, records: Sequence[ReadableLogRecord]) -> bool:
    return exporter.export(records) == LogRecordExportResult.SUCCESS


class Processors:
    """Construct before the customer's tracer provider; then ``bind`` that provider once.

    Registers nothing globally.
    """

    def __init__(self, options: Options) -> None:
        resolved = resolve(options)
        self._runtime = runtime = Runtime(resolved)
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
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=lambda: (span_session.close(), log_session.close()))
        self.spans: ExportQueue[ReadableSpan] = ExportQueue(
            "traces",
            resolved.queue,
            lambda batch: _export_spans(span_exporter, batch),
            lambda code, count: runtime.report(code, "traces", count),
            span_exporter.shutdown,
        )
        self.logs: ExportQueue[ReadableLogRecord] = ExportQueue(
            "logs",
            resolved.queue,
            lambda batch: _export_logs(log_exporter, batch),
            lambda code, count: runtime.report(code, "logs", count),
            log_exporter.shutdown,
        )
        self.span_processor: SpanProcessor = _SpanProcessor(runtime, self.spans)
        self.log_processor: LogRecordProcessor = _LogProcessor(runtime, self.logs)
        self.propagator: TextMapPropagator = _Propagator(runtime, options.propagator)
        self._bound = False

    def sampler(self, base: Sampler) -> Sampler:
        """Wrap the customer's sampler so managed spans it drops stay record-only."""
        return _Sampler(self._runtime, base)

    def bind(self, provider: trace.TracerProvider) -> Client:
        """The client that records with ``provider``. Call once."""
        if self._bound:
            raise ConfigurationError("These processors are already bound to a tracer provider.")
        self._bound = True
        return Client(self._runtime, provider, self.spans, self.logs)


def create_processors(options: Options) -> Processors:
    """Agentrail's sampler wrapper, span and log processors and propagator for an existing setup."""
    return Processors(options)
