# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""What an outgoing call carries, decided by its actual destination."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from opentelemetry.trace import Span, SpanKind, StatusCode

from ._handoff import HANDOFF_HEADER, HandoffInputError, format_traceparent, sign

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
    """``span`` is None when the call is not part of a managed operation."""

    span: Span | None
    headers: dict[str, str] = field(default_factory=lambda: {})
    strip: bool = False


def begin(client: Client, method: str, destination: str, path: str, target: str, url: str) -> Outgoing:
    """Owned headers are always stripped from managed calls; only exact allowed
    origins receive a newly signed handoff."""
    runtime = client._runtime  # pyright: ignore[reportPrivateUsage]
    if not client._managed():  # pyright: ignore[reportPrivateUsage]
        return Outgoing(None)
    options = runtime.options
    if destination == options.endpoint_origin and path in (
        f"{options.endpoint_path}/v1/traces",
        f"{options.endpoint_path}/v1/logs",
    ):
        return Outgoing(None, strip=True)
    tracer = client._tracer  # pyright: ignore[reportPrivateUsage]
    span = tracer.start_span(
        f"HTTP {method}",
        kind=SpanKind.CLIENT,
        attributes={"http.request.method": method, "url.full": url},
    )
    outgoing = Outgoing(span, strip=True)
    if destination in options.origins:
        context = span.get_span_context()
        parent = format_traceparent(context.trace_id, context.span_id, context.trace_flags)
        try:
            token = sign(options.input.hash_key, parent, method, target, time.time_ns() // 1_000_000)
        except HandoffInputError:
            runtime.report("handoff_refused", "context")
        else:
            outgoing.headers = {"traceparent": parent, HANDOFF_HEADER: token}
    return outgoing


def finish(span: Span, status: int | None, error: BaseException | None) -> None:
    if error is not None:
        span.set_status(StatusCode.ERROR)
        span.set_attribute("error.type", "TransportError")
    if status is not None:
        span.set_attribute("http.response.status_code", status)
        if status >= 400:
            span.set_status(StatusCode.ERROR)
    span.end()
