# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Agentrail's own tracer provider, export queues and delivery to its collector.

Nothing here is registered globally: the application's OpenTelemetry setup,
and any other tracer it runs, never sees Agentrail's spans, and Agentrail never
sees theirs. Nothing is read from OpenTelemetry's environment variables either:
``OTEL_*`` configure the application's own SDK and exporters.
"""

from __future__ import annotations

import math
import os
import random
import threading
import time
import weakref
from datetime import timezone
from email.utils import parsedate_to_datetime

import requests
from opentelemetry import context as otel_context
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk.trace import ReadableSpan, SpanLimits, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from ._client import Client
from ._options import Options, resolve
from ._privacy import copy_span, log_weight, span_weight, without_payloads
from ._queue import ExportQueue
from ._runtime import Runtime, SpanMeta, current
from ._suppression import suppressed

# The SDK's defaults, stated, so OTEL_SPAN_*_LIMIT and OTEL_ATTRIBUTE_*_LIMIT
# (the application's) never cut Agentrail's spans: no length limit.
_LIMITS = SpanLimits(
    max_attributes=128,
    max_events=128,
    max_links=128,
    max_span_attributes=128,
    max_event_attributes=128,
    max_link_attributes=128,
    max_attribute_length=SpanLimits.UNSET,
    max_span_attribute_length=SpanLimits.UNSET,
)
# What opentelemetry-exporter-otlp-proto-http 1.45 retries, and how.
_ATTEMPTS = 6
_JITTER = 0.2
_RETRYABLE = frozenset((429, 502, 503, 504))
_UNREACHABLE = (requests.ConnectionError, requests.Timeout)


def _retry_after(value: str | None) -> float | None:
    """The seconds a Retry-After header asks for: delay-seconds or an HTTP-date."""
    if value is None:
        return None
    value = value.strip()
    try:
        seconds: float | None = float(value)
    except ValueError:
        seconds = None
    if seconds is not None:
        return max(seconds, 0.0) if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(when.timestamp() - time.time(), 0.0)


class _Collector:
    """One signal's path on Agentrail's collector, configured by Agentrail alone.

    OpenTelemetry's OTLP exporters also take the application's
    ``OTEL_EXPORTER_OTLP_*`` settings: its headers (a vendor's key would reach
    Agentrail), compression, certificates and timeout. This sends the batch
    OpenTelemetry's encoder builds with Agentrail's headers only, uncompressed,
    over requests' own TLS.

    It retries as that exporter does (1.45): six attempts at most, all within the
    export timeout, on 429, 502, 503 and 504 and when the collector cannot be
    reached (once at once, then with backoff), waiting 2**n seconds within 20%
    or what Retry-After asks, never past the deadline, and not once shut down.
    urllib3's Retry does not fit: it has no overall deadline and its waits
    cannot be cut short at shutdown, and the queue's flush and shutdown bounds
    rely on both.
    """

    def __init__(self, url: str, secret: str, timeout: float, session: requests.Session) -> None:
        self._url = url
        self._headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/x-protobuf"}
        self._timeout = timeout
        self._session = session
        self._stopped = threading.Event()

    def send(self, body: bytes) -> bool:
        deadline = time.monotonic() + self._timeout
        for attempt in range(_ATTEMPTS):
            wait = 2**attempt * random.uniform(1 - _JITTER, 1 + _JITTER)
            try:
                response = self._post(body, deadline)
            except _UNREACHABLE:
                retryable = True
            else:
                if 200 <= response.status_code < 400:
                    return True
                retryable = response.status_code in _RETRYABLE
                asked = _retry_after(response.headers.get("retry-after")) if retryable else None
                if asked is not None:
                    wait = asked
            if not retryable or attempt + 1 == _ATTEMPTS or wait >= deadline - time.monotonic():
                return False
            if self._stopped.wait(wait):
                return False
        return False

    def _post(self, body: bytes, deadline: float) -> requests.Response:
        try:
            return self._attempt(body, deadline)
        except _UNREACHABLE:
            # At once, once: usually a pooled connection the collector closed.
            return self._attempt(body, deadline)

    def _attempt(self, body: bytes, deadline: float) -> requests.Response:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout("The export timeout passed before the collector answered.")
        return self._session.post(self._url, data=body, headers=self._headers, timeout=remaining, allow_redirects=False)

    def shutdown(self) -> None:
        self._stopped.set()
        self._session.close()


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


def build(options: Options) -> Client:
    """A client with its own tracer provider and export queues. Registers nothing globally."""
    resolved = resolve(options)
    runtime = Runtime(resolved)
    secret = options.collector.secret
    timeout = resolved.queue.export_timeout
    span_session, log_session = requests.Session(), requests.Session()
    traces = _Collector(f"{resolved.endpoint}/v1/traces", secret, timeout, span_session)
    logs_collector = _Collector(f"{resolved.endpoint}/v1/logs", secret, timeout, log_session)
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
        lambda batch: traces.send(encode_spans(batch).SerializePartialToString()),
        lambda code, count: runtime.report(code, "traces", count),
        traces.shutdown,
        weigh=span_weight,
        strip=without_payloads,
    )
    logs: ExportQueue[ReadableLogRecord] = ExportQueue(
        "logs",
        resolved.queue,
        lambda batch: logs_collector.send(encode_logs(batch).SerializePartialToString()),
        lambda code, count: runtime.report(code, "logs", count),
        logs_collector.shutdown,
        weigh=log_weight,
    )
    # Every span this provider starts is Agentrail's own, so all are sampled;
    # the client's shutdown (``init`` runs it at exit) ends the provider too.
    # OTEL_SDK_DISABLED, read by the SDK's provider itself, still turns it off:
    # its tracer records nothing, reported as entry_not_recording.
    provider = TracerProvider(sampler=ALWAYS_ON, resource=runtime.resource, span_limits=_LIMITS, shutdown_on_exit=False)
    provider.add_span_processor(_SpanProcessor(runtime, spans))
    client = Client(runtime, provider, spans, logs)
    client._on_shutdown.append(provider.shutdown)  # pyright: ignore[reportPrivateUsage]
    return client
