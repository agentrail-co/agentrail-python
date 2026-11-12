# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""What leaves the process: an allowlist, never the customer's data by default."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentrail_python import LogsOptions, MilestoneContext, StructuredLog, VerifiedIdentity
from agentrail_python._privacy import sanitized_url, selected_body
from agentrail_python._text import utf16_length
from agentrail_python.logging import AgentrailHandler
from support import by_name, recording

MARKER = "028T5CY4TQKFFX"
STEP = MilestoneContext(surface="custom", kind="checkout.confirm")


class Keep(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Any) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


# Fails if someone exports query values other than valid ``h`` markers, a
# fragment or credentials in a URL.
def test_urls_keep_only_valid_markers() -> None:
    assert sanitized_url(f"https://user:pw@Shop.test:8443/p/a?token=secret&h={MARKER}&h=600#frag") == (
        f"https://shop.test:8443/p/a?h={MARKER}"
    )
    assert sanitized_url("javascript:alert(1)") is None
    assert sanitized_url("https://shop.test/" + "a" * 5000) is None


# Fails if someone exports span attributes, events, exception messages or names
# from supporting work, or changes what the customer's own exporter receives.
def test_supporting_spans_are_neutralized_and_customer_spans_untouched() -> None:
    with recording() as rec:
        customer = Keep()
        rec.provider.add_span_processor(SimpleSpanProcessor(customer))
        tracer = rec.provider.get_tracer("db")

        async def endpoint(request: Request) -> PlainTextResponse:
            with tracer.start_as_current_span(
                "SELECT * FROM users WHERE email='a@b.test'",
                kind=SpanKind.CLIENT,
                attributes={"db.statement": "secret", "enduser.id": "u-1", "http.url": "https://x.test/?k=secret"},
            ) as span:
                span.record_exception(ValueError("private message"))
                span.add_event("custom", {"card": "4242"})
            return PlainTextResponse("ok")

        TestClient(rec.client.asgi(Starlette(routes=[Route("/", endpoint)]))).get("/?email=a@b.test")
        spans = rec.spans()
        supporting = by_name(spans, "backend.operation")
        assert supporting["attributes"] == {"error.type": "ValueError", "url.full": "https://x.test/"}
        assert supporting["events"] == []
        assert by_name(spans, "HTTP GET")["attributes"]["url.full"] == "http://testserver/"
        for secret in ("private message", "4242", "a@b.test", "secret", "u-1"):
            assert secret not in repr(spans)
        original = next(span for span in customer.spans if span.name.startswith("SELECT"))
        assert original.attributes is not None and original.attributes["db.statement"] == "secret"
        assert len(original.events) == 2


# Fails if someone exports message text or fields not named in include_fields,
# or lets a long field break the 4096 limit or the body's JSON.
def test_log_body_is_only_selected_fields() -> None:
    assert selected_body({"msg": "hi", "user": "secret"}, ("msg",)) == '{"msg":"hi"}'
    assert selected_body({"user": "secret"}, ()) is None
    assert selected_body({"n": 1, "obj": {"a": 1}, "ok": True}, ("obj", "n", "ok")) == '{"n":1,"ok":true}'
    long = selected_body({"a": "x" * 3000, "b": "😀" * 3000, "c": 1}, ("a", "b", "c"))
    assert long is not None
    assert utf16_length(long) <= 4096
    parsed = json.loads(long)
    assert parsed["a"] == "x" * 3000
    assert set(parsed["b"]) == {"😀"}
    assert "c" not in parsed  # no room left once "b" was shortened to fit


# Fails if someone uncouples a log from the span it was emitted in, exports
# arbitrary extra fields, or takes an exception's message.
def test_logging_handler_correlates_and_filters() -> None:
    with recording(logs=LogsOptions(include_fields=("message", "order_status"))) as rec:
        logger = logging.getLogger("synthetic.orders")
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        logger.addHandler(AgentrailHandler(rec.client))
        try:
            with rec.client.start_milestone("checkout", STEP):
                try:
                    raise KeyError("private key detail")
                except KeyError:
                    logger.exception(
                        "order %s failed",
                        "o-1",
                        extra={"order_status": "failed", "email": "a@b.test", "event.name": "checkout.failed"},
                    )
            with rec.provider.get_tracer("customer").start_as_current_span("unmanaged work"):
                logger.info("outside")
        finally:
            logger.handlers.clear()
        milestone = by_name(rec.spans(), "checkout")
        inside, outside = rec.logs()
        assert (inside["trace_id"], inside["span_id"]) == (milestone["trace_id"], milestone["span_id"])
        assert inside["severity_number"] == 17
        assert inside["severity_text"] == "ERROR"
        assert inside["event_name"] == "checkout.failed"
        assert inside["attributes"]["error.type"] == "KeyError"
        assert json.loads(inside["body"]) == {"message": "order o-1 failed", "order_status": "failed"}
        assert outside["trace_id"] == ""
        assert "a@b.test" not in repr(rec.logs()) and "private key detail" not in repr(rec.logs())


# Fails if someone records uncorrelated logs after the application excluded them.
def test_uncorrelated_logs_can_be_excluded() -> None:
    with recording(logs=LogsOptions(exclude_uncorrelated=True)) as rec:
        rec.client.log(StructuredLog(event_name="outside"))
        with rec.client.start_milestone("x", STEP):
            rec.client.log(StructuredLog(event_name="inside"))
        assert [log["event_name"] for log in rec.logs()] == ["inside"]


# Fails if someone lets an on_error callback's own logging be captured, which
# could feed the queue it reports on.
def test_error_reporting_is_not_captured() -> None:
    logger = logging.getLogger("synthetic.reporting")
    logger.propagate = False

    def on_error(error: Any) -> None:
        logger.error("capture issue %s", error.code)

    with recording(on_error=on_error, logs=LogsOptions(include_fields=("message",))) as rec:
        logger.addHandler(AgentrailHandler(rec.client))
        try:
            rec.client.identify(VerifiedIdentity(kind="user", id="u"))
        finally:
            logger.handlers.clear()
        assert rec.logs() == []


# Fails if someone changes OpenTelemetry's level-to-severity mapping, which the
# collector reads to count errors.
@pytest.mark.parametrize(
    ("level", "number"), [(logging.DEBUG, 5), (logging.INFO, 9), (logging.WARNING, 13), (logging.CRITICAL, 21)]
)
def test_severity_numbers(level: int, number: int) -> None:
    from agentrail_python.logging import severity_number

    assert severity_number(level) == number
