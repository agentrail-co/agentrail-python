# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Per-installation state: context keys, the resource, and span metadata."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal

from opentelemetry import context as otel_context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, SpanContext
from opentelemetry.util.types import AttributeValue

from ._handoff import key_id, keyed_hash
from ._options import CaptureCode, CaptureError, MilestoneContext, Resolved, Signal, VerifiedIdentity
from ._version import VERSION

COMPONENT = "agentrail-python"
Attributes = dict[str, AttributeValue]


@dataclass
class RequestState:
    """One managed operation's evidence, shared by its child spans and tasks."""

    verified: bool = False
    links: list[Link] = field(default_factory=lambda: [])
    location: Attributes = field(default_factory=lambda: {})
    entry: SpanContext | None = None
    """The managed entry's span: what a milestone nests under, whatever a library continued."""
    _identity: Attributes | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def principal(self) -> Attributes:
        with self._lock:
            return dict(self._identity or {})

    def assert_identity(self, identity: Attributes) -> bool:
        """Keeps the first assertion. True when ``identity`` conflicts with it."""
        with self._lock:
            if self._identity is None:
                self._identity = identity
                return False
            return self._identity != identity


@dataclass
class McpEvidence:
    """What an MCP span records, filled in while its message is handled.

    It lives in the span's mark, never on the customer's span: the payloads
    reach only Agentrail's sanitized copy.
    """

    layer: Literal["transport", "handler"]
    method: str | None
    client: str | None = None
    client_version: str | None = None
    protocol: str | None = None
    session_hash: str | None = None
    request_bytes: int | None = None
    response_bytes: int | None = None
    error_code: int | None = None
    is_error: bool = False
    request_payload: str | None = None
    response_payload: str | None = None
    request_truncated: bool = False
    response_truncated: bool = False


@dataclass(frozen=True)
class Mark:
    name: str
    details: MilestoneContext
    performance: Attributes | None = None
    identity: Attributes | None = None
    step: bool = True
    """False for an MCP handler inside a transport step: a child span, never a second step."""
    mcp: McpEvidence | None = None


@dataclass(frozen=True)
class SpanMeta:
    state: RequestState
    mark: Mark | None


class Runtime:
    def __init__(self, options: Resolved) -> None:
        self.options = options
        unique = uuid.uuid4().hex
        self.state_key = otel_context.create_key(f"agentrail.state.{unique}")
        self.mark_key = otel_context.create_key(f"agentrail.milestone.{unique}")
        self.key_id = key_id(options.input.hash_key)
        self.resource = Resource(
            {
                "service.name": options.input.service_name,
                "service.instance.id": options.instance,
                "agentrail.component.name": COMPONENT,
                "agentrail.component.version": VERSION,
                "agentrail.hash_key_id": self.key_id,
                "agentrail.capture.surfaces": "http,mcp,auth,custom",
            }
        )
        self.scope = InstrumentationScope(COMPONENT, VERSION)
        self._spans: dict[tuple[int, int], SpanMeta] = {}
        self._spans_lock = threading.Lock()

    def state(self, ctx: otel_context.Context | None = None) -> RequestState | None:
        value = otel_context.get_value(self.state_key, ctx)
        return value if isinstance(value, RequestState) else None

    def mark(self, ctx: otel_context.Context | None = None) -> Mark | None:
        value = otel_context.get_value(self.mark_key, ctx)
        return value if isinstance(value, Mark) else None

    def remember(self, trace_id: int, span_id: int, meta: SpanMeta) -> None:
        with self._spans_lock:
            self._spans[(trace_id, span_id)] = meta

    def forget(self, trace_id: int, span_id: int) -> SpanMeta | None:
        with self._spans_lock:
            return self._spans.pop((trace_id, span_id), None)

    def identity(self, identity: VerifiedIdentity) -> Attributes:
        key = self.options.input.hash_key
        attributes: Attributes = {
            "agentrail.identity.verified": True,
            "agentrail.principal.kind": identity.kind,
            "agentrail.principal.id_hash": keyed_hash(key, identity.id),
        }
        if identity.acting_user_id is not None:
            attributes["agentrail.principal.acting_user_hash"] = keyed_hash(key, identity.acting_user_id)
        return attributes

    def report(self, code: CaptureCode, signal: Signal, count: int = 1) -> None:
        self.options.report(CaptureError(code, signal, count))
