# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Synthetic OTLP bytes produced through the public Python SDK, for the receiver gate.

No provider calls, real identities or production credentials: the collector
token and workspace hash key below are synthetic test constants.
"""

from __future__ import annotations

import argparse
import base64
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

import agentrail_python as ar
from agentrail_python.httpx import transport

HASH_KEY = "synthetic-python-receiver-hash-key"


@contextmanager
def server(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def run(output: Path) -> None:
    packets: list[dict[str, str]] = []
    lock = threading.Lock()

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            signal = self.path.rsplit("/", 1)[-1]
            if self.headers.get("content-type") != "application/x-protobuf" or signal not in ("traces", "logs"):
                self.send_response(415)
                self.end_headers()
                return
            with lock:
                packets.append({"signal": signal, "protobuf": base64.b64encode(body).decode()})
            self.send_response(200)
            self.send_header("content-type", "application/x-protobuf")
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    class Target(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    errors: list[ar.CaptureError] = []
    result: dict[str, Any] = {"provenance": "synthetic public Python SDK; no provider calls or production credentials"}
    with server(Collector) as collector_url, server(Target) as target_url:
        processors = ar.create_processors(
            ar.Options(
                collector=ar.CollectorOptions(endpoint=collector_url, secret="synthetic-local-collector"),
                hash_key=HASH_KEY,
                service_name="synthetic-python-service",
                service_instance_id="synthetic-python-instance",
                queue=ar.QueueOptions(scheduled_delay=3600),
                incoming_location=lambda request: ar.IPLocation(
                    country="US", region="Washington", city="Seattle", source="source"
                ),
                on_error=errors.append,
            )
        )
        provider = TracerProvider(sampler=processors.sampler(ALWAYS_ON))
        provider.add_span_processor(processors.span_processor)
        client = processors.bind(provider)
        outbound = httpx.Client(transport=transport(client))

        async def checkout(request: Request) -> Response:
            context = trace.get_current_span().get_span_context()
            result["traceId"] = f"{context.trace_id:032x}"
            result["rootSpanId"] = f"{context.span_id:016x}"
            client.identify(
                ar.VerifiedIdentity(
                    kind="user", id="synthetic-python-user", acting_user_id="synthetic-python-acting-user"
                )
            )
            with client.start_milestone(
                "synthetic.checkout", ar.MilestoneContext(surface="custom", kind="checkout.confirm")
            ):
                result["milestoneSpanId"] = f"{trace.get_current_span().get_span_context().span_id:016x}"
                client.log(ar.StructuredLog(severity_number=9, severity_text="INFO", event_name="synthetic.milestone"))
                with provider.get_tracer("synthetic-support").start_as_current_span("synthetic database work") as span:
                    result["supportingSpanId"] = f"{span.get_span_context().span_id:016x}"
                    client.log(
                        ar.StructuredLog(
                            severity_number=17,
                            severity_text="ERROR",
                            error_type="SyntheticError",
                            event_name="synthetic.supporting",
                        )
                    )
                    outbound.get(f"{target_url}/synthetic-target").raise_for_status()
            return Response(status_code=204)

        app = client.asgi(Starlette(routes=[Route("/synthetic-checkout", checkout, methods=["POST"])]))
        response = TestClient(app).post("/synthetic-checkout")
        if response.status_code != 204:
            raise SystemExit(f"synthetic request status {response.status_code}")
        client.flush()
        client.shutdown()
        provider.shutdown()
    if errors:
        raise SystemExit(f"capture reported {len(errors)} errors: {[error.code for error in errors]}")
    if {packet["signal"] for packet in packets} != {"traces", "logs"}:
        raise SystemExit(f"expected traces and logs exports; got {len(packets)} packets")
    result["exports"] = packets
    result["expected"] = {"entries": 2, "spans": 4, "logs": 2}
    output.write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    output.chmod(0o600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    run(parser.parse_args().output)
