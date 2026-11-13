# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""MCP with the official SDK: one transport step per message, handlers as its
children, and content only when the application opts in.

Attribute names and expected shapes come from the contract's
``otlp.mcpAttributes`` and its ``sdk-mcp-*`` fixtures, never copied by hand.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from typing import Any

import anyio
import httpx2
import pytest
from mcp.client.client import Client as McpClient
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    TextContent,
)

from agentrail_python import CapturePayloads, McpPayload
from agentrail_python._handoff import keyed_hash
from agentrail_python._queue import BATCH_BYTES
from agentrail_python.mcp import _PAYLOAD_BYTES, mcp_capture
from support import BODY_BYTES, CONTRACT, HASH_KEY, by_name, recording

OTLP = CONTRACT["otlp"]
MCP = OTLP["mcpAttributes"]
NAMED = {*OTLP["spanAttributes"].values(), *MCP.values(), *OTLP["locationAttributes"].values()}
ACCEPTED = {fixture["name"]: fixture for fixture in OTLP["fixtures"]["accepted"]}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def fixture_spans(name: str) -> list[dict[str, Any]]:
    """The fixture's spans as ``{"status": code, "attributes": {key: value}}``."""
    spans = ACCEPTED[name]["request"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    out: list[dict[str, Any]] = []
    for span in spans:
        values = {item["key"]: next(iter(item["value"].values())) for item in span.get("attributes", [])}
        out.append({"status": span.get("status", {}).get("code", 0), "attributes": values})
    return out


def compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def transports(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [span for span in spans if span["attributes"].get(MCP["layer"]) == "transport"]


def transport(spans: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [span for span in transports(spans) if span["name"] == name]
    assert len(matches) == 1, [span["name"] for span in spans]
    return matches[0]


def assert_named(spans: list[dict[str, Any]]) -> None:
    for span in spans:
        unnamed = {key for key in span["attributes"] if key.startswith("agentrail.") and key not in NAMED}
        assert not unnamed, (span["name"], unnamed)


async def refuse_listing(ctx: Any, call_next: Any) -> Any:
    """An application middleware after Agentrail's that raises an ordinary exception."""
    if ctx.method == "prompts/list":
        raise PermissionError("synthetic refusal")
    if ctx.method == "synthetic/huge-code":
        raise MCPError(code=2**31, message="synthetic code past int32")
    return await call_next(ctx)


def server(capture: Any, *, middleware: bool = True, size: int = 0) -> MCPServer:
    app = MCPServer("orders", middleware=[capture.middleware, refuse_listing] if middleware else [refuse_listing])

    def checkout(item: str) -> str:
        return f"ordered {item}"

    async def refund(order: str) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text="no")], is_error=True)

    def synthetic_tool(q: str) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=f"synthetic failure for {q}")], is_error=True)

    def big() -> str:
        return "x" * size

    def catalog() -> str:
        return "private catalog body"

    def greet(name: str) -> str:
        return f"Hello {name}, private prompt"

    app.tool(name="checkout")(capture.tool("checkout", checkout))
    app.tool(name="refund")(capture.tool("refund", refund))
    app.tool(name="synthetic_tool")(capture.tool("synthetic_tool", synthetic_tool))
    app.tool(name="big")(capture.tool("big", big))
    app.resource("catalog://items/sku", name="catalog")(capture.resource("catalog", catalog))

    def ratio() -> float:
        return 1e-7

    def whoami(ctx: Context) -> str:
        return "synthetic"

    def confirm(ctx: Context) -> Any:
        # A multi-round-trip tool: the SDK seals this state on the wire.
        if ctx.request_state is None:
            form = ElicitRequestFormParams(message="ok?", requested_schema={"type": "object", "properties": {}})
            return InputRequiredResult(
                input_requests={"ok": ElicitRequest(params=form)}, request_state="plaintext-secret-state"
            )
        return "done"

    app.prompt(name="greet")(capture.prompt("greet", greet))
    app.tool(name="whoami")(capture.tool("whoami", whoami))
    app.tool(name="ratio")(capture.tool("ratio", ratio))
    app.tool(name="confirm")(capture.tool("confirm", confirm))
    return app


async def json_rpc(
    http: httpx2.AsyncClient, headers: Mapping[str, str], message: dict[str, Any]
) -> tuple[httpx2.Response, Any]:
    response = await http.post("/mcp", content=compact(message), headers=dict(headers))
    return response, response.json() if response.content else None


def stateful_session(
    rec: Any,
    messages: list[dict[str, Any]],
    *,
    middleware: bool = True,
    requested: str = "2025-06-18",
    sizes: list[int] | None = None,
) -> tuple[str, list[Any]]:
    """Initialize a stateful streamable HTTP session behind the ASGI wrapper, then send ``messages``."""
    capture = mcp_capture(rec.client)
    app = server(capture, middleware=middleware)
    asgi = rec.client.asgi(app.streamable_http_app(json_response=True))
    answers: list[Any] = []
    session: list[str] = []

    async def run() -> None:
        async with app.session_manager.run():
            transport = httpx2.ASGITransport(app=asgi)
            async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8000") as http:
                initialize = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": requested,
                        "capabilities": {},
                        "clientInfo": {"name": "synthetic-client", "version": "1.0.0"},
                    },
                }
                response, body = await json_rpc(http, HEADERS, initialize)
                answers.append(body)
                session.append(response.headers["mcp-session-id"])
                headers = {
                    **HEADERS,
                    "mcp-session-id": session[0],
                    "mcp-protocol-version": body["result"]["protocolVersion"],
                }
                await json_rpc(http, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                for message in messages:
                    response, body = await json_rpc(http, headers, message)
                    answers.append(body)
                    if sizes is not None:
                        sizes.append(len(response.content))

    anyio.run(run)
    return session[0], answers


# Fails if someone counts a tool call twice (handler and transport both steps),
# records arguments, results or prompt text with capture off, drops the client's
# details or the isError outcome, or emits an attribute the contract does not name.
def test_one_step_per_message_and_no_content_by_default() -> None:
    with recording() as rec:
        app = server(mcp_capture(rec.client))

        async def run() -> None:
            async with McpClient(app) as client:
                result = await client.call_tool("checkout", {"item": "secret-item"})
                assert "ordered secret-item" in repr(result)
                await client.call_tool("refund", {"order": "o-1"})
                await client.read_resource("catalog://items/sku")
                await client.get_prompt("greet", {"name": "secret-name"})

        anyio.run(run)
        spans = rec.spans()
        steps = [span for span in spans if span["attributes"].get("agentrail.step") is True]
        assert [span["name"] for span in steps].count("mcp.tools/call:checkout") == 1
        tool = transport(spans, "mcp.tools/call:checkout")
        assert (
            tool["attributes"].items()
            >= {
                "agentrail.step.surface": "mcp",
                "agentrail.step.kind": "tool.call",
                MCP["method"]: "tools/call",
                MCP["clientName"]: "mcp",
                MCP["protocolVersion"]: "2026-07-28",
            }.items()
        )
        assert tool["attributes"][MCP["requestBytes"]] > 0 and tool["attributes"][MCP["responseBytes"]] > 0
        handler = [span for span in spans if span["parent_span_id"] == tool["span_id"]]
        assert len(handler) == 1 and handler[0]["name"] == "mcp.tools/call:checkout"
        assert handler[0]["attributes"] == fixture_spans("sdk-mcp-transport-with-handler")[1]["attributes"]

        failed = fixture_spans("sdk-mcp-tool-error-with-payload")[0]
        refund = transport(spans, "mcp.tools/call:refund")
        assert refund["status"] == failed["status"]
        assert refund["attributes"][MCP["isError"]] is True
        assert refund["attributes"]["error.type"] == failed["attributes"]["error.type"]
        assert transport(spans, "mcp.resources/read:catalog://items/sku")["attributes"]["agentrail.step.kind"] == (
            "mcp.resources/read"
        )
        assert by_name(spans, "mcp.resources/read:catalog")["attributes"][MCP["layer"]] == "handler"
        assert transport(spans, "mcp.prompts/get:greet")["attributes"]["agentrail.step.kind"] == "mcp.prompts/get"
        assert_named(spans)
        for span in spans:
            assert not {MCP["requestPayload"], MCP["responsePayload"]} & span["attributes"].keys()
        for secret in ("secret-item", "secret-name", "private catalog", "private prompt", "o-1"):
            assert secret not in repr(spans)


# Fails if someone drops the session hash on stateful streamable HTTP (the
# opening initialize included), hashes it with anything but the principal hash,
# sends it without the resource's key ID (the contract's refused
# mcp-session-hash-without-key-id), names a layer the contract does not
# (mcp-unknown-layer), misses a notification, or measures a message other than
# as its compact JSON.
def test_stateful_http_session_and_sizes() -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "checkout", "arguments": {"item": "x"}},
    }
    with recording() as rec:
        session, answers = stateful_session(rec, [call])
        spans = rec.spans()
        expected = keyed_hash(HASH_KEY, session)
        initialize = transport(spans, "mcp.initialize")
        assert initialize["attributes"][MCP["sessionHash"]] == expected
        assert initialize["attributes"][MCP["clientName"]] == "synthetic-client"
        assert initialize["attributes"][MCP["responseBytes"]] == len(compact(answers[0]).encode())
        notified = transport(spans, "mcp.notifications/initialized")
        assert notified["attributes"][MCP["sessionHash"]] == expected
        assert MCP["responseBytes"] not in notified["attributes"]
        assert notified["attributes"][MCP["requestBytes"]] == len(
            compact({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
        )

        tool = transport(spans, "mcp.tools/call:checkout")
        wanted = fixture_spans("sdk-mcp-transport-with-handler")[0]["attributes"]
        assert tool["attributes"].keys() == wanted.keys()
        assert tool["attributes"][MCP["sessionHash"]] == expected
        assert tool["attributes"][MCP["requestBytes"]] == len(compact(call).encode())
        assert tool["attributes"][MCP["responseBytes"]] == len(compact(answers[1]).encode())
        assert tool["resource"][OTLP["resourceAttributes"]["hashKeyId"]]
        assert {span["attributes"][MCP["layer"]] for span in spans if MCP["layer"] in span["attributes"]} == {
            "transport",
            "handler",
        }
        entries = {span["span_id"] for span in spans if span["name"] == "HTTP POST"}
        assert all(span["parent_span_id"] in entries for span in transports(spans))
        assert_named(spans)


# Fails if someone loses the JSON-RPC error code (METHOD_NOT_FOUND, or params
# validation), sends a method outside the collector's pattern, swallows or
# rewrites the error the client receives, or marks it other than the
# contract's fixture does.
def test_json_rpc_errors_keep_their_code() -> None:
    unknown = {"jsonrpc": "2.0", "id": 2, "method": "no/such"}
    invalid = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}}
    unnamed = {"jsonrpc": "2.0", "id": 4, "method": "bad method!"}
    with recording() as rec:
        _, answers = stateful_session(rec, [unknown, invalid, unnamed])
        spans = rec.spans()
        # A method the collector would refuse is named in the span only.
        assert MCP["method"] not in transport(spans, "mcp.bad method!")["attributes"]
        assert answers[1]["error"]["code"] == METHOD_NOT_FOUND
        assert answers[2]["error"]["code"] == INVALID_PARAMS
        wanted = fixture_spans("sdk-mcp-jsonrpc-error")[0]
        for name, answer in (("mcp.no/such", answers[1]), ("mcp.tools/call", answers[2])):
            span = transport(spans, name)
            assert span["attributes"][MCP["errorCode"]] == answer["error"]["code"]
            assert span["attributes"][MCP["responseBytes"]] == len(compact(answer).encode())
            assert span["status"] == wanted["status"]
            assert span["attributes"]["error.type"] == wanted["attributes"]["error.type"]
        assert transport(spans, "mcp.tools/call")["attributes"].keys() == wanted["attributes"].keys()
        assert_named(spans)


# Fails if someone breaks the wrappers without the middleware: each call must
# stay a step of its own, naming its layer and method, and an isError result
# must say so as a transport step would.
def test_handler_alone_is_the_step() -> None:
    with recording() as rec:
        app = server(mcp_capture(rec.client), middleware=False)

        async def run() -> None:
            async with McpClient(app) as client:
                await client.call_tool("checkout", {"item": "x"})
                await client.call_tool("refund", {"order": "o-1"})

        anyio.run(run)
        spans = rec.spans()
        refund = by_name(spans, "mcp.tools/call:refund")
        assert refund["attributes"][MCP["isError"]] is True and refund["status"] == 2
        assert MCP["isError"] not in by_name(spans, "mcp.tools/call:checkout")["attributes"]
        tool = by_name(spans, "mcp.tools/call:checkout")
        assert (
            tool["attributes"].items()
            >= {"agentrail.step": True, "agentrail.step.kind": "tool.call", MCP["layer"]: "handler"}.items()
        )
        assert tool["attributes"][MCP["method"]] == "tools/call"
        assert transports(spans) == []


def call_with_payloads(redact: Any, tool: str, arguments: dict[str, Any], size: int = 0, calls: int = 1) -> Any:
    with recording() as rec:
        capture = mcp_capture(rec.client, capture_payloads=CapturePayloads(redact=redact))
        app = server(capture, size=size)
        results: list[Any] = []

        async def run() -> None:
            async with McpClient(app) as client:
                for _ in range(calls):
                    results.append(await client.call_tool(tool, arguments))

        anyio.run(run)
        return rec.spans(), rec.codes(), results


# Fails if someone sends payloads without the hook's say, hands the hook the
# live result (so redacting changes the client's answer), or encodes them other
# than as compact JSON text.
def test_payloads_after_redaction() -> None:
    seen: list[McpPayload] = []
    wanted = fixture_spans("sdk-mcp-tool-error-with-payload")[0]["attributes"]

    def redact(payload: McpPayload) -> Mapping[str, Any] | None:
        if payload["method"] != "tools/call":
            return None
        seen.append(payload)
        payload["response"]["content"][0]["text"] = "replaced"
        return {"request": {"name": payload["name"], "arguments": {"q": "[redacted]"}}, "response": payload["response"]}

    spans, _, results = call_with_payloads(redact, "synthetic_tool", {"q": "secret-question"})
    assert seen[0]["method"] == "tools/call" and seen[0]["name"] == "synthetic_tool"
    assert seen[0]["request"]["arguments"] == {"q": "secret-question"}
    assert "synthetic failure for secret-question" in repr(results[0])
    tool = transport(spans, "mcp.tools/call:synthetic_tool")
    assert tool["attributes"][MCP["requestPayload"]] == wanted[MCP["requestPayload"]]
    assert json.loads(tool["attributes"][MCP["responsePayload"]])["content"][0]["text"] == "replaced"
    assert MCP["requestTruncated"] not in tool["attributes"] and MCP["responseTruncated"] not in tool["attributes"]
    assert "secret-question" not in repr(spans)


# Fails if someone exports a payload when the hook returns None.
def test_redact_none_sends_nothing() -> None:
    spans, _, _ = call_with_payloads(lambda payload: None, "checkout", {"item": "secret-item"})
    tool = transport(spans, "mcp.tools/call:checkout")
    assert not {MCP["requestPayload"], MCP["responsePayload"]} & tool["attributes"].keys()
    assert "secret-item" not in repr(spans)


# Fails if a raising hook breaks the MCP call, sends anything, or goes unreported.
def test_failing_redact_is_reported_and_the_call_answers() -> None:
    def redact(payload: McpPayload) -> Mapping[str, Any]:
        raise RuntimeError("synthetic redact failure")

    spans, codes, results = call_with_payloads(redact, "checkout", {"item": "secret-item"})
    assert "ordered secret-item" in repr(results[0])
    assert "secret-item" not in repr(spans)
    assert "payload_redact_failed" in codes
    assert transport(spans, "mcp.tools/call:checkout")["attributes"][MCP["responseBytes"]] > 0


# Fails if a hook that returns something JSON cannot encode sends anything,
# breaks the call, or is reported under any code but the shared one.
def test_unencodable_redact_result_is_reported() -> None:
    spans, codes, results = call_with_payloads(
        lambda payload: {"request": object()} if payload["method"] == "tools/call" else None, "checkout", {"item": "x"}
    )
    assert "ordered x" in repr(results[0])
    assert (
        not {MCP["requestPayload"], MCP["responsePayload"]}
        & transport(spans, "mcp.tools/call:checkout")["attributes"].keys()
    )
    assert codes.count("payload_redact_failed") == 1


# Fails if a handler that is the step (no middleware) drops the session hash
# when its function receives the MCPServer Context.
def test_handler_step_keeps_the_session_hash() -> None:
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "whoami", "arguments": {}}}
    with recording() as rec:
        session, _ = stateful_session(rec, [call], middleware=False)
        tool = by_name(rec.spans(), "mcp.tools/call:whoami")
        assert tool["attributes"]["agentrail.step"] is True
        assert tool["attributes"][MCP["sessionHash"]] == keyed_hash(HASH_KEY, session)
        assert (
            tool["attributes"].items()
            >= {
                MCP["clientName"]: "synthetic-client",
                MCP["clientVersion"]: "1.0.0",
                MCP["protocolVersion"]: "2025-06-18",
            }.items()
        )
        assert_named([tool])


# Fails if an ordinary exception, which the SDK still answers as a JSON-RPC
# error (code 0 on a handshake-era connection), is recorded without that code,
# its size, or the contract's error type.
def test_ordinary_exception_is_a_json_rpc_error() -> None:
    listing = {"jsonrpc": "2.0", "id": 2, "method": "prompts/list"}
    with recording() as rec:
        _, answers = stateful_session(rec, [listing])
        span = transport(rec.spans(), "mcp.prompts/list")
        wanted = fixture_spans("sdk-mcp-jsonrpc-error")[0]["attributes"]["error.type"]
        assert span["attributes"][MCP["errorCode"]] == answers[1]["error"]["code"] == 0
        assert span["attributes"]["error.type"] == wanted and span["status"] == 2
        assert span["attributes"][MCP["responseBytes"]] == len(compact(answers[1]).encode())


# Fails if someone sends an error code past 32 bits, which the collector
# refuses with the whole export, instead of leaving it out.
def test_error_code_past_int32_is_left_out() -> None:
    huge = {"jsonrpc": "2.0", "id": 2, "method": "synthetic/huge-code"}
    with recording() as rec:
        _, answers = stateful_session(rec, [huge])
        assert answers[1]["error"]["code"] == 2**31
        span = transport(rec.spans(), "mcp.synthetic/huge-code")
        assert MCP["errorCode"] not in span["attributes"]
        assert span["status"] == 2
        assert span["attributes"]["error.type"] == fixture_spans("sdk-mcp-jsonrpc-error")[0]["attributes"]["error.type"]


# Fails if a client's traceparent in _meta (the SDK's own OpenTelemetry
# middleware continues it) takes the step out of its HTTP request's trace
# instead of being only a link.
def test_client_traceparent_is_only_a_link() -> None:
    remote_trace, remote_span = "ab" * 16, "cd" * 8
    meta = {"traceparent": f"00-{remote_trace}-{remote_span}-01"}
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "checkout", "arguments": {"item": "x"}, "_meta": meta},
    }
    with recording() as rec:
        stateful_session(rec, [call])
        spans = rec.spans()
        tool = transport(spans, "mcp.tools/call:checkout")
        entries = {span["span_id"]: span["trace_id"] for span in spans if span["name"] == "HTTP POST"}
        assert tool["parent_span_id"] in entries
        assert tool["trace_id"] == entries[tool["parent_span_id"]] != remote_trace
        assert (remote_trace, remote_span) in [(link[0], link[1]) for link in tool["links"]]


# Fails if the plaintext multi-round-trip state, which the SDK unseals before
# and seals after this middleware, reaches the redact hook or an export.
def test_request_state_never_leaves_in_a_payload() -> None:
    seen: list[McpPayload] = []

    def redact(payload: McpPayload) -> McpPayload:
        seen.append(payload)
        return payload

    async def elicit(context: Any, params: Any) -> ElicitResult:
        return ElicitResult(action="accept", content={})

    with recording() as rec:
        app = server(mcp_capture(rec.client, capture_payloads=CapturePayloads(redact=redact)))

        async def run() -> None:
            async with McpClient(app, elicitation_callback=elicit) as client:
                assert "done" in repr(await client.call_tool("confirm", {}))

        anyio.run(run)
        spans = rec.spans()
        assert len([span for span in transports(spans) if span["name"] == "mcp.tools/call:confirm"]) == 2
        assert "plaintext-secret-state" not in repr(spans)
        assert "plaintext-secret-state" not in repr(seen)


# Fails if someone runs the redact hook, and copies the payload for it, when
# the queue's payload budget is already spent, or does not report the loss.
def test_spent_payload_budget_skips_the_hook() -> None:
    calls: list[McpPayload] = []
    with recording() as rec:
        rec.processors.spans._payload_budget = 0  # pyright: ignore[reportPrivateUsage]
        capture = mcp_capture(rec.client, capture_payloads=CapturePayloads(redact=lambda p: calls.append(p) or p))
        app = server(capture)

        async def run() -> None:
            async with McpClient(app) as client:
                await client.call_tool("checkout", {"item": "secret-item"})

        anyio.run(run)
        spans = rec.spans()
        assert calls == []
        assert "payload_budget" in rec.codes()
        assert "secret-item" not in repr(spans)


# Fails if a resource URI's userinfo, query or fragment reaches a step name.
def test_resource_uri_is_named_without_userinfo_query_or_fragment() -> None:
    with recording() as rec:
        app = server(mcp_capture(rec.client))

        async def run() -> None:
            async with McpClient(app) as client:
                with contextlib.suppress(MCPError):  # no such resource: the step is still named
                    await client.read_resource("catalog://user:secret-pw@items/sku?token=secret-q#secret-f")

        anyio.run(run)
        spans = rec.spans()
        assert transport(spans, "mcp.resources/read:catalog://items/sku")
        assert "secret" not in repr([span["name"] for span in spans])


# Fails if someone measures a message other than as the MCP SDK encodes it on
# the wire (pydantic's compact JSON: 1e-7, where json.dumps writes 1e-07).
def test_response_bytes_match_the_wire() -> None:
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "ratio", "arguments": {}}}
    sizes: list[int] = []
    with recording() as rec:
        stateful_session(rec, [call], sizes=sizes)
        tool = transport(rec.spans(), "mcp.tools/call:ratio")
        assert tool["attributes"][MCP["responseBytes"]] == sizes[0]


# Fails if someone records the negotiated protocol version instead of the one
# the message carried.
def test_protocol_version_is_what_the_message_carried() -> None:
    with recording() as rec:
        _, answers = stateful_session(rec, [], requested="2024-01-01")
        assert answers[0]["result"]["protocolVersion"] != "2024-01-01"
        initialize = transport(rec.spans(), "mcp.initialize")
        assert initialize["attributes"][MCP["protocolVersion"]] == "2024-01-01"


# Fails if someone moves a size limit off the contract's.
def test_payload_and_batch_limits_are_the_contracts() -> None:
    assert OTLP["limits"]["mcpPayloadBytes"] == _PAYLOAD_BYTES
    assert OTLP["limits"]["exportBatchBytes"] == BATCH_BYTES


# Fails if a later mcp release that moves the connection makes the session
# read raise into the MCP call instead of sending no hash.
def test_session_read_fails_soft() -> None:
    class Bare:
        session = object()

    with recording() as rec:
        assert mcp_capture(rec.client)._session_hash(Bare.session) is None  # pyright: ignore[reportPrivateUsage]


# Fails if someone lets a result past one export's body limit fail the export
# (the step is lost) instead of truncating its payload to fit and flagging it.
def test_oversized_payload_is_truncated_to_one_export() -> None:
    size = BODY_BYTES + 1024 * 1024
    spans, codes, _ = call_with_payloads(lambda payload: payload, "big", {}, size=size)
    tool = transport(spans, "mcp.tools/call:big")
    response = tool["attributes"][MCP["responsePayload"]]
    assert tool["attributes"][MCP["responseTruncated"]] is True
    assert MCP["requestTruncated"] not in tool["attributes"]
    assert len(response.encode()) + len(tool["attributes"][MCP["requestPayload"]].encode()) <= _PAYLOAD_BYTES
    assert tool["attributes"][MCP["responseBytes"]] > size
    assert "export_failed" not in codes


# Fails if someone batches by count alone: three steps with 6 MiB payloads
# would share one export past the collector's body limit and all be lost.
def test_large_payloads_are_split_across_exports() -> None:
    spans, codes, _ = call_with_payloads(lambda payload: payload, "big", {}, size=3 * 1024 * 1024, calls=3)
    calls = [span for span in transports(spans) if span["name"] == "mcp.tools/call:big"]
    assert len(calls) == 3
    assert all(MCP["responseTruncated"] not in span["attributes"] for span in calls)
    assert "export_failed" not in codes


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
