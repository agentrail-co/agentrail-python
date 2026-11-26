# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Agentrail for Python: backend journey evidence over OpenTelemetry.

``agentrail = init(Options(...))``, the same with or without Sentry, Datadog or
an OpenTelemetry setup of your own: Agentrail registers no OpenTelemetry global.
"""

from ._client import Client, Diagnostics, MilestoneScope
from ._init import init
from ._options import (
    AuthorizedOperation,
    CaptureCode,
    CaptureError,
    CapturePayloads,
    CollectorOptions,
    ConfigurationError,
    IncomingRequest,
    IPLocation,
    LocationSource,
    LogsOptions,
    McpContext,
    McpPayload,
    MilestoneContext,
    Options,
    PerformanceContext,
    PluginInvocation,
    PrincipalKind,
    QueueOptions,
    Surface,
    VerifiedIdentity,
)
from ._privacy import StructuredLog
from ._queue import QueueStats
from ._version import VERSION

__version__ = VERSION
__all__ = [
    "VERSION",
    "AuthorizedOperation",
    "CaptureCode",
    "CaptureError",
    "CapturePayloads",
    "Client",
    "CollectorOptions",
    "ConfigurationError",
    "Diagnostics",
    "IPLocation",
    "IncomingRequest",
    "LocationSource",
    "LogsOptions",
    "McpContext",
    "McpPayload",
    "MilestoneContext",
    "MilestoneScope",
    "Options",
    "PerformanceContext",
    "PluginInvocation",
    "PrincipalKind",
    "QueueOptions",
    "QueueStats",
    "StructuredLog",
    "Surface",
    "VerifiedIdentity",
    "init",
]
