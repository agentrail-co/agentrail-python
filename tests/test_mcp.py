# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""MCP steps with the official SDK: names, client details, outcomes, nothing else."""

from __future__ import annotations

from typing import Any

import anyio
import httpx2
import pytest
from mcp.client.client import Client as McpClient
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer

from agentrail_python.mcp import mcp_capture
from support import by_name, recording


def server(rec: Any, capture: Any) -> MCPServer:
    app = MCPServer("orders", middleware=[capture.middleware])

    def checkout(item: str) -> str:
        return f"ordered {item}"

    async def refund(order: str) -> dict[str, object]:
        return {"content": [{"type": "text", "text": "no"}], "isError": True}

    def catalog() -> str:
        return "private catalog body"

    def greet(name: str) -> str:
        return f"Hello {name}, private prompt"

    app.tool(name="checkout")(capture.tool("checkout", checkout))
    app.tool(name="refund")(capture.tool("refund", refund))
    app.resource("catalog://items/secret-sku", name="catalog")(capture.resource("catalog", catalog))
    app.prompt(name="greet")(capture.prompt("greet", greet))
    return app


# Fails if someone records arguments, results, prompt text or resource URIs, drops
# the client's name, version or protocol, or stops marking isError results failed.
def test_tool_resource_prompt_steps() -> None:
    with recording() as rec:
        capture = mcp_capture(rec.client)
        app = server(rec, capture)

        async def run() -> None:
            async with McpClient(app) as client:
                result = await client.call_tool("checkout", {"item": "secret-item"})
                assert "ordered secret-item" in repr(result)
                await client.call_tool("refund", {"order": "o-1"})
                await client.read_resource("catalog://items/secret-sku")
                await client.get_prompt("greet", {"name": "secret-name"})

        anyio.run(run)
        spans = rec.spans()
        tool = by_name(spans, "mcp.tools/call:checkout")
        assert (
            tool["attributes"].items()
            >= {
                "agentrail.step": True,
                "agentrail.step.surface": "mcp",
                "agentrail.step.kind": "tool.call",
                "agentrail.mcp.client.name": "mcp",
                "agentrail.mcp.protocol.version": "2026-07-28",
            }.items()
        )
        assert "agentrail.mcp.client.version" in tool["attributes"]
        refund = by_name(spans, "mcp.tools/call:refund")
        assert refund["status"] == 2
        assert refund["attributes"]["error.type"] == "McpOperationError"
        assert by_name(spans, "mcp.resources/read:catalog")["attributes"]["agentrail.step.kind"] == "mcp.resources/read"
        assert by_name(spans, "mcp.prompts/get:greet")["attributes"]["agentrail.step.kind"] == "mcp.prompts/get"
        for secret in ("secret-item", "secret-sku", "secret-name", "private catalog", "private prompt", "o-1"):
            assert secret not in repr(spans)


# Fails if someone breaks the request context on the way to the tool, so an MCP
# call over HTTP would start its own scope instead of nesting under the request.
def test_mcp_over_streamable_http_nests_under_the_request() -> None:
    with recording() as rec:
        capture = mcp_capture(rec.client)
        app = server(rec, capture)
        asgi = rec.client.asgi(app.streamable_http_app(stateless_http=True, json_response=True))

        async def run() -> None:
            async with app.session_manager.run():
                # The MCP SDK's own HTTP client package.
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=asgi)) as http:
                    transport = streamable_http_client("http://localhost:8000/mcp", http_client=http)
                    async with McpClient(transport) as client:
                        await client.call_tool("checkout", {"item": "x"})

        anyio.run(run)
        spans = rec.spans()
        tool = by_name(spans, "mcp.tools/call:checkout")
        entries = [span for span in spans if span["kind"] == 2]
        parents = {span["span_id"] for span in spans}
        assert tool["trace_id"] in {entry["trace_id"] for entry in entries}
        assert tool["parent_span_id"] in parents
        assert all(entry["name"] == "HTTP POST" for entry in entries)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
