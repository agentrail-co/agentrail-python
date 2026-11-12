# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""The client an application records with."""

from __future__ import annotations

import functools
import inspect
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar, cast

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Link, NonRecordingSpan, Span, SpanContext, SpanKind, StatusCode, TraceFlags

from ._handoff import HANDOFF_HEADER, Refused, parse_traceparent, verify
from ._options import (
    LOCATION_SOURCES,
    PRINCIPAL_KINDS,
    SURFACES,
    ConfigurationError,
    IncomingRequest,
    MilestoneContext,
    VerifiedIdentity,
)
from ._performance import performance_attributes
from ._privacy import StructuredLog, clean_context, copy_log
from ._queue import ExportQueue, QueueStats
from ._runtime import Attributes, Mark, RequestState, Runtime
from ._suppression import suppressed
from ._text import bounded, valid_text
from ._version import VERSION

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import ReadableLogRecord
    from opentelemetry.sdk.trace import ReadableSpan

    from .asgi import AgentrailASGIMiddleware, ASGIApp
    from .wsgi import AgentrailWSGIMiddleware, WSGIApp

F = TypeVar("F", bound=Callable[..., Any])
_COUNTRY = re.compile(r"[A-Z]{2}")


@dataclass(frozen=True)
class Diagnostics:
    spans: QueueStats
    logs: QueueStats


def _error_type(error: BaseException) -> str:
    return bounded(type(error).__qualname__, 4096)


class MilestoneScope:
    """A milestone around a block: ``with`` or ``async with``.

    The block's exception is recorded and re-raised unchanged.
    """

    def __init__(self, client: Client, name: str, details: MilestoneContext) -> None:
        client._validate_milestone(name, details)  # pyright: ignore[reportPrivateUsage]
        self._client = client
        self._name = name
        self._details = details
        self._span: Span | None = None
        self._token: object | None = None

    def __enter__(self) -> MilestoneScope:
        span, ctx = self._client._start(self._name, self._details)  # pyright: ignore[reportPrivateUsage]
        self._span = span
        self._token = otel_context.attach(ctx)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        span, token = self._span, self._token
        self._span = self._token = None
        if span is None or token is None:
            return
        try:
            if exc is not None:
                span.set_status(StatusCode.ERROR)
                span.set_attribute("error.type", _error_type(exc))
            span.end()
        finally:
            otel_context.detach(cast(Any, token))

    async def __aenter__(self) -> MilestoneScope:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, traceback)


class Client:
    """Records backend evidence with a bound tracer provider.

    Get one from ``init`` or from ``create_processors(...).bind(provider)``.
    """

    def __init__(
        self,
        runtime: Runtime,
        provider: trace.TracerProvider,
        spans: ExportQueue[ReadableSpan],
        logs: ExportQueue[ReadableLogRecord],
    ) -> None:
        self._runtime = runtime
        self._tracer = provider.get_tracer(runtime.scope.name, VERSION)
        self._spans = spans
        self._logs = logs
        self._closed = False
        self._shutdown_lock = threading.Lock()
        self._on_shutdown: list[Callable[[], object]] = []

    # Identity and milestones

    def identify(self, identity: VerifiedIdentity) -> None:
        """Assert an identity the application has already authenticated.

        Only inside a managed request or milestone. IDs are hashed before export.
        """
        if (
            identity.kind not in PRINCIPAL_KINDS
            or not identity.id
            or not valid_text(identity.id, 1 << 20)
            or (identity.acting_user_id is not None and not identity.acting_user_id)
            or (identity.acting_user_id is not None and not valid_text(identity.acting_user_id, 1 << 20))
        ):
            raise ConfigurationError("Use a supported principal kind and nonempty, well-formed identity IDs.")
        if suppressed():
            return
        state = self._runtime.state()
        if state is None:
            self._runtime.report("identity_outside_request", "context")
            return
        hashed = self._runtime.identity(identity)
        if state.assert_identity(hashed):
            # The first assertion stands; a different one is recorded as its own auth step.
            span, _ = self._start("identity.asserted", MilestoneContext(surface="auth", kind="auth.identity"), hashed)
            span.end()

    def milestone(self, name: str, details: MilestoneContext) -> None:
        """Record an instant step."""
        self._validate_milestone(name, details)
        if suppressed():
            return
        span, _ = self._start(name, details)
        span.end()

    def start_milestone(self, name: str, details: MilestoneContext) -> MilestoneScope:
        """A step around a block: ``with client.start_milestone(...):``."""
        return MilestoneScope(self, name, details)

    def with_milestone(self, name: str, details: MilestoneContext) -> Callable[[F], F]:
        """Decorate a function or coroutine function: each call is one step."""
        self._validate_milestone(name, details)

        def decorate(function: F) -> F:
            if inspect.iscoroutinefunction(function):

                @functools.wraps(function)
                async def run_async(*args: Any, **kwargs: Any) -> Any:
                    async with MilestoneScope(self, name, details):
                        return await function(*args, **kwargs)

                return cast(F, run_async)

            @functools.wraps(function)
            def run(*args: Any, **kwargs: Any) -> Any:
                with MilestoneScope(self, name, details):
                    return function(*args, **kwargs)

            return cast(F, run)

        return decorate

    def _validate_milestone(self, name: str, details: MilestoneContext) -> None:
        if (
            not name
            or not details.kind
            or not valid_text(name, 256)
            or not valid_text(details.kind, 256)
            or details.surface not in SURFACES
        ):
            raise ConfigurationError("Milestone name and kind must contain 1-256 characters, with a known surface.")

    def _start(
        self, name: str, details: MilestoneContext, identity: Attributes | None = None
    ) -> tuple[Span, otel_context.Context]:
        performance = None
        if details.performance is not None:
            try:
                performance = performance_attributes(details.performance, self._runtime.options.input.hash_key)
            except ConfigurationError:
                self._runtime.report("invalid_performance", "context")
        base = otel_context.get_current()
        state = self._runtime.state(base)
        if state is None:
            external = trace.get_current_span(base).get_span_context()
            links = [Link(clean_context(external))] if external.is_valid else []
            state = RequestState(links=links)
            # Its own managed scope: an unrelated caller's trace is a link, never the parent.
            base = otel_context.set_value(self._runtime.state_key, state, otel_context.Context())
        mark = Mark(name=name, details=details, performance=performance, identity=identity)
        marked = otel_context.set_value(self._runtime.mark_key, mark, base)
        parented = trace.get_current_span(base).get_span_context().is_valid
        span = self._tracer.start_span(name, context=marked, links=[] if parented else state.links)
        if not span.is_recording():
            self._runtime.report("entry_not_recording", "traces")
        return span, trace.set_span_in_context(span, base)

    # Logs

    def log(self, entry: StructuredLog) -> None:
        """Record a structured log, correlated with the current managed span."""
        if suppressed():
            return
        state = self._runtime.state()
        if state is None and self._runtime.options.input.logs.exclude_uncorrelated:
            return
        span = None
        if state is not None:
            context = trace.get_current_span().get_span_context()
            span = context if context.is_valid else None
        now = time.time_ns()
        self._logs.add(copy_log(entry, now, now, span, self._runtime))

    # Delivery

    def flush(self, timeout: float | None = None) -> None:
        """Wait for delivery attempts of what was recorded so far. Not a delivery guarantee."""
        limit = self._runtime.options.queue.flush_timeout if timeout is None else timeout
        deadline = time.monotonic() + limit
        self._spans.flush(limit)
        self._logs.flush(max(0.0, deadline - time.monotonic()))

    def shutdown(self) -> None:
        """Drain once and stop. Terminal and idempotent."""
        with self._shutdown_lock:
            if self._closed:
                return
            self._closed = True
        deadline = time.monotonic() + self._runtime.options.queue.shutdown_timeout
        self._spans.shutdown()
        self._logs.shutdown(max(0.0, deadline - time.monotonic()))
        for stop in self._on_shutdown:
            stop()

    def diagnostics(self) -> Diagnostics:
        return Diagnostics(self._spans.diagnostics(), self._logs.diagnostics())

    # HTTP

    def asgi(self, app: ASGIApp) -> AgentrailASGIMiddleware:
        """Wrap an ASGI application (FastAPI, Starlette, Django ASGI, ...)."""
        from .asgi import AgentrailASGIMiddleware

        return AgentrailASGIMiddleware(app, client=self)

    def wsgi(self, app: WSGIApp) -> AgentrailWSGIMiddleware:
        """Wrap a WSGI application (Flask, Django WSGI, ...)."""
        from .wsgi import AgentrailWSGIMiddleware

        return AgentrailWSGIMiddleware(app, client=self)

    def _managed(self) -> bool:
        return not self._closed and not suppressed() and self._runtime.state() is not None

    def _enter(self, request: IncomingRequest) -> tuple[Span, otel_context.Context] | None:
        """Admit an incoming request: verify its handoff, then open its entry span."""
        if self._closed or suppressed():
            return None
        ignore = self._runtime.options.input.ignore_incoming_request
        if ignore is not None and ignore(request):
            return None
        parent = request.headers.get("traceparent")
        token = request.headers.get(HANDOFF_HEADER)
        now = time.time_ns() // 1_000_000
        state = RequestState()
        ctx = otel_context.Context()
        try:
            remote = verify(self._runtime.options.input.hash_key, token, parent, request.method, request.target, now)
        except Refused as refused:
            external = parse_traceparent(parent)
            if external is not None:
                state.links.append(
                    Link(SpanContext(external.trace_id, external.span_id, True, TraceFlags(external.flags & 1)))
                )
            if refused.reason != "missing":
                self._runtime.report("handoff_refused", "context")
        else:
            state.verified = True
            remote_context = SpanContext(remote.trace_id, remote.span_id, True, TraceFlags(remote.flags & 1))
            ctx = trace.set_span_in_context(NonRecordingSpan(remote_context), ctx)
        state.location = self._location(request)
        ctx = otel_context.set_value(self._runtime.state_key, state, ctx)
        attributes: Attributes = {
            "http.request.method": request.method,
            "url.full": f"{request.scheme}://{request.host}{request.target}",
        }
        user_agent = request.headers.get("user-agent")
        if user_agent:
            attributes["user_agent.original"] = user_agent
        span = self._tracer.start_span(
            f"HTTP {request.method}", context=ctx, kind=SpanKind.SERVER, attributes=attributes
        )
        if not span.is_recording():
            self._runtime.report("entry_not_recording", "traces")
        return span, trace.set_span_in_context(span, ctx)

    def _location(self, request: IncomingRequest) -> Attributes:
        hook = self._runtime.options.input.incoming_location
        if hook is None:
            return {}
        try:
            location = hook(request)
        except Exception:  # noqa: BLE001 - the application's lookup must never fail its request
            self._runtime.report("location_failed", "context")
            return {}
        if location is None:
            return {}
        places = (location.country, location.region, location.city)
        if (
            any(place is not None and not isinstance(place, str) for place in places)  # pyright: ignore[reportUnnecessaryIsInstance]
            or (location.country is not None and _COUNTRY.fullmatch(location.country) is None)
            or location.source not in LOCATION_SOURCES
        ):
            self._runtime.report("location_failed", "context")
            return {}
        attributes: Attributes = {}
        for name, value in zip(("country", "region", "city"), places, strict=True):
            text = "" if value is None else bounded(value, 128)
            if text:
                attributes[f"agentrail.client.location.{name}"] = text
        if attributes:
            attributes["agentrail.client.location.source"] = location.source
        return attributes
