# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""What an outgoing call carries, decided by its actual destination."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from opentelemetry.trace import Span, SpanKind, StatusCode

from ._handoff import (
    AGENTRAIL_HEADERS,
    COLLECTOR_STRIPPED,
    HANDOFF_HEADER,
    SIGNED_REPLACES,
    HandoffInputError,
    format_traceparent,
    sign,
)
from ._runtime import current

if TYPE_CHECKING:
    from ._client import Client


def origin(scheme: str, host: str, port: int | None) -> str:
    scheme = scheme.lower()
    host = host.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


@dataclass
class Outgoing:
    """``span`` is None when the call is not part of a managed operation.

    The call is sent without the headers in ``remove``, then with ``headers``.
    Neither changes the application's own request object: an HTTP client builds
    a redirect from that, and the next destination is admitted on its own.
    """

    span: Span | None
    remove: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=lambda: {})

    @property
    def changes(self) -> bool:
        return bool(self.remove or self.headers)


def begin(client: Client, method: str, destination: str, path: str, target: str, url: str) -> Outgoing:
    """A managed call keeps the application's headers, its tracers' included,
    without Agentrail's own. Only an exact allowed origin receives a newly signed
    handoff, whose traceparent replaces the application's."""
    runtime = client._runtime  # pyright: ignore[reportPrivateUsage]
    if not client._managed():  # pyright: ignore[reportPrivateUsage]
        return Outgoing(None)
    options = runtime.options
    if destination == options.endpoint_origin and path in (
        f"{options.endpoint_path}/v1/traces",
        f"{options.endpoint_path}/v1/logs",
    ):
        return Outgoing(None, remove=COLLECTOR_STRIPPED)
    tracer = client._tracer  # pyright: ignore[reportPrivateUsage]
    span = tracer.start_span(
        f"HTTP {method}",
        context=current(),
        kind=SpanKind.CLIENT,
        attributes={"http.request.method": method, "url.full": url},
    )
    if destination not in options.origins:
        return Outgoing(span, remove=AGENTRAIL_HEADERS)
    context = span.get_span_context()
    parent = format_traceparent(context.trace_id, context.span_id, context.trace_flags)
    try:
        token = sign(options.input.hash_key, parent, method, target, time.time_ns() // 1_000_000)
    except HandoffInputError:
        # Nothing signed, nothing replaced: the call goes as to any other origin.
        runtime.report("handoff_refused", "context")
        return Outgoing(span, remove=AGENTRAIL_HEADERS)
    return Outgoing(
        span, remove=(*AGENTRAIL_HEADERS, *SIGNED_REPLACES), headers={"traceparent": parent, HANDOFF_HEADER: token}
    )


def finish(span: Span, status: int | None, error: BaseException | None) -> None:
    if error is not None:
        span.set_status(StatusCode.ERROR)
        span.set_attribute("error.type", "TransportError")
    if status is not None:
        span.set_attribute("http.response.status_code", status)
        if status >= 400:
            span.set_status(StatusCode.ERROR)
    span.end()
