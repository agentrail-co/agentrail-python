# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""An existing OpenTelemetry setup keeps its sampling, exporters and propagation."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from opentelemetry import context as otel_context
from opentelemetry.propagators.textmap import DefaultGetter, DefaultSetter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from agentrail_python import ConfigurationError, MilestoneContext, QueueOptions
from support import by_name, options, recording

STEP = MilestoneContext(surface="custom", kind="k")
REMOTE = {"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"}


# Fails if someone stops promoting managed spans the customer's sampler drops,
# or flips the customer's sampled flag.
def test_dropped_managed_spans_stay_record_only() -> None:
    with recording(sampler=ALWAYS_OFF) as rec:
        with rec.client.start_milestone("kept", STEP):
            with rec.provider.get_tracer("db").start_as_current_span("child") as child:
                assert child.is_recording()
                assert not child.get_span_context().trace_flags.sampled
        with rec.provider.get_tracer("db").start_as_current_span("unmanaged") as unmanaged:
            assert not unmanaged.is_recording()
        assert {span["name"] for span in rec.spans()} == {"kept", "backend.operation"}
        assert rec.codes() == []


# Fails if someone promotes a dropped managed span without the attributes it was
# started with: an outgoing HTTP call would lose its method and address.
def test_dropped_http_spans_keep_their_attributes() -> None:
    with recording(sampler=ALWAYS_OFF) as rec:
        with rec.client.start_milestone("kept", STEP):
            tracer = rec.provider.get_tracer("http")
            with tracer.start_as_current_span(
                "GET", attributes={"http.request.method": "GET", "url.full": "https://inventory.example/items"}
            ):
                pass
        call = by_name(rec.spans(), "HTTP GET")
        assert call["attributes"]["url.full"] == "https://inventory.example/items"


# Fails if someone lets the customer's propagator continue managed work from an
# unsigned header or inject into it, or breaks propagation for unmanaged work.
def test_propagator_delegates_only_for_unmanaged_work() -> None:
    with recording(propagator=TraceContextTextMapPropagator()) as rec:
        propagator = rec.processors.propagator
        unmanaged = propagator.extract(REMOTE, otel_context.Context(), DefaultGetter())
        assert unmanaged != otel_context.Context()
        carrier: dict[str, str] = {}
        with rec.client.start_milestone("managed", STEP):
            current = otel_context.get_current()
            assert propagator.extract(REMOTE, current, DefaultGetter()) is current
            propagator.inject(carrier, current, DefaultSetter())
        assert carrier == {}
        assert "x-agentrail-trace-handoff" in propagator.fields


# Fails if someone lets a library's own server span inside a request become a
# second public entry (the MCP SDK opens one per message).
def test_nested_library_server_span_is_supporting() -> None:
    from opentelemetry.trace import SpanKind
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    with recording() as rec:

        async def endpoint(request: Request) -> PlainTextResponse:
            with rec.provider.get_tracer("mcp-python-sdk").start_as_current_span("tools/call x", kind=SpanKind.SERVER):
                pass
            return PlainTextResponse("ok")

        TestClient(rec.client.asgi(Starlette(routes=[Route("/", endpoint)]))).get("/")
        spans = rec.spans()
        assert by_name(spans, "HTTP GET")["kind"] == 2
        nested = by_name(spans, "backend.operation")
        assert nested["kind"] == 1
        assert "agentrail.ingress.verified" not in nested["attributes"]


# Fails if someone binds one set of processors to two providers.
def test_bind_once() -> None:
    with recording() as rec, pytest.raises(ConfigurationError):
        rec.processors.bind(rec.provider)


@pytest.mark.parametrize(
    "change",
    [
        {"collector_endpoint": "ftp://collector.test"},
        {"collector_endpoint": "https://user:pw@collector.test"},
        {"collector_endpoint": "https://collector.test/otlp?x=1"},
        {"hash_key": ""},
        {"service_name": "bad\x00"},
        {"outbound_origins": ("https://inventory.internal/",)},
        {"outbound_origins": ("https://Inventory.internal",)},
        {"outbound_origins": ("https://inventory.internal:443",)},
        {"queue": QueueOptions(batch_size=10, max_records=5)},
        {"queue": QueueOptions(export_timeout=20)},
    ],
)
# Fails if someone accepts a configuration that would leak credentials, sign
# for an inexact origin, or deadlock delivery.
def test_invalid_options(change: dict[str, object]) -> None:
    from agentrail_python import CollectorOptions, create_processors

    endpoint = change.pop("collector_endpoint", "https://collector.test/ingest/otlp")
    with pytest.raises(ConfigurationError):
        create_processors(options("x", collector=CollectorOptions(endpoint=str(endpoint), secret="s"), **change))


INIT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {tests!r})
    from opentelemetry import trace, propagate
    from support import collector, options
    import agentrail_python as ar
    with collector() as state:
        settings = options(state.url)
        client = ar.init(settings)
        assert ar.init(settings) is client
        try:
            ar.init(options(state.url))
        except ar.ConfigurationError:
            pass
        else:
            raise SystemExit("second init accepted")
        with trace.get_tracer("app").start_as_current_span("unmanaged"):
            carrier = {{}}
            propagate.inject(carrier)
            assert "traceparent" in carrier, carrier
        client.milestone("standalone", ar.MilestoneContext(surface="custom", kind="k"))
        client.shutdown()
        assert [span["name"] for span in state.spans()] == ["standalone"], state.spans()
    print("ok")
    """
)


# Fails if someone lets init replace an existing provider, accepts a second
# configuration, or drops the process's propagation for unmanaged work.
def test_init_in_a_fresh_process() -> None:
    tests = str(Path(__file__).parent)
    result = subprocess.run(  # noqa: PLW1510 - the exit code is asserted
        [sys.executable, "-c", INIT.format(tests=tests)],
        capture_output=True,
        text=True,
        timeout=60,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    existing = (
        "from opentelemetry import trace; from opentelemetry.sdk.trace import TracerProvider;"
        "trace.set_tracer_provider(TracerProvider());"
        f"import sys; sys.path.insert(0, {tests!r}); from support import options; import agentrail_python as ar\n"
        "try:\n    ar.init(options('http://127.0.0.1:9'))\nexcept ar.ConfigurationError:\n    print('refused')"
    )
    refused = subprocess.run([sys.executable, "-c", existing], check=False, capture_output=True, text=True, timeout=60)
    assert refused.stdout.strip() == "refused", refused.stderr
