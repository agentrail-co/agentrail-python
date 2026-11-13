# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""MCP for the official ``mcp`` Python SDK (2.x, ``MCPServer``).

Add the middleware first in your list. Each JSON-RPC message the server
receives (``initialize``, ``tools/list``, ``tools/call``, notifications, and
requests answered with a JSON-RPC error) is then one step, with its method,
outcome, message sizes, the calling client and the hashed session::

    capture = mcp_capture(agentrail)
    server = MCPServer("orders", middleware=[capture.middleware])
    server.tool(name="checkout")(capture.tool("checkout", checkout))

The wrappers are optional. Inside the middleware's step a wrapped function is
its child span, never a second step; without the middleware it is the step,
with the session hash when the function receives the MCPServer ``Context``.

Arguments, results and prompt text are never recorded unless
``capture_payloads`` is set:
``mcp_capture(agentrail, capture_payloads=CapturePayloads(redact=...))``. A
step's name carries the requested tool, resource URI or prompt. Client details
come from the client and are descriptive only: they never assert an identity.

The session hash is the keyed hash of the ``Mcp-Session-Id`` the server
issued, on every message of a stateful streamable HTTP session, the opening
``initialize`` included. stdio, stateless HTTP and the 2026-07-28 protocol
have no session, and send none.
"""

from __future__ import annotations

import contextvars
import copy
import functools
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Span, StatusCode
from pydantic import BaseModel, ValidationError

from ._client import MilestoneScope
from ._options import CapturePayloads, McpPayload, MilestoneContext
from ._queue import BATCH_BYTES
from ._runtime import McpEvidence
from ._text import bounded, well_formed

if TYPE_CHECKING:
    from ._client import Client

F = TypeVar("F", bound=Callable[..., Any])

# What the collector accepts as a method; another is named in the span only.
_METHOD = re.compile(r"[A-Za-z0-9_./-]{1,128}")
_TARGETS = {"tools/call": "name", "resources/read": "uri", "prompts/get": "name"}
# One record's payloads, with room for the rest of the span within one export.
_PAYLOAD_BYTES = BATCH_BYTES - 128 * 1024

_transport: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "agentrail_python_mcp_transport", default=None
)


def _text(value: object, limit: int) -> str | None:
    return bounded(value, limit) or None if isinstance(value, str) else None


def _span_name(method: str, name: str | None) -> str:
    return bounded(f"mcp.{method}:{name}" if name else f"mcp.{method}", 256)


def _is_error(result: object) -> bool:
    if isinstance(result, Mapping):
        return cast(Mapping[str, object], result).get("isError") is True
    return getattr(result, "is_error", None) is True or getattr(result, "isError", None) is True


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _byte_length(message: object) -> int | None:
    try:
        return len(_compact(message).encode("utf-8", "surrogatepass"))
    except (TypeError, ValueError):  # not JSON: a later middleware's own result object
        return None


def _payload_text(value: object) -> str:
    text = _compact(value)
    if not well_formed(text):
        # A lone surrogate cannot travel as text; escaped, it is the same JSON.
        text = json.dumps(value, separators=(",", ":"))
    return text


def _cut(text: str, limit: int) -> str:
    """The longest prefix of at most ``limit`` UTF-8 bytes; never half a character."""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _fit_payloads(
    request: str | None, response: str | None, limit: int = _PAYLOAD_BYTES
) -> tuple[str | None, bool, str | None, bool]:
    """Both payloads within ``limit`` bytes: whole when they fit, else cut and flagged truncated."""
    sizes = [0 if text is None else len(text.encode("utf-8")) for text in (request, response)]
    if sizes[0] + sizes[1] <= limit:
        return request, False, response, False
    half = limit // 2
    if sizes[0] <= half:
        caps = (sizes[0], limit - sizes[0])
    elif sizes[1] <= half:
        caps = (limit - sizes[1], sizes[1])
    else:
        caps = (half, limit - half)
    cut_request = request is not None and sizes[0] > caps[0]
    cut_response = response is not None and sizes[1] > caps[1]
    return (
        _cut(request, caps[0]) if request is not None and cut_request else request,
        cut_request,
        _cut(response, caps[1]) if response is not None and cut_response else response,
        cut_response,
    )


def _wire(result: object) -> object:
    """A result as the SDK puts it on the wire."""
    if result is None:
        return {}
    if isinstance(result, BaseModel):
        return result.model_dump(by_alias=True, mode="json", exclude_none=True)
    return result


def _client_details(ctx: Any, method: str, params: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    if method == "initialize" and params is not None:
        # The handshake commits after the middleware: the client describes itself here.
        info = params.get("clientInfo")
        if isinstance(info, Mapping):
            details = cast(Mapping[str, object], info)
            return _text(details.get("name"), 128), _text(details.get("version"), 128)
        return None, None
    info = getattr(getattr(getattr(ctx, "session", None), "client_params", None), "client_info", None)
    return _text(getattr(info, "name", None), 128), _text(getattr(info, "version", None), 128)


class McpCapture:
    def __init__(self, client: Client, *, capture_payloads: CapturePayloads | None = None) -> None:
        self._client = client
        self._payloads = capture_payloads

    async def middleware(self, ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        """``ServerMiddleware``: one transport step per JSON-RPC message received.

        Put it first in ``middleware=[...]``. The application's exceptions and
        results pass through unchanged.
        """
        method = ctx.method if isinstance(ctx.method, str) else ""
        raw = ctx.params
        params = cast(Mapping[str, Any], raw) if isinstance(raw, Mapping) else None
        target = None
        if method in _TARGETS and params is not None:
            value = params.get(_TARGETS[method])
            target = value if isinstance(value, str) and value else None
        request_id = ctx.request_id
        client_name, client_version = _client_details(ctx, method, params)
        evidence = McpEvidence(
            layer="transport",
            method=method if _METHOD.fullmatch(method) else None,
            client=client_name,
            client_version=client_version,
            protocol=_text(getattr(ctx, "protocol_version", None), 64),
            session_hash=self._session_hash(getattr(ctx, "session", None)),
        )
        message: dict[str, Any] = {"jsonrpc": "2.0"}
        if request_id is not None:
            message["id"] = request_id
        message["method"] = method
        if params is not None:
            message["params"] = dict(params)
        evidence.request_bytes = _byte_length(message)
        kind = "tool.call" if method == "tools/call" else bounded(f"mcp.{method}", 256)
        span, context = self._client._start(  # pyright: ignore[reportPrivateUsage]
            _span_name(method, target), MilestoneContext(surface="mcp", kind=kind), mcp=evidence
        )
        token = otel_context.attach(context)
        inside = _transport.set(self._client)
        response: object = None
        try:
            try:
                result = await call_next(ctx)
            except MCPError as error:
                response = error.error.model_dump(by_alias=True, mode="json", exclude_none=True)
                self._json_rpc_error(span, evidence, request_id, response, error.error.code)
                raise
            except ValidationError:
                # As the SDK answers it: INVALID_PARAMS, without pydantic's text.
                response = {"code": INVALID_PARAMS, "message": "Invalid request parameters", "data": ""}
                self._json_rpc_error(span, evidence, request_id, response, INVALID_PARAMS)
                raise
            except BaseException as error:
                span.set_status(StatusCode.ERROR)
                span.set_attribute("error.type", bounded(type(error).__qualname__, 4096))
                raise
            wire = _wire(result)
            if request_id is not None:
                response = wire
                evidence.response_bytes = _byte_length({"jsonrpc": "2.0", "id": request_id, "result": wire})
            if method == "tools/call" and _is_error(wire):
                evidence.is_error = True
                span.set_status(StatusCode.ERROR)
                span.set_attribute("error.type", "McpOperationError")
            if method == "initialize" and isinstance(wire, Mapping):
                negotiated = _text(cast(Mapping[str, object], wire).get("protocolVersion"), 64)
                evidence.protocol = negotiated or evidence.protocol
            return result
        finally:
            _transport.reset(inside)
            try:
                if self._payloads is not None:
                    self._capture(self._payloads, evidence, method, target, params, response)
                span.end()
            finally:
                otel_context.detach(token)

    def tool(self, name: str, handler: F) -> F:
        """Wrap a tool function. A result with ``isError`` marks the span failed."""
        return self._wrap("tools/call", "tool.call", name, handler)

    def resource(self, name: str, handler: F) -> F:
        """Wrap a resource function. Pass the registered name, never the requested URI."""
        return self._wrap("resources/read", "mcp.resources/read", name, handler)

    def prompt(self, name: str, handler: F) -> F:
        """Wrap a prompt function."""
        return self._wrap("prompts/get", "mcp.prompts/get", name, handler)

    def _json_rpc_error(
        self, span: Span, evidence: McpEvidence, request_id: object, error: dict[str, Any], code: int
    ) -> None:
        evidence.error_code = code
        if request_id is not None:
            evidence.response_bytes = _byte_length({"jsonrpc": "2.0", "id": request_id, "error": error})
        span.set_status(StatusCode.ERROR)
        span.set_attribute("error.type", "McpJsonRpcError")

    def _session_hash(self, session: object) -> str | None:
        # The id the server issued, from the connection: the opening initialize
        # has it before the response header does, and a stateless server, which
        # issues none, never hashes an id a client made up. mcp 2.3 has no public
        # path to it from a ServerSession; if a later release moves it, no hash.
        connection = getattr(session, "_connection", None)
        session_id = getattr(connection, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            return None
        return self._client._keyed_hash(session_id)  # pyright: ignore[reportPrivateUsage]

    def _capture(
        self,
        settings: CapturePayloads,
        evidence: McpEvidence,
        method: str,
        name: str | None,
        params: Mapping[str, Any] | None,
        response: object,
    ) -> None:
        payload: McpPayload = {
            "method": method,
            "name": name,
            "request": None if params is None else copy.deepcopy(dict(params)),
            "response": copy.deepcopy(response),
        }
        try:
            chosen = settings.redact(payload)
            if chosen is None:
                return
            request, answer = chosen.get("request"), chosen.get("response")
            request_text = None if request is None else _payload_text(request)
            response_text = None if answer is None else _payload_text(answer)
        except Exception:  # noqa: BLE001 - the application's hook, or what it returned, must never fail the MCP call
            self._client._report("payload_redact_failed", "traces")  # pyright: ignore[reportPrivateUsage]
            return
        (
            evidence.request_payload,
            evidence.request_truncated,
            evidence.response_payload,
            evidence.response_truncated,
        ) = _fit_payloads(request_text, response_text)

    def _scope(self, label: str, method: str, kind: str, arguments: tuple[object, ...]) -> MilestoneScope:
        details = MilestoneContext(surface="mcp", kind=kind)
        evidence = McpEvidence(layer="handler", method=method)
        # Inside this client's transport step the handler is its child, not a second step.
        step = _transport.get() is not self._client
        if step:
            evidence.session_hash = self._handler_session(arguments)
        return MilestoneScope(self._client, label, details, step=step, mcp=evidence)

    def _handler_session(self, arguments: tuple[object, ...]) -> str | None:
        """The session hash, when the function receives the MCPServer ``Context``."""
        for value in arguments:
            if isinstance(value, Context):
                try:
                    session = cast(Context[Any, Any], value).session
                except ValueError:  # a Context built outside a request
                    return None
                return self._session_hash(session)
        return None

    def _wrap(self, method: str, kind: str, name: str, handler: F) -> F:
        label = _span_name(method, name)

        def mark_result(result: object) -> None:
            if _is_error(result):
                span = trace.get_current_span()
                span.set_status(StatusCode.ERROR)
                span.set_attribute("error.type", "McpOperationError")

        if inspect.iscoroutinefunction(handler):

            @functools.wraps(handler)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                async with self._scope(label, method, kind, (*args, *kwargs.values())):
                    result = await handler(*args, **kwargs)
                    mark_result(result)
                    return result

            return cast(F, run_async)

        @functools.wraps(handler)
        def run(*args: Any, **kwargs: Any) -> Any:
            with self._scope(label, method, kind, (*args, *kwargs.values())):
                result = handler(*args, **kwargs)
                mark_result(result)
                return result

        return cast(F, run)


def mcp_capture(client: Client, *, capture_payloads: CapturePayloads | None = None) -> McpCapture:
    """MCP capture for ``client``. Payloads are sent only with ``capture_payloads``."""
    return McpCapture(client, capture_payloads=capture_payloads)
