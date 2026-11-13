# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Agentrail for Python: backend journey evidence over OpenTelemetry.

Standalone: ``agentrail = init(Options(...))``. With an existing OpenTelemetry
setup: ``create_processors(Options(...))``, then ``bind`` your provider.
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
from ._processors import Processors, create_processors
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
    "Processors",
    "QueueOptions",
    "QueueStats",
    "StructuredLog",
    "Surface",
    "VerifiedIdentity",
    "create_processors",
    "init",
]
