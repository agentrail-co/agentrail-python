# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Public configuration and evidence types, and their validation."""

from __future__ import annotations

import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import SplitResult, urlsplit

from ._text import valid_text, well_formed

if TYPE_CHECKING:
    from opentelemetry.propagators.textmap import TextMapPropagator

PrincipalKind = Literal["user", "agent_registration", "agent_instance", "oauth_client", "api_key"]
PRINCIPAL_KINDS: frozenset[str] = frozenset(("user", "agent_registration", "agent_instance", "oauth_client", "api_key"))
Surface = Literal["http", "mcp", "auth", "custom", "a2a", "genai", "payment", "edge", "firewall", "browser"]
SURFACES: frozenset[str] = frozenset(
    ("http", "mcp", "auth", "custom", "a2a", "genai", "payment", "edge", "firewall", "browser")
)
LocationSource = Literal["dbip", "cloudflare", "vercel", "source"]
LOCATION_SOURCES: frozenset[str] = frozenset(("dbip", "cloudflare", "vercel", "source"))

CaptureCode = Literal[
    "queue_full",
    "export_failed",
    "deadline",
    "closed",
    "handoff_refused",
    "entry_not_recording",
    "location_failed",
    "ignore_failed",
    "identity_outside_request",
    "invalid_performance",
]
Signal = Literal["traces", "logs", "context"]


class ConfigurationError(ValueError):
    """Options, identity or milestone input the SDK cannot use."""


class CaptureError(Exception):
    """Lost records or refused context. Holds no customer data.

    Passed to ``on_error``; the SDK never raises it into the application.
    """

    def __init__(self, code: CaptureCode, signal: Signal, count: int) -> None:
        super().__init__(f"Agentrail {signal} capture: {code} ({count} records).")
        self.code: CaptureCode = code
        self.signal: Signal = signal
        self.count = count


@dataclass(frozen=True, kw_only=True)
class CollectorOptions:
    """The collector's OTLP base URL and a backend source secret."""

    endpoint: str
    secret: str


@dataclass(frozen=True, kw_only=True)
class QueueOptions:
    """Per-signal delivery bounds. Times are seconds."""

    max_records: int = 2048
    batch_size: int = 128
    scheduled_delay: float = 1.0
    export_timeout: float = 5.0
    flush_timeout: float = 10.0
    shutdown_timeout: float = 10.0


@dataclass(frozen=True, kw_only=True)
class LogsOptions:
    """Message text and other fields leave only when named in ``include_fields``."""

    include_fields: Sequence[str] = ()
    exclude_uncorrelated: bool = False


@dataclass(frozen=True, kw_only=True)
class IPLocation:
    """An approximate network exit, never a verified place. ``None`` is unknown."""

    country: str | None = None
    region: str | None = None
    city: str | None = None
    source: LocationSource = "source"


@dataclass(frozen=True)
class IncomingRequest:
    """What the hooks see of an incoming request.

    ``headers`` has lowercase names; a repeated header's values are joined with
    ``", "``. ``client`` is the peer address the server saw, not a forwarded
    header. ``raw`` is the ASGI scope or the WSGI environ.
    """

    method: str
    target: str
    scheme: str
    host: str
    headers: Mapping[str, str]
    client: str | None
    raw: Mapping[str, object]


@dataclass(frozen=True, kw_only=True)
class Options:
    collector: CollectorOptions
    hash_key: str
    service_name: str
    service_instance_id: str | None = None
    outbound_origins: Sequence[str] = ()
    ignore_incoming_request: Callable[[IncomingRequest], bool] | None = None
    incoming_location: Callable[[IncomingRequest], IPLocation | None] | None = None
    logs: LogsOptions = field(default_factory=LogsOptions)
    queue: QueueOptions = field(default_factory=QueueOptions)
    propagator: TextMapPropagator | None = None
    on_error: Callable[[CaptureError], None] | None = None


@dataclass(frozen=True, kw_only=True)
class VerifiedIdentity:
    """An identity the application has already authenticated."""

    kind: PrincipalKind
    id: str
    acting_user_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class McpContext:
    method: str
    client: str | None = None
    client_version: str | None = None
    protocol: str | None = None


@dataclass(frozen=True, kw_only=True)
class AuthorizedOperation:
    """A business attempt the application has authorized.

    ``instance_ref`` is hashed locally and never exported.
    """

    namespace_id: str
    instance_ref: str
    role: Literal["start", "progress", "terminal"]


@dataclass(frozen=True, kw_only=True)
class PluginInvocation:
    """``invocation_id`` is opaque: never tool arguments or a customer identifier."""

    binding_id: str
    invocation_id: str


@dataclass(frozen=True, kw_only=True)
class PerformanceContext:
    """Workflow and plugin evidence. Supply ``operation`` with ``event_name``,
    ``operation`` with ``plugin``, or ``plugin`` alone."""

    environment: Literal["production", "test"]
    operation: AuthorizedOperation | None = None
    event_name: str | None = None
    plugin: PluginInvocation | None = None


@dataclass(frozen=True, kw_only=True)
class MilestoneContext:
    surface: Surface
    kind: str
    mcp: McpContext | None = None
    performance: PerformanceContext | None = None


@dataclass(frozen=True)
class Resolved:
    """Validated options with derived values."""

    input: Options
    endpoint: str
    endpoint_origin: str
    endpoint_path: str
    instance: str
    origins: frozenset[str]
    queue: QueueOptions
    include_fields: tuple[str, ...]

    def report(self, error: CaptureError) -> None:
        from ._suppression import reporting

        with reporting():
            callback = self.input.on_error
            if callback is None:
                print(error, file=sys.stderr)
                return
            try:
                callback(error)
            except Exception as callback_error:  # noqa: BLE001 - an application callback may raise anything
                print("Agentrail on_error callback failed:", repr(callback_error), error, file=sys.stderr)


def origin_of(parts: SplitResult) -> str:
    """Lowercase scheme and host, IPv6 in brackets, default port omitted."""
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def _url(value: str) -> SplitResult | None:
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises for an invalid port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment or "?" in value or "#" in value:
        return None
    return parts


def resolve(options: Options) -> Resolved:
    endpoint = _url(options.collector.endpoint)
    if endpoint is None:
        raise ConfigurationError("Use an absolute http(s) collector base URL without credentials, query or fragment.")
    secret = options.collector.secret
    if (
        not options.hash_key
        or not well_formed(options.hash_key)
        or not secret
        or any(character in secret for character in " \t\r\n")
        or not options.service_name
        or not valid_text(options.service_name, 4096)
        or (options.service_instance_id is not None and not options.service_instance_id)
        or (options.service_instance_id is not None and not valid_text(options.service_instance_id, 4096))
    ):
        raise ConfigurationError(
            "Configure a source secret, a UTF-8 workspace hash key and a service name "
            "(and instance ID) of well-formed text without NUL, at most 4096 characters."
        )
    queue = options.queue
    integers = (queue.max_records, queue.batch_size)
    times = (queue.scheduled_delay, queue.export_timeout, queue.flush_timeout, queue.shutdown_timeout)
    if (
        any(isinstance(value, bool) or value < 1 for value in integers)
        or any(isinstance(value, bool) or not value > 0 for value in times)
        or queue.batch_size > queue.max_records
        or queue.batch_size > 10_000
        or queue.export_timeout > min(queue.flush_timeout, queue.shutdown_timeout)
    ):
        raise ConfigurationError(
            "Use positive queue limits, batch_size <= max_records and 10000, "
            "and flush/shutdown timeouts >= export_timeout."
        )
    origins: set[str] = set()
    for raw in options.outbound_origins:
        parts = _url(raw)
        if parts is None or parts.path != "" or origin_of(parts) != raw:
            raise ConfigurationError("outbound_origins must contain exact canonical http(s) origins.")
        origins.add(raw)
    for name in options.logs.include_fields:
        if not isinstance(name, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ConfigurationError("logs.include_fields must contain field names.")
    trimmed = options.collector.endpoint.rstrip("/")
    return Resolved(
        input=options,
        endpoint=trimmed,
        endpoint_origin=origin_of(endpoint),
        endpoint_path=endpoint.path.rstrip("/"),
        instance=options.service_instance_id or uuid.uuid4().hex,
        origins=frozenset(origins),
        queue=queue,
        include_fields=tuple(options.logs.include_fields),
    )


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_EVENT = re.compile(r"[a-zA-Z][a-zA-Z0-9_.:-]*")


def valid_uuid(value: str) -> bool:
    return _UUID.fullmatch(value) is not None


def valid_event_name(value: str) -> bool:
    return len(value) <= 120 and _EVENT.fullmatch(value) is not None
