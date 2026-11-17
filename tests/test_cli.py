# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""A customer CLI's x-agentrail-cli header, read by the middleware."""

from __future__ import annotations

from typing import Any

import pytest
from flask import Flask
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentrail_python._cli import read_cli_header
from agentrail_python._handoff import keyed_hash
from support import CONTRACT, recording

VECTORS: dict[str, Any] = CONTRACT["cliHeader"]["vectors"]
NAMES: dict[str, str] = CONTRACT["otlp"]["cliAttributes"]
HEADER: str = VECTORS["header"]["value"]
READS: dict[str, Any] = VECTORS["header"]["reads"]
KEYED: dict[str, str] = VECTORS["keyedSession"]


def as_reads(value: str) -> dict[str, Any] | None:
    header = read_cli_header(value)
    if header is None:
        return None
    reads: dict[str, Any] = {
        "sdkVersion": header.sdk_version,
        "cli": header.cli,
        "cliVersion": header.cli_version,
        "run": header.run,
        "sequence": header.sequence,
        "session": header.session,
        "sessionFrom": header.session_from,
        "mode": header.mode,
        "agent": header.agent,
        "agentVersion": header.agent_version,
        "evidence": None if header.evidence is None else list(header.evidence),
        "command": header.command,
    }
    return {key: value for key, value in reads.items() if value is not None}


# Fails if the Python reader and the contract part: a header agentrail-cli-sdk
# wrote would read as another command or session here than in Node and Go, or
# a header no CLI SDK writes (a raw session id, two header lines joined) would
# reach the collector as a session.
def test_the_header_reads_as_the_contract_says() -> None:
    assert as_reads(VECTORS["header"]["value"]) == READS
    assert as_reads(VECTORS["later"]["value"]) == VECTORS["later"]["reads"]
    assert VECTORS["unread"]
    for value in VECTORS["unread"]:
        assert as_reads(value) is None, value
    assert keyed_hash(KEYED["hashKey"], KEYED["session"]) == KEYED["hash"]


def expected() -> dict[str, Any]:
    return {
        NAMES["name"]: READS["cli"],
        NAMES["version"]: READS["cliVersion"],
        NAMES["command"]: READS["command"],
        NAMES["run"]: READS["run"],
        NAMES["sequence"]: READS["sequence"],
        NAMES["mode"]: READS["mode"],
        NAMES["sessionHash"]: KEYED["hash"],
        NAMES["sessionFrom"]: READS["sessionFrom"],
        NAMES["agent"]: READS["agent"],
        NAMES["agentVersion"]: READS["agentVersion"],
        NAMES["evidence"]: ",".join(READS["evidence"]),
        NAMES["sdkVersion"]: READS["sdkVersion"],
    }


def cli_of(span: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in span["attributes"].items() if key.startswith("agentrail.cli.")}


def asgi(client: Any) -> TestClient:
    async def endpoint(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return TestClient(client.asgi(Starlette(routes=[Route("/{path:path}", endpoint, methods=["POST"])])))


def wsgi(client: Any) -> Any:
    app = Flask(__name__)

    @app.post("/<path:path>")
    def handler(path: str) -> str:
        return "ok"

    app.wsgi_app = client.wsgi(app.wsgi_app)  # type: ignore[method-assign]
    return app.test_client()


@pytest.mark.parametrize("serve", [asgi, wsgi], ids=["asgi", "wsgi"])
# Fails if someone stops reading the header, sends the session as the CLI gave
# it, or puts the facts under names the collector does not read: a CLI's runs
# would join no journey, or its session would leave the server unhashed. Also
# fails if a header that does not read, or one sent twice (which servers join
# into one value), sets anything.
def test_the_request_span_says_what_the_header_said(serve: Any) -> None:
    assert READS["session"] == KEYED["session"]
    with recording(hash_key=KEYED["hashKey"]) as rec:
        client = serve(rec.client)

        def send(path: str, headers: list[tuple[str, str]]) -> None:
            response = client.post(path, headers=headers)
            assert response.status_code == 200
            response.close()

        send("/cli", [("x-agentrail-cli", HEADER)])
        send("/plain", [])
        for index, value in enumerate(VECTORS["unread"]):
            if value:
                send(f"/unread/{index}", [("x-agentrail-cli", value)])
        send("/twice", [("x-agentrail-cli", HEADER), ("x-agentrail-cli", HEADER)])
        spans = [span for span in rec.spans() if span["name"] == "HTTP POST"]
        by_path = {span["attributes"]["url.full"].split("://", 1)[1].split("/", 1)[1]: span for span in spans}
        assert cli_of(by_path["cli"]) == expected()
        others = [span for path, span in by_path.items() if path != "cli"]
        assert len(others) == 1 + (len(VECTORS["unread"]) - 1) + 1
        for span in others:
            assert cli_of(span) == {}, span["attributes"]["url.full"]
        assert KEYED["session"] not in repr(spans)
        assert rec.codes() == []
