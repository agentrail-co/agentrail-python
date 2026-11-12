# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""ASGI middleware: FastAPI, Starlette, Django (ASGI), Quart, Litestar."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from opentelemetry import context as otel_context
from opentelemetry.trace import StatusCode

from ._options import IncomingRequest

if TYPE_CHECKING:
    from ._client import Client

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _request(scope: Scope) -> IncomingRequest:
    headers: dict[str, list[str]] = {}
    for raw_name, raw_value in scope.get("headers", ()):
        name = bytes(raw_name).decode("latin-1").lower()
        headers.setdefault(name, []).append(bytes(raw_value).decode("latin-1"))
    raw_path = scope.get("raw_path")
    path = (
        bytes(raw_path).decode("latin-1")
        if raw_path
        else quote(str(scope.get("root_path", "")) + str(scope.get("path", "/")), safe="/%:@!$&'()*+,;=~")
    )
    query = bytes(scope.get("query_string", b"")).decode("latin-1")
    target = f"{path}?{query}" if query else path
    host = headers.get("host", [""])[0]
    if not host:
        server = scope.get("server")
        host = f"{server[0]}:{server[1]}" if server else "localhost"
    client = scope.get("client")
    return IncomingRequest(
        method=str(scope.get("method", "GET")),
        target=target,
        scheme=str(scope.get("scheme", "http")),
        host=host,
        # A header sent more than once is joined, so a handoff or traceparent
        # sent twice cannot verify.
        headers={name: ", ".join(values) for name, values in headers.items()},
        client=None if not client else str(client[0]),
        raw=scope,
    )


def _route(scope: Scope) -> str | None:
    route = scope.get("route")
    for attribute in ("path_format", "path"):
        value = getattr(route, attribute, None)
        if isinstance(value, str) and value:
            return value
    return None


class AgentrailASGIMiddleware:
    """Records each HTTP request the wrapped application serves as a journey step.

    ``app.add_middleware(AgentrailASGIMiddleware, client=agentrail)`` in FastAPI and
    Starlette, or ``application = agentrail.asgi(application)``. Install it once.
    """

    def __init__(self, app: ASGIApp, *, client: Client) -> None:
        self.app = app
        self._client = client

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        entered = self._client._enter(_request(scope))  # pyright: ignore[reportPrivateUsage]
        if entered is None:
            await self.app(scope, receive, send)
            return
        span, ctx = entered
        status: list[int] = []

        async def observed_send(message: Message) -> None:
            if message.get("type") == "http.response.start" and not status:
                status.append(int(message["status"]))
            await send(message)

        token = otel_context.attach(ctx)
        try:
            await self.app(scope, receive, observed_send)
        except BaseException as error:
            span.set_status(StatusCode.ERROR)
            span.set_attribute("error.type", type(error).__qualname__)
            raise
        finally:
            try:
                code = status[0] if status else None
                if code is not None:
                    span.set_attribute("http.response.status_code", code)
                    if code >= 500:
                        span.set_status(StatusCode.ERROR)
                route = _route(scope)
                if route is not None:
                    span.set_attribute("http.route", route)
                span.end()
            finally:
                otel_context.detach(token)
