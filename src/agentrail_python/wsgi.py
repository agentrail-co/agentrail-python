# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""WSGI middleware: Flask, Django (WSGI), Falcon, Pyramid, Bottle."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from opentelemetry import context as otel_context
from opentelemetry.trace import Span, StatusCode

from ._options import IncomingRequest

if TYPE_CHECKING:
    from ._client import Client

StartResponse = Callable[..., Any]
WSGIApp = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]


def _request(environ: Mapping[str, Any]) -> IncomingRequest:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_") and isinstance(value, str):
            headers[key[5:].replace("_", "-").lower()] = value
    for key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
        value = environ.get(key)
        if isinstance(value, str) and value:
            headers[key.replace("_", "-").lower()] = value
    # Servers that keep the request line as sent (gunicorn, Werkzeug, uWSGI) give
    # the exact target a handoff was signed over; PATH_INFO is already decoded.
    raw = environ.get("RAW_URI") or environ.get("REQUEST_URI")
    if isinstance(raw, str) and raw.startswith("/"):
        target = raw
    else:
        path = quote(
            str(environ.get("SCRIPT_NAME", "")) + str(environ.get("PATH_INFO", "/")),
            safe="/%:@!$&'()*+,;=~",
        )
        query = str(environ.get("QUERY_STRING", ""))
        target = f"{path}?{query}" if query else path
    scheme = str(environ.get("wsgi.url_scheme", "http"))
    host = headers.get("host") or f"{environ.get('SERVER_NAME', 'localhost')}:{environ.get('SERVER_PORT', '80')}"
    address = environ.get("REMOTE_ADDR")
    return IncomingRequest(
        method=str(environ.get("REQUEST_METHOD", "GET")),
        target=target,
        scheme=scheme,
        host=host,
        headers=headers,
        client=address if isinstance(address, str) and address else None,
        raw=environ,
    )


class _Body:
    """The response body; the entry span ends when the server closes it.

    A streamed body runs after the application returned, so each step of it, and
    its close, runs in the request's context: its logs, milestones and outgoing
    calls belong to the request.
    """

    def __init__(
        self,
        body: Iterable[bytes],
        finish: Callable[[BaseException | None], None],
        ctx: otel_context.Context,
    ) -> None:
        self._body = body
        self._finish = finish
        self._ctx = ctx

    def __iter__(self) -> Iterator[bytes]:
        token = otel_context.attach(self._ctx)
        try:
            chunks = iter(self._body)
        except BaseException as error:
            self._finish(error)
            raise
        finally:
            otel_context.detach(token)
        while True:
            token = otel_context.attach(self._ctx)
            try:
                chunk = next(chunks)
            except StopIteration:
                return
            except BaseException as error:
                self._finish(error)
                raise
            finally:
                otel_context.detach(token)
            yield chunk

    def close(self) -> None:
        close = getattr(self._body, "close", None)
        if not callable(close):
            self._finish(None)
            return
        token = otel_context.attach(self._ctx)
        try:
            close()
        except BaseException as error:
            self._finish(error)
            raise
        finally:
            otel_context.detach(token)
        self._finish(None)


class AgentrailWSGIMiddleware:
    """Records each HTTP request the wrapped application serves as a journey step.

    ``app.wsgi_app = agentrail.wsgi(app.wsgi_app)`` in Flask, or
    ``application = agentrail.wsgi(get_wsgi_application())`` in Django.
    """

    def __init__(self, app: WSGIApp, *, client: Client) -> None:
        self.app = app
        self._client = client

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        entered = self._client._enter(_request(environ))  # pyright: ignore[reportPrivateUsage]
        if entered is None:
            return self.app(environ, start_response)
        span, ctx = entered
        status: list[int] = []
        ended = threading.Lock()

        def finish(error: BaseException | None) -> None:
            if not ended.acquire(blocking=False):
                return
            _end(span, status, error)

        def observed_start(status_line: str, headers: Any, exc_info: Any = None) -> Any:
            code = status_line.split(" ", 1)[0]
            if code.isdigit():
                status[:] = [int(code)]
            return start_response(status_line, headers, exc_info) if exc_info else start_response(status_line, headers)

        token = otel_context.attach(ctx)
        try:
            body = self.app(environ, observed_start)
        except BaseException as error:
            finish(error)
            raise
        finally:
            otel_context.detach(token)
        return _Body(body, finish, ctx)


def _end(span: Span, status: list[int], error: BaseException | None) -> None:
    if error is not None:
        span.set_status(StatusCode.ERROR)
        span.set_attribute("error.type", type(error).__qualname__)
    if status:
        span.set_attribute("http.response.status_code", status[0])
        if status[0] >= 500:
            span.set_status(StatusCode.ERROR)
    span.end()
