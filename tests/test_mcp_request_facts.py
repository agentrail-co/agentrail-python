# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""What an MCP step says of the HTTP request that carried its message.

The two requests in the contract's ``otlp.mcpRequestFacts``, as Claude Code
and Codex wrote them, are sent byte for byte over TLS to real servers, and the
exported step must carry the attributes the contract lists for each.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx2
import pytest
import trustme
import uvicorn
from a2wsgi import ASGIMiddleware
from mcp.client.client import Client as McpClient
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from werkzeug.serving import make_server

from agentrail_python.mcp import McpCapture, mcp_capture
from support import CONTRACT, recording

OTLP = CONTRACT["otlp"]
MCP = OTLP["mcpAttributes"]
REQUESTS: list[dict[str, Any]] = OTLP["mcpRequestFacts"]["requests"]
# Everything otlp.mcpSpans.requestFacts names: a step carries these and no others of the request.
FACTS = {
    "http.request.method",
    "url.full",
    "http.response.status_code",
    "user_agent.original",
    MCP["headerNames"],
    MCP["accept"],
    MCP["acceptEncoding"],
    MCP["jsonrpcId"],
    MCP["toolName"],
}
HOST = "mcp.example.test"
MARKER = "028T5CY4TQKFFX"  # a valid marker: the one query parameter a URL keeps
assert {urlsplit(request["url"]).hostname for request in REQUESTS} == {HOST}


def mcp_server(capture: McpCapture) -> MCPServer:
    server = MCPServer("projects", middleware=[capture.middleware])

    def list_projects() -> str:
        return "synthetic projects"

    server.tool(name="list_projects")(capture.tool("list_projects", list_projects))
    return server


def mcp_app(server: MCPServer) -> Any:
    # A deployed server allows its own host; a stateless one takes a call
    # without initialize, as both recorded requests are.
    return server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=[HOST]),
    )


@pytest.fixture(scope="module")
def ca() -> trustme.CA:
    return trustme.CA()


def send(port: int, ca: trustme.CA, request: dict[str, Any]) -> tuple[int, Any]:
    """The recorded request over TLS, its headers in their order and case, as its client wrote them."""
    body = json.dumps(request["body"], separators=(",", ":")).encode()
    headers = [
        (name, str(len(body)) if name.lower() == "content-length" else value) for name, value in request["headers"]
    ]
    head = f"{request['method']} {urlsplit(request['url']).path} HTTP/1.1\r\n"
    head += "".join(f"{name}: {value}\r\n" for name, value in headers) + "\r\n"
    context = ssl.create_default_context()
    ca.configure_trust(context)
    with (
        socket.create_connection(("127.0.0.1", port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname=HOST) as tls,
    ):
        tls.sendall(head.encode("latin-1") + body)
        response = http.client.HTTPResponse(tls)
        response.begin()
        return response.status, json.loads(response.read())


@contextmanager
def uvicorn_server(app: Any, ca: trustme.CA) -> Iterator[int]:
    listener = socket.create_server(("127.0.0.1", 0))
    with ca.issue_cert(HOST).private_key_and_cert_chain_pem.tempfile() as pem:
        server = uvicorn.Server(uvicorn.Config(app, ssl_certfile=pem, log_level="error", lifespan="on"))
        # Connections wait in the listener's backlog until the server, its lifespan started, accepts them.
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1]
        finally:
            server.should_exit = True
            thread.join(10)
            listener.close()


@contextmanager
def werkzeug_server(
    make_app: Callable[[asyncio.AbstractEventLoop], Any], server: MCPServer, ca: trustme.CA
) -> Iterator[int]:
    """A WSGI server in front of the MCP app, through a2wsgi, on a loop that runs its session manager."""
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    started, stop = threading.Event(), asyncio.Event()

    async def session_manager() -> None:
        async with server.session_manager.run():
            started.set()
            await stop.wait()

    running = asyncio.run_coroutine_threadsafe(session_manager(), loop)
    assert started.wait(10)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert(HOST).configure_cert(context)
    wsgi = make_server("127.0.0.1", 0, make_app(loop), threaded=True, ssl_context=context)
    thread = threading.Thread(target=wsgi.serve_forever, daemon=True)
    thread.start()
    try:
        yield wsgi.server_port
    finally:
        wsgi.shutdown()
        wsgi.server_close()
        loop.call_soon_threadsafe(stop.set)
        running.result(10)
        loop.call_soon_threadsafe(loop.stop)
        runner.join(10)
        loop.close()


def step(spans: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [
        span
        for span in spans
        if span["name"] == "mcp.tools/call:list_projects" and span["attributes"].get("agentrail.step") is True
    ]
    assert len(matches) == 1, [span["name"] for span in spans]
    return matches[0]


def facts(span: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in span["attributes"].items() if key in FACTS}


# Fails if the ASGI step loses or reorders a header name, takes a value for a
# name, drops Accept, Accept-Encoding, the user agent, the method, the URL or
# the JSON-RPC id, invents an Accept-Encoding Codex never sent, or lets the
# authorization value or the load balancer's address leave the process.
@pytest.mark.parametrize("request_", REQUESTS, ids=[request["name"] for request in REQUESTS])
def test_asgi_step_carries_the_recorded_request(ca: trustme.CA, request_: dict[str, Any]) -> None:
    with recording() as rec:
        server = mcp_server(mcp_capture(rec.client))
        with uvicorn_server(rec.client.asgi(mcp_app(server)), ca) as port:
            status, answer = send(port, ca, request_)
        assert status == 200 and answer["id"] == request_["body"]["id"], answer
        spans = rec.spans()
        # ASGI keeps arrival order and repeats: the names exactly as the client wrote them.
        assert facts(step(spans)) == request_["attributes"]
        for secret in ("stand-in", "203.0.113.7"):
            assert secret not in repr(spans)


# Fails if the WSGI step loses content-type or content-length (WSGI moves both
# out of HTTP_*), or host, or sends the environ's own order, or anything the
# ASGI replay above would catch.
@pytest.mark.parametrize("request_", REQUESTS, ids=[request["name"] for request in REQUESTS])
def test_wsgi_step_carries_the_recorded_request(ca: trustme.CA, request_: dict[str, Any]) -> None:
    with recording() as rec:
        server = mcp_server(mcp_capture(rec.client))
        app = mcp_app(server)
        with werkzeug_server(lambda loop: rec.client.wsgi(ASGIMiddleware(app, loop=loop)), server, ca) as port:
            status, answer = send(port, ca, request_)
        assert status == 200 and answer["id"] == request_["body"]["id"], answer
        spans = rec.spans()
        sent, wanted = facts(step(spans)), dict(request_["attributes"])
        # WSGI keeps neither the order nor the case of header names: compared
        # lower-cased, as a set, which the SDK sends sorted.
        names = sent.pop(MCP["headerNames"]).split(",")
        assert names == sorted({name.lower() for name in wanted.pop(MCP["headerNames"]).split(",")})
        assert sent == wanted
        for secret in ("stand-in", "203.0.113.7"):
            assert secret not in repr(spans)


# Fails if someone drops the check that every name is an HTTP token: a server
# that hands on a malformed header list would send a header's value in the
# names. No part of such a list is sent, as the collector takes none of it.
# Also fails if the step's URL keeps its query past Agentrail's markers.
def test_a_value_never_reaches_the_names() -> None:
    def lax(app: Any) -> Any:
        async def run(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                scope = {**scope, "headers": [*scope["headers"], (b"x-note: secret-value", b"")]}
            await app(scope, receive, send)

        return run

    call = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}}
    with recording() as rec:
        server = mcp_server(mcp_capture(rec.client))
        asgi = lax(rec.client.asgi(mcp_app(server)))

        async def run() -> None:
            async with server.session_manager.run():
                transport = httpx2.ASGITransport(app=asgi)
                async with httpx2.AsyncClient(transport=transport, base_url=f"https://{HOST}") as http:
                    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
                    response = await http.post(f"/mcp?token=secret-query&h={MARKER}", json=call, headers=headers)
                    assert response.status_code == 200

        anyio.run(run)
        spans = rec.spans()
        sent = step(spans)["attributes"]
        assert MCP["headerNames"] not in sent
        assert sent[MCP["jsonrpcId"]] == "4"
        assert sent["url.full"] == f"https://{HOST}/mcp?h={MARKER}"
        for secret in ("secret-value", "secret-query"):
            assert secret not in repr(spans)


# Fails if someone gives a message that came by no HTTP request the facts of
# the one it was handled inside: an application calling its own MCP server in
# memory while answering a request would claim that request's headers.
def test_an_in_memory_message_carries_no_request() -> None:
    with recording() as rec:
        server = mcp_server(mcp_capture(rec.client))

        async def endpoint(request: Request) -> PlainTextResponse:
            async with McpClient(server) as client:
                await client.call_tool("list_projects", {})
            return PlainTextResponse("ok")

        asgi = rec.client.asgi(Starlette(routes=[Route("/agent", endpoint, methods=["POST"])]))

        async def run() -> None:
            transport = httpx2.ASGITransport(app=asgi)
            async with httpx2.AsyncClient(transport=transport, base_url="http://localhost") as http:
                assert (await http.post("/agent")).status_code == 200

        anyio.run(run)
        spans = rec.spans()
        assert any(span["name"] == "HTTP POST" for span in spans)
        assert facts(step(spans)) == {MCP["toolName"]: "list_projects"}
