# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""httpx transports that sign trace context only for allowed origins.

``httpx.Client(transport=agentrail_python.httpx.transport(agentrail))``. Each
redirect passes through the transport again, so every actual destination is
admitted on its own. The application's headers, its tracers' included, pass
through; only an allowed origin's traceparent is replaced by a signed one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import httpx

from ._outbound import begin, finish, origin

if TYPE_CHECKING:
    from opentelemetry.trace import Span

    from ._client import Client


def _prepare(client: Client, request: httpx.Request) -> tuple[httpx.Request, Span | None]:
    """The request to send, and the call's span. The application's request is left
    as it was: httpx builds a redirect from it, never from what was sent."""
    url = request.url
    target = url.raw_path.decode("ascii")
    outgoing = begin(
        client,
        request.method,
        origin(url.scheme, url.host, url.port),
        target.split("?", 1)[0],
        target,
        str(url),
    )
    if not outgoing.changes:
        return request, outgoing.span
    headers = request.headers.copy()
    for name in outgoing.remove:
        headers.pop(name, None)
    headers.update(outgoing.headers)
    sent = httpx.Request(request.method, url, headers=headers, stream=request.stream, extensions=request.extensions)
    return sent, outgoing.span


class _Stream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, span: Span, status: int) -> None:
        self._stream = stream
        self._span = span
        self._status = status
        self._done = False

    def _finish(self, error: BaseException | None) -> None:
        if not self._done:
            self._done = True
            finish(self._span, self._status, error)

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._stream
        except BaseException as error:
            self._finish(error)
            raise

    def close(self) -> None:
        try:
            self._stream.close()
        except BaseException as error:
            self._finish(error)
            raise
        self._finish(None)


class _AsyncStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, span: Span, status: int) -> None:
        self._stream = stream
        self._span = span
        self._status = status
        self._done = False

    def _finish(self, error: BaseException | None) -> None:
        if not self._done:
            self._done = True
            finish(self._span, self._status, error)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._stream:
                yield chunk
        except BaseException as error:
            self._finish(error)
            raise

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        except BaseException as error:
            self._finish(error)
            raise
        self._finish(None)


class AgentrailTransport(httpx.BaseTransport):
    def __init__(self, client: Client, base: httpx.BaseTransport | None = None) -> None:
        self._client = client
        self._base = base if base is not None else httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        sent, span = _prepare(self._client, request)
        if span is None:
            return self._base.handle_request(sent)
        try:
            response = self._base.handle_request(sent)
        except BaseException as error:
            finish(span, None, error)
            raise
        stream = response.stream
        if response.is_closed or not isinstance(stream, httpx.SyncByteStream):
            # A body already read (or none to read) ends the call now.
            finish(span, response.status_code, None)
            return response
        response.stream = _Stream(stream, span, response.status_code)
        return response

    def close(self) -> None:
        self._base.close()


class AgentrailAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, client: Client, base: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = client
        self._base = base if base is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        sent, span = _prepare(self._client, request)
        if span is None:
            return await self._base.handle_async_request(sent)
        try:
            response = await self._base.handle_async_request(sent)
        except BaseException as error:
            finish(span, None, error)
            raise
        stream = response.stream
        if response.is_closed or not isinstance(stream, httpx.AsyncByteStream):
            finish(span, response.status_code, None)
            return response
        response.stream = _AsyncStream(stream, span, response.status_code)
        return response

    async def aclose(self) -> None:
        await self._base.aclose()


def transport(client: Client, base: httpx.BaseTransport | None = None) -> AgentrailTransport:
    """A transport for ``httpx.Client``. ``base`` defaults to ``httpx.HTTPTransport()``."""
    return AgentrailTransport(client, base)


def async_transport(client: Client, base: httpx.AsyncBaseTransport | None = None) -> AgentrailAsyncTransport:
    """A transport for ``httpx.AsyncClient``. ``base`` defaults to ``httpx.AsyncHTTPTransport()``."""
    return AgentrailAsyncTransport(client, base)
