# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""MCP steps for the official ``mcp`` Python SDK (2.x, ``MCPServer``).

Wrap the functions you register with a stable registered name; add the
middleware so each step knows the calling client and protocol::

    capture = mcp_capture(agentrail)
    server = MCPServer("orders", middleware=[capture.middleware])
    server.tool(name="checkout")(capture.tool("checkout", checkout))

Arguments, results, prompt text and resource URIs are never recorded. Client
details come from the client and are descriptive only: they never assert an
identity.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from ._options import McpContext, MilestoneContext
from ._text import bounded

if TYPE_CHECKING:
    from ._client import Client

F = TypeVar("F", bound=Callable[..., Any])

_request: contextvars.ContextVar[tuple[str | None, str | None, str | None] | None] = contextvars.ContextVar(
    "agentrail_python_mcp_request", default=None
)


def _text(value: object, limit: int) -> str | None:
    return bounded(value, limit) or None if isinstance(value, str) else None


def _span_name(method: str, name: str) -> str:
    return bounded(f"mcp.{method}:{name}", 256)


def _is_error(result: object) -> bool:
    if isinstance(result, Mapping):
        return cast(Mapping[str, object], result).get("isError") is True
    return getattr(result, "is_error", None) is True or getattr(result, "isError", None) is True


class McpCapture:
    def __init__(self, client: Client) -> None:
        self._client = client

    async def middleware(self, ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        """``ServerMiddleware``: keeps this request's client name, version and protocol."""
        info = getattr(getattr(getattr(ctx, "session", None), "client_params", None), "client_info", None)
        token = _request.set(
            (
                _text(getattr(info, "name", None), 128),
                _text(getattr(info, "version", None), 128),
                _text(getattr(ctx, "protocol_version", None), 64),
            )
        )
        try:
            return await call_next(ctx)
        finally:
            _request.reset(token)

    def tool(self, name: str, handler: F) -> F:
        """Wrap a tool function. A result with ``isError`` marks the step failed."""
        return self._wrap("tools/call", "tool.call", name, handler)

    def resource(self, name: str, handler: F) -> F:
        """Wrap a resource function. Pass the registered name, never the requested URI."""
        return self._wrap("resources/read", "mcp.resources/read", name, handler)

    def prompt(self, name: str, handler: F) -> F:
        """Wrap a prompt function."""
        return self._wrap("prompts/get", "mcp.prompts/get", name, handler)

    def _details(self, method: str, kind: str) -> MilestoneContext:
        client, version, protocol = _request.get() or (None, None, None)
        return MilestoneContext(
            surface="mcp",
            kind=kind,
            mcp=McpContext(method=method, client=client, client_version=version, protocol=protocol),
        )

    def _wrap(self, method: str, kind: str, name: str, handler: F) -> F:
        label = _span_name(method, name)
        client = self._client

        def mark_result(result: object) -> None:
            if _is_error(result):
                span = trace.get_current_span()
                span.set_status(StatusCode.ERROR)
                span.set_attribute("error.type", "McpOperationError")

        if inspect.iscoroutinefunction(handler):

            @functools.wraps(handler)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                async with client.start_milestone(label, self._details(method, kind)):
                    result = await handler(*args, **kwargs)
                    mark_result(result)
                    return result

            return cast(F, run_async)

        @functools.wraps(handler)
        def run(*args: Any, **kwargs: Any) -> Any:
            with client.start_milestone(label, self._details(method, kind)):
                result = handler(*args, **kwargs)
                mark_result(result)
                return result

        return cast(F, run)


def mcp_capture(client: Client) -> McpCapture:
    return McpCapture(client)
