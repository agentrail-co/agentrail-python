# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""One request through Agentrail beside another tracer, in a process of its own.

``python tests/coexistence.py <tracer> <order> <installed>`` prints the run as
JSON for ``test_coexistence.py``. Every tracer patches libraries or sets process
globals, so each run gets a fresh interpreter.

- ``tracer``: ``opentelemetry``, ``sentry`` or ``datadog``: whose headers the
  incoming request carries and, with ``installed`` ``1``, the tracer the
  application runs. ``0`` is the same run without it.
- ``order``: ``outside`` puts that tracer's ASGI middleware outside
  Agentrail's, ``inside`` within it.

``python tests/coexistence.py init-then-provider`` is the application setting
its own OpenTelemetry globals after ``init``.

The request identifies a user, opens a milestone, logs, and calls one ordinary
and one allowed origin through httpx and through requests, with the headers
the application's tracer gives them.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN = "00f067aa0ba902b7"
DD_TRACE = "8158530521380843211"
DD_PARENT = "6153422424722463093"
INCOMING = {
    "opentelemetry": {
        "traceparent": f"00-{TRACE}-{SPAN}-01",
        "tracestate": "vendor=1",
        "baggage": "customer.key=value",
    },
    "sentry": {
        "sentry-trace": f"{TRACE}-{SPAN}-1",
        "baggage": f"sentry-trace_id={TRACE},sentry-sample_rate=1.0,sentry-sampled=true,sentry-public_key=public",
    },
    "datadog": {
        "x-datadog-trace-id": DD_TRACE,
        "x-datadog-parent-id": DD_PARENT,
        "x-datadog-sampling-priority": "1",
    },
}
ORDINARY = "https://ordinary.example"
ALLOWED = "https://allowed.example"
# What every HTTP client sends; the rest is the application's, its tracers' or Agentrail's.
_PLAIN = frozenset(("host", "user-agent", "accept", "accept-encoding", "connection", "content-length"))


def _headers(items: Any) -> dict[str, str]:
    return {name.lower(): value for name, value in items if name.lower() not in _PLAIN}


@contextmanager
def _origin() -> Iterator[tuple[str, list[dict[str, str]]]]:
    """A local origin for requests, keeping the headers of each call it receives."""
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(_headers(self.headers.items()))
            self.send_response(200)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


async def _get(app: Any, path: str, headers: dict[str, str]) -> int:
    """One GET straight into the ASGI application: an HTTP test client would be
    traced too, and the tracer would replace the incoming headers with its own."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"user-agent", b"synthetic-agent")]
        + [(name.encode(), value.encode()) for name, value in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    started: list[int] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            started.append(message["status"])

    await app(scope, receive, send)
    return started[0]


class _Tracer:
    """The application's own tracer: installed before the application is built."""

    def child(self) -> AbstractContextManager[object]:
        return nullcontext()

    def outgoing(self) -> dict[str, str]:
        return {}

    def wrap(self, app: Any, agentrail: Any, order: str) -> Any:
        return agentrail.asgi(app)

    def evidence(self) -> dict[str, Any]:
        return {}


class _OpenTelemetry(_Tracer):
    def __init__(self) -> None:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        self.exported = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exported))
        trace.set_tracer_provider(provider)
        self.tracer = trace.get_tracer("customer")

    def child(self) -> AbstractContextManager[object]:
        return self.tracer.start_as_current_span("customer.child")

    def outgoing(self) -> dict[str, str]:
        from opentelemetry import propagate

        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        return carrier

    def wrap(self, app: Any, agentrail: Any, order: str) -> Any:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

        if order == "outside":
            return OpenTelemetryMiddleware(agentrail.asgi(app))
        return agentrail.asgi(OpenTelemetryMiddleware(app))

    def evidence(self) -> dict[str, Any]:
        spans = []
        for span in self.exported.get_finished_spans():
            context = span.get_span_context()
            assert context is not None
            spans.append(
                {
                    "name": span.name,
                    "kind": span.kind.value,
                    "trace_id": f"{context.trace_id:032x}",
                    "span_id": f"{context.span_id:016x}",
                    "parent_span_id": None if span.parent is None else f"{span.parent.span_id:016x}",
                }
            )
        return {"spans": spans}


class _Sentry(_Tracer):
    def __init__(self) -> None:
        import sentry_sdk
        from sentry_sdk.transport import Transport

        events: list[dict[str, Any]] = []

        class Keep(Transport):
            def capture_envelope(self, envelope: Any) -> None:
                event = envelope.get_transaction_event()
                if event is not None:
                    events.append(event)

        self.events = events
        sentry_sdk.init(dsn="https://public@example.com/1", transport=Keep(), traces_sample_rate=1.0)

    def wrap(self, app: Any, agentrail: Any, order: str) -> Any:
        from agentrail_python.asgi import AgentrailASGIMiddleware

        if order == "outside":
            # Sentry wraps the Starlette application itself; this is inside it.
            app.add_middleware(AgentrailASGIMiddleware, client=agentrail)
            return app
        return agentrail.asgi(app)

    def evidence(self) -> dict[str, Any]:
        import sentry_sdk

        sentry_sdk.flush()
        return {
            "transactions": [
                {"name": event.get("transaction"), "trace_id": event["contexts"]["trace"]["trace_id"]}
                for event in self.events
            ]
        }


class _Datadog(_Tracer):
    def __init__(self) -> None:
        import ddtrace.auto  # noqa: F401 - what ddtrace-run does: patch the libraries before they load
        from ddtrace.trace import TraceFilter, tracer

        spans: list[dict[str, Any]] = []

        class Keep(TraceFilter):
            def process_trace(self, trace: Any) -> None:
                for span in trace:
                    spans.append(
                        {"name": span.name, "trace_id": str(span.trace_id & (2**64 - 1)), "span_id": str(span.span_id)}
                    )
                # Kept here, never sent: no agent runs.

        tracer.configure(trace_processors=[Keep()])
        self.spans = spans
        self.otel_current: list[str] = []

    def child(self) -> AbstractContextManager[object]:
        from opentelemetry import trace

        # With DD_TRACE_OTEL_ENABLED, OpenTelemetry's current span is ddtrace's.
        self.otel_current.append(f"{trace.get_current_span().get_span_context().trace_id & (2**64 - 1)}")
        return nullcontext()

    def evidence(self) -> dict[str, Any]:
        from opentelemetry import trace

        return {
            "spans": self.spans,
            "otel_provider": type(trace.get_tracer_provider()).__module__,
            "otel_current": self.otel_current,
        }


def _url(span: dict[str, Any], origins: dict[str, str]) -> str:
    url = str(span["attributes"].get("url.full", ""))
    for origin, name in origins.items():
        url = url.replace(origin, name)
    return url


def _label(span: dict[str, Any], origins: dict[str, str]) -> str:
    return f"{span['name']} kind={span['kind']} {_url(span, origins)}"


def _ours(collector: Any, origins: dict[str, str]) -> dict[str, Any]:
    spans = collector.spans()
    by_id = {span["span_id"]: span for span in spans}
    out = []
    for span in spans:
        parent = by_id.get(span["parent_span_id"])
        attributes = dict(span["attributes"])
        if "url.full" in attributes:
            attributes["url.full"] = _url(span, origins)
        out.append(
            {
                "span": _label(span, origins),
                "parent": None if parent is None else _label(parent, origins),
                "links": [list(link[:2]) for link in span["links"]],
                "attributes": attributes,
                "status": span["status"],
            }
        )
    logs = []
    for log in collector.logs():
        owner = by_id.get(log["span_id"]) if log["span_id"] else None
        logs.append({"event": log["event_name"], "span": None if owner is None else _label(owner, origins)})
    return {
        "spans": sorted(out, key=lambda span: span["span"]),
        "traces": len({span["trace_id"] for span in spans}),
        "logs": logs,
    }


def run(name: str, order: str, installed: bool) -> dict[str, Any]:
    tracer = (
        {"opentelemetry": _OpenTelemetry, "sentry": _Sentry, "datadog": _Datadog}[name]() if installed else _Tracer()
    )
    import httpx
    import requests
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from agentrail_python import LogsOptions, MilestoneContext, StructuredLog, VerifiedIdentity
    from agentrail_python._processors import build
    from agentrail_python.asgi import AgentrailASGIMiddleware
    from agentrail_python.httpx import transport
    from agentrail_python.requests import adapter
    from support import collector, options

    sent: dict[str, list[dict[str, str]]] = {}

    def network(request: httpx.Request) -> httpx.Response:
        sent.setdefault(f"{request.url.scheme}://{request.url.host}", []).append(_headers(request.headers.items()))
        return httpx.Response(200)

    errors: list[str] = []
    with collector() as state, _origin() as (ordinary, ordinary_seen), _origin() as (allowed, allowed_seen):
        agentrail = build(
            options(
                state.url,
                outbound_origins=(ALLOWED, allowed),
                logs=LogsOptions(include_fields=()),
                on_error=lambda error: errors.append(error.code),
            )
        )
        web = httpx.Client(transport=transport(agentrail, httpx.MockTransport(network)))
        session = requests.Session()
        session.mount("http://", adapter(agentrail))

        async def checkout(request: Request) -> PlainTextResponse:
            agentrail.identify(VerifiedIdentity(kind="user", id="synthetic-user"))
            with agentrail.start_milestone("checkout.confirm", MilestoneContext(surface="custom", kind="checkout")):
                agentrail.log(StructuredLog(event_name="checkout.logged"))
                with tracer.child():
                    headers = tracer.outgoing()
                    web.get(f"{ORDINARY}/stock", headers=headers)
                    web.get(f"{ALLOWED}/stock", headers=headers)
                    session.get(f"{ordinary}/stock", headers=headers)
                    session.get(f"{allowed}/stock", headers=headers)
            return PlainTextResponse("ok")

        routes = [Route("/checkout", checkout)]
        if name == "datadog" and order == "outside":
            # In the application's middleware list: ddtrace puts its own before it.
            app: Any = Starlette(routes=routes, middleware=[Middleware(AgentrailASGIMiddleware, client=agentrail)])
        else:
            app = tracer.wrap(Starlette(routes=routes), agentrail, order)
        status = asyncio.run(_get(app, "/checkout", INCOMING[name]))
        assert status == 200, status
        agentrail.flush()
        origins = {ALLOWED: "httpx-allowed", ORDINARY: "httpx-ordinary", allowed: "requests-allowed"}
        origins[ordinary] = "requests-ordinary"
        result = {
            "ours": _ours(state, origins),
            "outbound": {
                "httpx-ordinary": sent[ORDINARY],
                "httpx-allowed": sent[ALLOWED],
                "requests-ordinary": ordinary_seen,
                "requests-allowed": allowed_seen,
            },
            "theirs": tracer.evidence(),
            "errors": errors,
        }
        agentrail.shutdown()
    return result


def init_then_provider() -> dict[str, Any]:
    """``init`` first, then the application's own OpenTelemetry globals."""
    from opentelemetry import _logs as otel_logs
    from opentelemetry import propagate, trace
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import agentrail_python as ar
    from support import collector, options

    with collector() as state:
        agentrail = ar.init(options(state.url))
        fields = sorted(propagate.get_global_textmap().fields)
        theirs, logger = TracerProvider(), LoggerProvider()
        exported = InMemorySpanExporter()
        theirs.add_span_processor(SimpleSpanProcessor(exported))
        trace.set_tracer_provider(theirs)
        otel_logs.set_logger_provider(logger)
        with trace.get_tracer("customer").start_as_current_span("customer.work"):
            agentrail.milestone("standalone", ar.MilestoneContext(surface="custom", kind="k"))
        agentrail.shutdown()
        return {
            "propagator_fields": fields,
            "tracer_provider_is_theirs": trace.get_tracer_provider() is theirs,
            "logger_provider_is_theirs": otel_logs.get_logger_provider() is logger,
            "their_spans": [span.name for span in exported.get_finished_spans()],
            "our_spans": [span["name"] for span in state.spans()],
        }


if __name__ == "__main__":
    if sys.argv[1] == "init-then-provider":
        print(json.dumps(init_then_provider()))
    else:
        print(json.dumps(run(sys.argv[1], sys.argv[2], sys.argv[3] == "1")))
