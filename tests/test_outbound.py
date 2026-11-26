# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Outgoing calls: only exact allowed destinations receive signed context."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
import requests

from agentrail_python import MilestoneContext
from agentrail_python._handoff import HANDOFF_HEADER, verify
from agentrail_python.httpx import async_transport, transport
from agentrail_python.requests import adapter
from support import HASH_KEY, by_name, recording

ALLOWED = "https://inventory.internal"
STEP = MilestoneContext(surface="custom", kind="order.sync")
FORBIDDEN = ("traceparent", "tracestate", "baggage", "sentry-trace", HANDOFF_HEADER)


class Recorder(httpx.BaseTransport):
    def __init__(self, redirect_to: str | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.redirect_to = redirect_to

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.redirect_to is not None and len(self.requests) == 1:
            return httpx.Response(302, headers={"location": self.redirect_to})
        return httpx.Response(200, content=b"body")


def customer_headers() -> dict[str, str]:
    return {"traceparent": "00-" + "9" * 32 + "-" + "8" * 16 + "-01", "baggage": "user=secret", "tracestate": "v=1"}


# Fails if someone signs for an origin that is not listed, signs a different
# target than the one sent, sends a handoff the server received on, or removes
# the application's trace headers other than the traceparent (and its
# tracestate) an allowed origin's signed one replaces.
def test_httpx_signs_only_allowed_origins() -> None:
    with recording(outbound_origins=(ALLOWED,)) as rec:
        network = Recorder()
        client = httpx.Client(transport=transport(rec.client, network))
        received = {**customer_headers(), HANDOFF_HEADER: "ah1.copied-from-the-incoming-request"}
        with rec.client.start_milestone("sync", STEP):
            client.get(f"{ALLOWED}/stock/%2F?sku=1", headers=received)
            client.get("https://inventory.internal.evil.test/stock", headers=received)
        allowed, other = network.requests
        token = allowed.headers[HANDOFF_HEADER]
        verify(HASH_KEY, token, allowed.headers["traceparent"], "GET", "/stock/%2F?sku=1", int(token.split(".")[2]))
        assert "tracestate" not in allowed.headers
        assert allowed.headers["baggage"] == "user=secret"
        assert {name: other.headers.get(name) for name in customer_headers()} == customer_headers()
        assert HANDOFF_HEADER not in other.headers
        spans = rec.spans()
        assert [span["kind"] for span in spans if span["name"] == "HTTP GET"] == [3, 3]
        assert {span["attributes"]["http.response.status_code"] for span in spans if span["name"] == "HTTP GET"} == {
            200
        }


# Fails if someone signs a redirect's next hop because the first one was allowed.
def test_httpx_redirect_is_admitted_per_destination() -> None:
    with recording(outbound_origins=(ALLOWED,)) as rec:
        network = Recorder(redirect_to="https://elsewhere.test/landing")
        client = httpx.Client(transport=transport(rec.client, network), follow_redirects=True)
        with rec.client.start_milestone("sync", STEP):
            client.get(f"{ALLOWED}/go")
        first, second = network.requests
        assert HANDOFF_HEADER in first.headers
        assert not any(name in second.headers for name in FORBIDDEN)


# Fails if someone admits by the Host header instead of the actual destination.
def test_httpx_host_header_does_not_admit() -> None:
    with recording(outbound_origins=(ALLOWED,)) as rec:
        network = Recorder()
        with rec.client.start_milestone("sync", STEP):
            httpx.Client(transport=transport(rec.client, network)).get(
                "https://other.test/x", headers={"host": "inventory.internal"}
            )
        assert HANDOFF_HEADER not in network.requests[0].headers


# Fails if someone records or alters calls made outside a managed operation.
def test_unmanaged_calls_pass_through() -> None:
    with recording(outbound_origins=(ALLOWED,)) as rec:
        network = Recorder()
        httpx.Client(transport=transport(rec.client, network)).get(f"{ALLOWED}/x", headers=customer_headers())
        assert network.requests[0].headers["baggage"] == "user=secret"
        assert HANDOFF_HEADER not in network.requests[0].headers
        assert rec.spans() == []


# Fails if someone records an async call or loses its trace context.
@pytest.mark.anyio
async def test_async_httpx() -> None:
    class AsyncRecorder(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(503)

    with recording(outbound_origins=(ALLOWED,)) as rec:
        network = AsyncRecorder()
        async with httpx.AsyncClient(transport=async_transport(rec.client, network)) as client:
            async with rec.client.start_milestone("sync", STEP):
                await client.post(f"{ALLOWED}/sync")
        assert HANDOFF_HEADER in network.requests[0].headers
        call = by_name(rec.spans(), "HTTP POST")
        assert call["status"] == 2
        assert call["attributes"]["http.response.status_code"] == 503


@contextmanager
def server(redirect_to: str | None = None) -> Iterator[tuple[str, list[dict[str, str]]]]:
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}})
            if redirect_to is not None:
                self.send_response(302)
                self.send_header("location", redirect_to)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: Any) -> None:
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    finally:
        httpd.shutdown()
        httpd.server_close()


# Fails if someone signs for an unlisted origin through requests, signs a
# different target than the request line sends, or removes the application's
# trace headers other than those an allowed origin's signed traceparent replaces.
def test_requests_adapter() -> None:
    with server() as (allowed_origin, allowed_seen), server() as (other_origin, other_seen):
        with recording(outbound_origins=(allowed_origin,)) as rec:
            session = requests.Session()
            session.mount("http://", adapter(rec.client))
            with rec.client.start_milestone("sync", STEP):
                session.get(f"{allowed_origin}/stock?sku=1", headers=customer_headers())
                session.get(f"{other_origin}/stock", headers=customer_headers())
            (allowed,), (other,) = allowed_seen, other_seen
            token = allowed[HANDOFF_HEADER]
            verify(HASH_KEY, token, allowed["traceparent"], "GET", allowed["path"], int(token.split(".")[2]))
            assert allowed["path"] == "/stock?sku=1"
            assert "tracestate" not in allowed
            assert allowed["baggage"] == "user=secret"
            assert {name: other.get(name) for name in customer_headers()} == customer_headers()
            assert HANDOFF_HEADER not in other
            names = [span["name"] for span in rec.spans()]
            assert names.count("HTTP GET") == 2, names


# Fails if someone adds the signed headers to the application's own request
# object: requests builds a redirect from it, so the next destination would
# receive the handoff and the traceparent signed for the allowed one.
def test_requests_redirect_is_admitted_per_destination() -> None:
    with (
        server() as (other_origin, other_seen),
        server(redirect_to=f"{other_origin}/landing") as (
            allowed_origin,
            allowed_seen,
        ),
    ):
        with recording(outbound_origins=(allowed_origin,)) as rec:
            session = requests.Session()
            session.mount("http://", adapter(rec.client))
            with rec.client.start_milestone("sync", STEP):
                session.get(f"{allowed_origin}/go")
            ((first,), (second,)) = allowed_seen, other_seen
            assert HANDOFF_HEADER in first
            assert not any(name in second for name in FORBIDDEN)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# Fails if someone records an outgoing call whose response stream failed to
# close as a success.
def test_httpx_close_failure_is_recorded() -> None:
    class FailingClose(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"ok"

        def close(self) -> None:
            raise OSError("connection reset on close")

    class Network(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=FailingClose())

    with recording() as rec:
        client = httpx.Client(transport=transport(rec.client, Network()))
        with rec.client.start_milestone("sync", STEP):
            with pytest.raises(OSError, match="connection reset on close"):
                client.get("https://inventory.example/items")
        call = by_name(rec.spans(), "HTTP GET")
        assert call["attributes"]["error.type"] == "TransportError"
        assert call["status"] == 2


# Fails if someone lets a server send a CLI's header on: a server that copies
# incoming headers into its own calls would tell the next service, listed or
# not, the CLI's run, session and command.
def test_a_cli_header_is_never_sent_on() -> None:
    cli = {"x-agentrail-cli": 'v=1, sdk="0.4.0", cli="acme"'}
    with server() as (allowed_origin, allowed_seen), server() as (other_origin, other_seen):
        with recording(outbound_origins=(allowed_origin, ALLOWED)) as rec:
            network = Recorder()
            client = httpx.Client(transport=transport(rec.client, network))
            session = requests.Session()
            session.mount("http://", adapter(rec.client))
            with rec.client.start_milestone("sync", STEP):
                client.get(f"{ALLOWED}/x", headers=cli)
                client.get("https://other.test/x", headers=cli)
                session.get(f"{allowed_origin}/x", headers=cli)
                session.get(f"{other_origin}/x", headers=cli)
            assert [request.headers.get("x-agentrail-cli") for request in network.requests] == [None, None]
            assert [seen.get("x-agentrail-cli") for seen in allowed_seen + other_seen] == [None, None]
