# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Runs a FastAPI app under uvicorn and a Flask app under the WSGI reference
server, each with the installed agentrail-python, against a stand-in collector.

Used by scripts/check-consumer.sh from a fresh virtual environment.
"""

from __future__ import annotations

import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, make_server

import uvicorn
from fastapi import FastAPI
from flask import Flask
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

import agentrail_python as ar
from agentrail_python.asgi import AgentrailASGIMiddleware

received: list[bytes] = []


class Collector(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        if self.path.endswith("/v1/traces"):
            received.append(body)
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


class QuietWSGI(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(server: Any) -> None:
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main() -> None:
    collector = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    serve(collector)
    agentrail = ar.create_processors(
        ar.Options(
            collector=ar.CollectorOptions(
                endpoint=f"http://127.0.0.1:{collector.server_address[1]}", secret="synthetic"
            ),
            hash_key="synthetic-consumer-key",
            service_name="consumer-check",
        )
    )
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    provider = TracerProvider(sampler=agentrail.sampler(ALWAYS_ON))
    provider.add_span_processor(agentrail.span_processor)
    client = agentrail.bind(provider)

    api = FastAPI()

    @api.get("/items/{item_id}")
    def item(item_id: int) -> dict[str, int]:
        client.identify(ar.VerifiedIdentity(kind="user", id="synthetic-user"))
        return {"id": item_id}

    api.add_middleware(AgentrailASGIMiddleware, client=client)
    config = uvicorn.Config(api, host="127.0.0.1", port=0, log_level="error")
    fastapi_server = uvicorn.Server(config)
    threading.Thread(target=fastapi_server.run, daemon=True).start()
    while not fastapi_server.started:
        time.sleep(0.02)
    port = fastapi_server.servers[0].sockets[0].getsockname()[1]
    assert urllib.request.urlopen(f"http://127.0.0.1:{port}/items/3").status == 200

    flask = Flask("consumer")

    @flask.get("/hello")
    def hello() -> str:
        client.milestone("hello", ar.MilestoneContext(surface="custom", kind="greeting"))
        return "hi"

    flask.wsgi_app = client.wsgi(flask.wsgi_app)  # type: ignore[method-assign]
    wsgi = make_server("127.0.0.1", 0, flask, handler_class=QuietWSGI)
    serve(wsgi)
    assert urllib.request.urlopen(f"http://127.0.0.1:{wsgi.server_port}/hello").read() == b"hi"

    client.shutdown()
    fastapi_server.should_exit = True
    names: list[tuple[str, str]] = []
    routes: list[str] = []
    for body in received:
        for group in ExportTraceServiceRequest.FromString(body).resource_spans:
            for scope in group.scope_spans:
                names.extend((span.name, scope.scope.name) for span in scope.spans)
                routes.extend(
                    a.value.string_value for span in scope.spans for a in span.attributes if a.key == "http.route"
                )
    if routes != ["/items/{item_id}"]:
        sys.exit(f"unexpected routes: {routes}")
    expected = {("HTTP GET", "agentrail-python"), ("hello", "agentrail-python")}
    if not expected <= set(names) or sum(name == "HTTP GET" for name, _ in names) != 2:
        sys.exit(f"unexpected spans: {names}")
    print(f"consumer check passed: agentrail-python {ar.VERSION}, spans {sorted(names)}")


if __name__ == "__main__":
    main()
