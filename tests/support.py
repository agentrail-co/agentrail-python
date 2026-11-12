# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""A local stand-in collector that keeps the OTLP protobuf it receives."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from agentrail_python import CollectorOptions, Options, QueueOptions

SECRET = "synthetic-collector-secret"
HASH_KEY = "synthetic-workspace-hash-key"


def value_of(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    return None if kind is None else getattr(value, kind)


def attributes(items: Any) -> dict[str, Any]:
    return {item.key: value_of(item.value) for item in items if isinstance(item, KeyValue)}


@dataclass
class Collector:
    url: str
    packets: list[tuple[str, bytes, dict[str, str]]] = field(default_factory=lambda: [])
    status: int = 200
    lock: threading.Lock = field(default_factory=threading.Lock)

    def spans(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self.lock:
            packets = list(self.packets)
        for signal, body, _ in packets:
            if signal != "traces":
                continue
            request = ExportTraceServiceRequest.FromString(body)
            for group in request.resource_spans:
                resource = attributes(group.resource.attributes)
                for scope in group.scope_spans:
                    for span in scope.spans:
                        out.append(
                            {
                                "name": span.name,
                                "kind": span.kind,
                                "trace_id": span.trace_id.hex(),
                                "span_id": span.span_id.hex(),
                                "parent_span_id": span.parent_span_id.hex(),
                                "attributes": attributes(span.attributes),
                                "links": [
                                    (link.trace_id.hex(), link.span_id.hex(), link.trace_state) for link in span.links
                                ],
                                "events": list(span.events),
                                "status": span.status.code,
                                "trace_state": span.trace_state,
                                "resource": resource,
                                "scope": scope.scope.name,
                            }
                        )
        return out

    def logs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self.lock:
            packets = list(self.packets)
        for signal, body, _ in packets:
            if signal != "logs":
                continue
            request = ExportLogsServiceRequest.FromString(body)
            for group in request.resource_logs:
                for scope in group.scope_logs:
                    for record in scope.log_records:
                        out.append(
                            {
                                "trace_id": record.trace_id.hex(),
                                "span_id": record.span_id.hex(),
                                "severity_number": record.severity_number,
                                "severity_text": record.severity_text,
                                "event_name": record.event_name,
                                "body": value_of(record.body),
                                "attributes": attributes(record.attributes),
                            }
                        )
        return out


@contextmanager
def collector(status: int = 200) -> Iterator[Collector]:
    state = Collector(url="")
    state.status = status

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            signal = self.path.rsplit("/", 1)[-1]
            with state.lock:
                state.packets.append((signal, body, {k.lower(): v for k, v in self.headers.items()}))
            self.send_response(state.status)
            self.send_header("content-type", "application/x-protobuf")
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def options(url: str, **overrides: Any) -> Options:
    values: dict[str, Any] = {
        "collector": CollectorOptions(endpoint=url, secret=SECRET),
        "hash_key": HASH_KEY,
        "service_name": "synthetic-python-service",
        "service_instance_id": "synthetic-instance",
        "queue": QueueOptions(scheduled_delay=3600, export_timeout=2, flush_timeout=5, shutdown_timeout=5),
        "on_error": lambda error: None,
    }
    values.update(overrides)
    return Options(**values)


@dataclass
class Recording:
    client: Any
    provider: Any
    collector: Collector
    errors: list[Any]
    processors: Any

    def spans(self) -> list[dict[str, Any]]:
        self.client.flush()
        return self.collector.spans()

    def logs(self) -> list[dict[str, Any]]:
        self.client.flush()
        return self.collector.logs()

    def codes(self) -> list[str]:
        return [error.code for error in self.errors]


@contextmanager
def recording(sampler: Any = None, **overrides: Any) -> Iterator[Recording]:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    from agentrail_python import create_processors

    errors: list[Any] = []
    with collector() as state:
        overrides.setdefault("on_error", errors.append)
        processors = create_processors(options(state.url, **overrides))
        provider = TracerProvider(sampler=processors.sampler(sampler or ALWAYS_ON))
        provider.add_span_processor(processors.span_processor)
        client = processors.bind(provider)
        try:
            yield Recording(client, provider, state, errors, processors)
        finally:
            client.shutdown()
            provider.shutdown()


def by_name(spans: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [span for span in spans if span["name"] == name]
    assert len(matches) == 1, [span["name"] for span in spans]
    return matches[0]
