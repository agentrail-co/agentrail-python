# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Sanitized copies: what leaves the process, built from an allowlist.

The customer's spans and log records are never changed; the queue only ever
holds these copies.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.context import Context
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import (
    INVALID_SPAN,
    Link,
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    TraceFlags,
    set_span_in_context,
)
from opentelemetry.util.types import AttributeValue

from ._handoff import valid_marker
from ._runtime import Attributes, McpEvidence, Runtime, SpanMeta
from ._text import bounded, js_json, utf16_length, valid_text

_LIMIT = 4096
REQUEST_PAYLOAD = "agentrail.mcp.request.payload"
RESPONSE_PAYLOAD = "agentrail.mcp.response.payload"
REQUEST_TRUNCATED = "agentrail.mcp.request.truncated"
RESPONSE_TRUNCATED = "agentrail.mcp.response.truncated"
_PAYLOADS = frozenset((REQUEST_PAYLOAD, RESPONSE_PAYLOAD))
_PAYLOAD_KEYS = _PAYLOADS | {REQUEST_TRUNCATED, RESPONSE_TRUNCATED}


def clean_context(context: SpanContext) -> SpanContext:
    """The IDs and flags only: trace state never leaves."""
    return SpanContext(
        trace_id=context.trace_id,
        span_id=context.span_id,
        is_remote=context.is_remote,
        trace_flags=TraceFlags(context.trace_flags),
    )


def sanitized_url(raw: object) -> str | None:
    """An http(s) URL with only valid ``h`` markers in its query; None past 4096 units."""
    if not isinstance(raw, str):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    markers = [value for key, value in parse_qsl(parts.query, keep_blank_values=True) if key == "h"]
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    query = urlencode([("h", marker) for marker in markers if valid_marker(marker)])
    url = urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", query, ""))
    return url if valid_text(url, _LIMIT) else None


def _first(attributes: Mapping[str, AttributeValue], *names: str) -> AttributeValue | None:
    for name in names:
        if name in attributes:
            return attributes[name]
    return None


def copy_span(span: ReadableSpan, meta: SpanMeta, runtime: Runtime) -> ReadableSpan:
    original: Mapping[str, AttributeValue] = span.attributes or {}
    attributes: Attributes = {}
    pairs = {
        "http.request.method": ("http.request.method", "http.method"),
        "http.route": ("http.route",),
        "http.response.status_code": ("http.response.status_code", "http.status_code"),
        "user_agent.original": ("user_agent.original", "http.user_agent"),
        "error.type": ("error.type", "exception.type"),
    }
    for key, names in pairs.items():
        value = _first(original, *names)
        if isinstance(value, str):
            attributes[key] = bounded(value, _LIMIT)
        elif isinstance(value, int) and not isinstance(value, bool):
            attributes[key] = value
    if "error.type" not in attributes:
        for event in span.events:
            kind = (event.attributes or {}).get("exception.type")
            if event.name == "exception" and isinstance(kind, str):
                attributes["error.type"] = bounded(kind, _LIMIT)
                break
    address, path, scheme = original.get("server.address"), original.get("url.path"), original.get("url.scheme")
    incoming = None
    if isinstance(address, str) and isinstance(path, str) and isinstance(scheme, str):
        port = original.get("server.port")
        query = original.get("url.query")
        incoming = f"{scheme}://{address}{f':{port}' if isinstance(port, int) else ''}{path}"
        if isinstance(query, str):
            incoming += f"?{query}"
    url = sanitized_url(_first(original, "url.full", "http.url") or incoming)
    if url is not None:
        attributes["url.full"] = url

    kind = span.kind
    parent = span.parent
    own = span.instrumentation_scope is not None and span.instrumentation_scope.name == runtime.scope.name
    if kind in (SpanKind.SERVER, SpanKind.CONSUMER) and not own and parent is not None and not parent.is_remote:
        # A library's own server span inside a managed operation (the MCP SDK opens
        # one per message) is supporting work, not a second entry.
        kind = SpanKind.INTERNAL
    mark = meta.mark
    entry = (mark is not None and mark.step) or kind in (SpanKind.SERVER, SpanKind.CONSUMER)
    if entry:
        identity = mark.identity if mark is not None and mark.identity is not None else meta.state.principal()
        attributes.update(identity)
        attributes.update(meta.state.location)
    if kind == SpanKind.SERVER:
        attributes["agentrail.ingress.verified"] = meta.state.verified
    method = attributes.get("http.request.method")
    name = f"HTTP {method}" if isinstance(method, str) else "backend.operation"
    if mark is not None:
        name = mark.name
        if mark.step:
            attributes["agentrail.step"] = True
            attributes["agentrail.step.surface"] = mark.details.surface
            attributes["agentrail.step.kind"] = mark.details.kind
        if mark.performance is not None:
            attributes.update(mark.performance)
        mcp = mark.details.mcp
        if mcp is not None:
            _client_details(attributes, mcp.client, mcp.client_version, mcp.protocol)
        if mark.mcp is not None:
            _mcp_attributes(attributes, mark.mcp)
    links = [Link(clean_context(link.context)) for link in span.links]
    if kind == SpanKind.SERVER:
        links.extend(Link(clean_context(link.context)) for link in meta.state.links)
    context = span.get_span_context()
    assert context is not None  # an SDK span always has a context
    return ReadableSpan(
        name=bounded(name, _LIMIT),
        context=clean_context(context),
        parent=None if parent is None else clean_context(parent),
        resource=runtime.resource,
        attributes=attributes,
        events=(),
        links=links,
        kind=kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=runtime.scope,
    )


def _client_details(attributes: Attributes, client: str | None, version: str | None, protocol: str | None) -> None:
    for key, value, limit in (
        ("agentrail.mcp.client.name", client, 128),
        ("agentrail.mcp.client.version", version, 128),
        ("agentrail.mcp.protocol.version", protocol, 64),
    ):
        if value:
            attributes[key] = bounded(value, limit)


def _mcp_attributes(attributes: Attributes, evidence: McpEvidence) -> None:
    """The contract's ``otlp.mcpAttributes``. Payloads are exempt from the text limit."""
    attributes["agentrail.mcp.layer"] = evidence.layer
    if evidence.method is not None:
        attributes["agentrail.mcp.method"] = evidence.method
    _client_details(attributes, evidence.client, evidence.client_version, evidence.protocol)
    values = (
        ("agentrail.mcp.session.id_hash", evidence.session_hash),
        ("agentrail.mcp.request.bytes", evidence.request_bytes),
        ("agentrail.mcp.response.bytes", evidence.response_bytes),
        ("agentrail.mcp.jsonrpc.error_code", evidence.error_code),
        (REQUEST_PAYLOAD, evidence.request_payload),
        (RESPONSE_PAYLOAD, evidence.response_payload),
    )
    for key, value in values:
        if value is not None:
            attributes[key] = value
    for key, flag in (
        ("agentrail.mcp.result.is_error", evidence.is_error),
        (REQUEST_TRUNCATED, evidence.request_truncated and evidence.request_payload is not None),
        (RESPONSE_TRUNCATED, evidence.response_truncated and evidence.response_payload is not None),
    ):
        if flag:
            attributes[key] = True


def _utf8_length(value: str) -> int:
    return len(value.encode("utf-8", "surrogatepass"))


def span_weight(span: ReadableSpan) -> tuple[int, int]:
    """An over-estimate of the span's OTLP protobuf size, and its payload bytes."""
    size = 256 + _utf8_length(span.name) + 64 * len(span.links)
    payload = 0
    for key, value in (span.attributes or {}).items():
        size += 16 + len(key)
        if isinstance(value, str):
            length = _utf8_length(value)
            size += length
            if key in _PAYLOADS:
                payload += length
        else:
            size += 16
    return size, payload


def log_weight(record: ReadableLogRecord) -> tuple[int, int]:
    """An over-estimate of the record's OTLP protobuf size; logs carry no payloads."""
    log = record.log_record
    body = log.body
    size = 256 + (_utf8_length(body) if isinstance(body, str) else 0)
    size += sum(
        16 + len(key) + (_utf8_length(value) if isinstance(value, str) else 16)
        for key, value in (log.attributes or {}).items()
    )
    return size, 0


def without_payloads(span: ReadableSpan) -> ReadableSpan:
    """The same sanitized copy without its payload attributes."""
    attributes = {key: value for key, value in (span.attributes or {}).items() if key not in _PAYLOAD_KEYS}
    return ReadableSpan(
        name=span.name,
        context=span.get_span_context(),
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=(),
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def _scalar(value: object) -> str | int | float | bool | type[_Skip] | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return bounded(value, _LIMIT)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _Skip
    return _Skip


class _Skip:
    """A field that is not a scalar and is left out."""


def _fit(value: str, budget: int) -> str:
    """The longest prefix whose JSON encoding fits ``budget`` UTF-16 units."""
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if utf16_length(js_json(value[:middle])) <= budget:
            low = middle
        else:
            high = middle - 1
    return value[:low]


def selected_body(fields: Mapping[str, object], selected: tuple[str, ...]) -> str | None:
    """A JSON object of the selected scalar fields, in allowlist order, at most 4096 units."""
    result: dict[str, object] = {}
    body: str | None = None
    for key in selected:
        if key not in fields or key in result:
            continue
        scalar = _scalar(fields[key])
        if scalar is _Skip:
            continue
        candidate = {**result, key: scalar}
        encoded = js_json(candidate)
        if utf16_length(encoded) > _LIMIT:
            if not isinstance(scalar, str):
                continue
            overhead = utf16_length(js_json({**result, key: ""})) - 2
            budget = _LIMIT - overhead
            if budget < 2:
                continue
            candidate[key] = _fit(scalar, budget)
            encoded = js_json(candidate)
        result = candidate
        body = encoded
    return body


@dataclass(frozen=True, kw_only=True)
class StructuredLog:
    """A log entry. ``fields`` reach Agentrail only when named in ``logs.include_fields``."""

    severity_number: int | None = None
    severity_text: str | None = None
    error_type: str | None = None
    event_name: str | None = None
    fields: Mapping[str, object] = field(default_factory=lambda: {})


def copy_log(
    entry: StructuredLog,
    timestamp: int,
    observed: int,
    span: SpanContext | None,
    runtime: Runtime,
) -> ReadableLogRecord:
    attributes: Attributes = {"agentrail.log.id": str(uuid.uuid4())}
    if entry.error_type:
        attributes["error.type"] = bounded(entry.error_type, _LIMIT)
    body = selected_body(entry.fields, runtime.options.include_fields)
    if body is not None:
        attributes["agentrail.log.body_allowed"] = True
    severity = entry.severity_number
    record = LogRecord(
        timestamp=timestamp,
        observed_timestamp=observed,
        # Exact correlation, never the ambient span: an uncorrelated record names none.
        context=set_span_in_context(INVALID_SPAN if span is None else NonRecordingSpan(span), Context()),
        severity_text=None if entry.severity_text is None else bounded(entry.severity_text, 64),
        severity_number=SeverityNumber(severity) if severity is not None and 1 <= severity <= 24 else None,
        body=body,
        attributes=attributes,
        event_name=None if not entry.event_name else bounded(entry.event_name, 256),
    )
    return ReadableLogRecord(record, resource=runtime.resource, instrumentation_scope=runtime.scope)
