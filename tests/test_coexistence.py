# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Agentrail beside Sentry, Datadog and the application's own OpenTelemetry.

Each run is a fresh interpreter (``coexistence.py``): those tracers patch
libraries and set process globals. A request carries the other tracer's
headers and calls one ordinary and one allowed origin through httpx and
requests. Each test checks that the other tracer continued its trace through
Agentrail, that the ordinary origins got its headers and none of Agentrail's
while the allowed ones got the signed handoff, and that the collector received
what the same run sends without the other tracer.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentrail_python._handoff import HANDOFF_HEADER, verify
from coexistence import DD_TRACE, META_SPAN, META_TRACE, SPAN, TRACE
from support import HASH_KEY, SECRET

SCRIPT = Path(__file__).parent / "coexistence.py"
CLIENTS = ("httpx", "requests")
# No agent runs: ddtrace keeps its spans in the run, and starts nothing else.
DATADOG = {
    "DD_TRACE_AGENT_URL": "http://127.0.0.1:9",
    "DD_INSTRUMENTATION_TELEMETRY_ENABLED": "false",
    "DD_REMOTE_CONFIGURATION_ENABLED": "false",
    "DD_TRACE_STARTUP_LOGS": "false",
}


# How an application configures its own OpenTelemetry exporters and SDK.
OTEL_ENVIRONMENT = {
    "OTEL_EXPORTER_OTLP_HEADERS": "x-vendor-key=customer-vendor-secret,authorization=Basic customer",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "x-vendor-traces=customer-traces-secret",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "x-vendor-logs=customer-logs-secret",
    "OTEL_EXPORTER_OTLP_COMPRESSION": "gzip",
    "OTEL_EXPORTER_OTLP_CERTIFICATE": "/nonexistent/customer-ca.pem",
    "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE": "/nonexistent/customer-client.pem",
    "OTEL_EXPORTER_OTLP_CLIENT_KEY": "/nonexistent/customer-client.key",
    "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT": "12",
}


def scenario(*arguments: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    result = subprocess.run(  # noqa: PLW1510 - the exit code is asserted
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **(env or {})},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@functools.cache
def without(tracer: str, order: str) -> str:
    """What the collector receives from the same run without the other tracer."""
    return json.dumps(scenario(tracer, order, "0")["ours"], sort_keys=True)


def assert_signed(headers: dict[str, str], path: str = "/stock") -> None:
    token = headers[HANDOFF_HEADER]
    verify(HASH_KEY, token, headers["traceparent"], "GET", path, int(token.split(".")[2]))
    assert "tracestate" not in headers
    assert "x-agentrail-cli" not in headers


def assert_unchanged_capture(run: dict[str, Any], tracer: str, order: str) -> None:
    assert json.dumps(run["ours"], sort_keys=True) == without(tracer, order)
    assert run["ours"]["traces"] == 1
    assert run["errors"] == []


@pytest.mark.parametrize("order", ["outside", "inside"])
# Fails if someone keeps Agentrail's request in OpenTelemetry's context (the
# ASGI middleware inside ours attaches a context of its own, which wipes it), or
# clears or replaces the application's current span, or strips its trace
# headers from a call other than the traceparent an allowed origin's replaces.
def test_beside_opentelemetry(order: str) -> None:
    run = scenario("opentelemetry", order, "1")
    spans = run["theirs"]["spans"]
    (server,) = [span for span in spans if span["kind"] == 1]
    (child,) = [span for span in spans if span["name"] == "customer.child"]
    assert (server["trace_id"], server["parent_span_id"]) == (TRACE, SPAN)
    assert child["parent_span_id"] == server["span_id"]
    assert {span["name"] for span in spans} == {"GET /checkout", "GET /checkout http send", "customer.child"}
    for client in CLIENTS:
        (ordinary,) = run["outbound"][f"{client}-ordinary"]
        assert set(ordinary) == {"traceparent", "tracestate", "baggage"}
        assert ordinary["traceparent"].startswith(f"00-{TRACE}-{child['span_id']}-")
        assert (ordinary["tracestate"], ordinary["baggage"]) == ("vendor=1", "customer.key=value")
        (allowed,) = run["outbound"][f"{client}-allowed"]
        assert_signed(allowed)
        assert TRACE not in allowed["traceparent"]
        assert allowed["baggage"] == "customer.key=value"
    assert_unchanged_capture(run, "opentelemetry", order)


# Fails if someone makes init register an OpenTelemetry global (tracer
# provider, logger provider or propagator): the application's own, set after
# it, would be refused or replaced, and its spans would miss its exporter.
def test_the_application_sets_its_own_globals_after_init() -> None:
    run = scenario("init-then-provider")
    assert run["propagator_fields"] == ["baggage", "traceparent", "tracestate"]
    assert run["tracer_provider_is_theirs"] is True
    assert run["logger_provider_is_theirs"] is True
    assert run["their_spans"] == ["customer.work"]
    assert run["our_spans"] == ["standalone"]


@pytest.mark.parametrize("order", ["outside", "inside"])
# Fails if someone strips Sentry's headers from managed calls again, or lets
# Agentrail's middleware break Sentry's continuation of the incoming trace.
def test_beside_sentry(order: str) -> None:
    run = scenario("sentry", order, "1")
    assert [transaction["trace_id"] for transaction in run["theirs"]["transactions"]] == [TRACE]
    for client in CLIENTS:
        for destination in ("ordinary", "allowed"):
            (headers,) = run["outbound"][f"{client}-{destination}"]
            assert headers["sentry-trace"].startswith(f"{TRACE}-")
            assert f"sentry-trace_id={TRACE}" in headers["baggage"]
        (ordinary,) = run["outbound"][f"{client}-ordinary"]
        assert set(ordinary) == {"sentry-trace", "baggage"}
        assert_signed(run["outbound"][f"{client}-allowed"][0])
    assert_unchanged_capture(run, "sentry", order)


@pytest.mark.parametrize("otel", [False, True], ids=["ddtrace", "dd-otel-enabled"])
@pytest.mark.parametrize("order", ["outside", "inside"])
# Fails if someone lets Agentrail's middleware break ddtrace's request span or
# its x-datadog-* continuation, strips ddtrace's headers from managed calls, or
# links each milestone to the span OpenTelemetry has current, which with
# DD_TRACE_OTEL_ENABLED is ddtrace's own.
def test_beside_datadog(order: str, otel: bool) -> None:
    run = scenario("datadog", order, "1", env={**DATADOG, "DD_TRACE_OTEL_ENABLED": str(otel).lower()})
    theirs = run["theirs"]
    (request,) = [span for span in theirs["spans"] if span["name"] == "starlette.request"]
    assert request["trace_id"] == DD_TRACE
    if otel:
        assert theirs["otel_provider"].startswith("ddtrace.")
        assert theirs["otel_current"] == [DD_TRACE]
    for client in CLIENTS:
        for destination in ("ordinary", "allowed"):
            (headers,) = run["outbound"][f"{client}-{destination}"]
            assert headers["x-datadog-trace-id"] == DD_TRACE
            assert headers["x-datadog-sampling-priority"] == "1"
        (ordinary,) = run["outbound"][f"{client}-ordinary"]
        assert HANDOFF_HEADER not in ordinary
        assert int(ordinary["traceparent"].split("-")[1][16:], 16) == int(DD_TRACE)
        assert ordinary["tracestate"].startswith("dd=")
        assert_signed(run["outbound"][f"{client}-allowed"][0])
    assert_unchanged_capture(run, "datadog", order)


# Fails if Agentrail's delivery or provider takes the application's OTEL_*
# settings: its exporter headers (a vendor's key) would reach Agentrail's
# collector, its compression change the body, its client certificate fail
# every export, and its attribute length limit cut Agentrail's spans.
def test_the_application_s_otel_environment_stays_its_own() -> None:
    run = scenario("environment", env=OTEL_ENVIRONMENT)
    assert sorted(request["signal"] for request in run["requests"]) == ["logs", "traces"]
    for request in run["requests"]:
        assert request["headers"] == [
            "accept",
            "accept-encoding",
            "authorization",
            "connection",
            "content-length",
            "content-type",
            "host",
            "user-agent",
        ]
        assert request["authorization"] == f"Bearer {SECRET}"
    assert run["spans"] == [
        {"url.full": "http://testserver/orders/1234567890", "user_agent.original": "synthetic-agent/1.0 (long enough)"}
    ]
    assert run["logs"] == ["order.read"]
    assert run["errors"] == []


# Fails if an MCP step with no HTTP request links both a client's _meta
# traceparent and the span the MCP SDK continued it into in the application's
# own OpenTelemetry (one client trace, two links), or stops linking the
# application's span when the message names no trace.
def test_an_mcp_step_links_a_client_trace_once() -> None:
    run = scenario("mcp-in-memory")
    assert run["links"] == [[f"{META_TRACE}-{META_SPAN}"], ["tools/call checkout"]]
