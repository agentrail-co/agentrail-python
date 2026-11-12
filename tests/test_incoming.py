# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Incoming requests: which traces continue, and what an entry records."""

from __future__ import annotations

import time
from typing import Any

import pytest
from flask import Flask
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentrail_python import IncomingRequest, IPLocation, MilestoneContext
from agentrail_python._handoff import HANDOFF_HEADER, key_id, sign
from support import HASH_KEY, by_name, recording

CALLER = "00-11111111111111111111111111111111-2222222222222222-01"


def signed(method: str, target: str, issued_at: int | None = None, key: str = HASH_KEY) -> dict[str, str]:
    at = time.time_ns() // 1_000_000 if issued_at is None else issued_at
    return {"traceparent": CALLER, HANDOFF_HEADER: sign(key, CALLER, method, target, at)}


def starlette(client: Any, handler: Any = None) -> TestClient:
    async def endpoint(request: Request) -> PlainTextResponse:
        if handler is not None:
            handler(request)
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/orders/{id}", endpoint, methods=["GET", "POST"])])
    return TestClient(client.asgi(app))


# Fails if someone continues a trace without checking the signature over the
# exact method and target, or stops continuing a valid signed handoff.
def test_signed_handoff_continues_the_trace() -> None:
    with recording() as rec:
        response = starlette(rec.client).post("/orders/7?x=1+2", headers=signed("POST", "/orders/7?x=1+2"))
        assert response.status_code == 200
        entry = by_name(rec.spans(), "HTTP POST")
        assert entry["trace_id"] == "1" * 32
        assert entry["parent_span_id"] == "2" * 16
        assert entry["attributes"]["agentrail.ingress.verified"] is True
        assert entry["links"] == []
        assert rec.codes() == []


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(signed("GET", "/orders/7"), id="other-method"),
        pytest.param(signed("POST", "/orders/8"), id="other-target"),
        pytest.param(signed("POST", "/orders/7", issued_at=time.time_ns() // 1_000_000 - 61_000), id="expired"),
        pytest.param(signed("POST", "/orders/7", key="another-workspace-key"), id="other-key"),
    ],
)
# Fails if someone accepts a handoff signed for another request, an old one, or
# one under another key, instead of starting a fresh trace that only links.
def test_refused_handoff_starts_a_fresh_trace(headers: dict[str, str]) -> None:
    with recording() as rec:
        starlette(rec.client).post("/orders/7", headers=headers)
        entry = by_name(rec.spans(), "HTTP POST")
        assert entry["trace_id"] != "1" * 32
        assert entry["parent_span_id"] == ""
        assert entry["attributes"]["agentrail.ingress.verified"] is False
        assert entry["links"] == [("1" * 32, "2" * 16, "")]
        assert rec.codes() == ["handoff_refused"]


# Fails if someone treats an unsigned traceparent as a parent, or reports a
# refusal when no handoff was sent at all.
def test_unsigned_traceparent_is_only_a_link() -> None:
    with recording() as rec:
        starlette(rec.client).get("/orders/7", headers={"traceparent": CALLER, "tracestate": "vendor=secret"})
        entry = by_name(rec.spans(), "HTTP GET")
        assert entry["parent_span_id"] == ""
        assert entry["links"] == [("1" * 32, "2" * 16, "")]
        assert rec.codes() == []


# Fails if someone verifies a handoff against a decoded path: the edge signs the
# target exactly as sent, percent-encoding included.
def test_wsgi_verifies_the_raw_target() -> None:
    with recording() as rec:
        app = Flask(__name__)

        @app.post("/a/<path:rest>")
        def handler(rest: str) -> str:
            return "ok"

        app.wsgi_app = rec.client.wsgi(app.wsgi_app)  # type: ignore[method-assign]
        target = "/a/%7Ex?q=1+2"
        response = app.test_client().post(target, headers=signed("POST", target), environ_base={"REQUEST_URI": target})
        response.close()  # a WSGI server closes the body; the entry ends then
        entry = by_name(rec.spans(), "HTTP POST")
        assert entry["attributes"]["agentrail.ingress.verified"] is True
        assert entry["attributes"]["http.response.status_code"] == 200


# Fails if someone lets a WSGI application's exception or 5xx go unrecorded, or
# swallows the exception.
def test_wsgi_errors_are_recorded_and_raised() -> None:
    with recording() as rec:

        def broken(environ: Any, start_response: Any) -> Any:
            raise LookupError("private detail")

        app = rec.client.wsgi(broken)
        with pytest.raises(LookupError, match="private detail"):
            app({"REQUEST_METHOD": "GET", "PATH_INFO": "/", "wsgi.url_scheme": "http", "HTTP_HOST": "h"}, None)
        entry = by_name(rec.spans(), "HTTP GET")
        assert entry["attributes"]["error.type"] == "LookupError"
        assert entry["status"] == 2
        assert "private detail" not in repr(entry)


# Fails if someone records requests the application asked to ignore.
def test_ignored_requests_are_not_recorded() -> None:
    secret = "synthetic-internal-secret"
    with recording(ignore_incoming_request=lambda request: request.headers.get("x-internal") == secret) as rec:
        client = starlette(rec.client)
        client.get("/orders/1", headers={"x-internal": secret})
        client.get("/orders/2", headers={"x-internal": "guess"})
        assert [span["attributes"]["url.full"] for span in rec.spans()] == ["http://testserver/orders/2"]


def located(request: IncomingRequest) -> IPLocation:
    return IPLocation(country="US", region="Washington", city="Seattle", source="cloudflare")


# Fails if someone drops the location from entries and milestones, or attaches
# it to supporting spans.
def test_location_is_on_entries_and_milestones_only() -> None:
    with recording(incoming_location=located) as rec:

        def handler(request: Request) -> None:
            rec.client.milestone("checkout", MilestoneContext(surface="custom", kind="checkout.confirm"))
            with rec.provider.get_tracer("db").start_as_current_span("query"):
                pass

        starlette(rec.client, handler).get("/orders/7")
        spans = rec.spans()
        expected = {
            "agentrail.client.location.country": "US",
            "agentrail.client.location.region": "Washington",
            "agentrail.client.location.city": "Seattle",
            "agentrail.client.location.source": "cloudflare",
        }
        for name in ("HTTP GET", "checkout"):
            assert expected.items() <= by_name(spans, name)["attributes"].items()
        assert not any(key.startswith("agentrail.client") for key in by_name(spans, "backend.operation")["attributes"])


@pytest.mark.parametrize(
    "hook",
    [
        pytest.param(lambda request: (_ for _ in ()).throw(OSError("lookup down")), id="raises"),
        pytest.param(lambda request: IPLocation(country="usa"), id="invalid-country"),
    ],
)
# Fails if someone lets a failing location lookup fail the request, or exports
# an invalid location instead of reporting it.
def test_location_failure_never_fails_the_request(hook: Any) -> None:
    with recording(incoming_location=hook) as rec:
        assert starlette(rec.client).get("/orders/7").status_code == 200
        entry = by_name(rec.spans(), "HTTP GET")
        assert not any(key.startswith("agentrail.client") for key in entry["attributes"])
        assert rec.codes() == ["location_failed"]


def test_key_id_shape() -> None:
    assert len(key_id(HASH_KEY)) == 16


# Fails if someone lets an exception from the ignore hook fail the request, or
# stops recording and reporting that request.
def test_failing_ignore_hook_never_fails_the_request() -> None:
    def hook(request: IncomingRequest) -> bool:
        raise TypeError("comparing a header that is not ASCII")

    with recording(ignore_incoming_request=hook) as rec:
        assert starlette(rec.client).get("/orders/7").status_code == 200
        by_name(rec.spans(), "HTTP GET")
        assert rec.codes() == ["ignore_failed"]


WSGI_ENVIRON = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "wsgi.url_scheme": "http", "HTTP_HOST": "h"}


# Fails if someone runs a streamed WSGI body outside its request, so the steps
# it records start traces of their own.
def test_wsgi_streamed_body_runs_in_its_request() -> None:
    with recording() as rec:

        def streaming(environ: Any, start_response: Any) -> Any:
            start_response("200 OK", [("content-type", "text/plain")])

            def body() -> Any:
                rec.client.milestone("streamed", MilestoneContext(surface="custom", kind="stream.chunk"))
                yield b"ok"

            return body()

        response = rec.client.wsgi(streaming)(dict(WSGI_ENVIRON), lambda *args: None)
        assert list(response) == [b"ok"]
        response.close()
        spans = rec.spans()
        entry, streamed = by_name(spans, "HTTP GET"), by_name(spans, "streamed")
        assert streamed["trace_id"] == entry["trace_id"]
        assert streamed["parent_span_id"] == entry["span_id"]


# Fails if someone records a WSGI body whose close raised as a success.
def test_wsgi_close_failure_is_recorded() -> None:
    with recording() as rec:

        class Body:
            def __iter__(self) -> Any:
                return iter([b"ok"])

            def close(self) -> None:
                raise OSError("cleanup failed")

        def app(environ: Any, start_response: Any) -> Any:
            start_response("200 OK", [])
            return Body()

        response = rec.client.wsgi(app)(dict(WSGI_ENVIRON), lambda *args: None)
        assert list(response) == [b"ok"]
        with pytest.raises(OSError, match="cleanup failed"):
            response.close()
        entry = by_name(rec.spans(), "HTTP GET")
        assert entry["attributes"]["error.type"] == "OSError"
        assert entry["status"] == 2


# Fails if someone resets a streamed body's context between chunks, so a span
# it holds open across them stops being current, or lets what the body sets
# leak into the server's own context.
def test_wsgi_streamed_span_stays_current_between_chunks() -> None:
    from opentelemetry import trace

    with recording() as rec:
        step = MilestoneContext(surface="custom", kind="stream.chunk")

        def streaming(environ: Any, start_response: Any) -> Any:
            start_response("200 OK", [])

            def body() -> Any:
                with rec.client.start_milestone("stream", step):
                    yield b"a"
                    rec.client.milestone("inner", step)
                    yield b"b"

            return body()

        response = rec.client.wsgi(streaming)(dict(WSGI_ENVIRON), lambda *args: None)
        chunks = iter(response)
        assert next(chunks) == b"a"
        assert not trace.get_current_span().is_recording()
        assert list(chunks) == [b"b"]
        response.close()
        spans = rec.spans()
        assert by_name(spans, "inner")["parent_span_id"] == by_name(spans, "stream")["span_id"]
