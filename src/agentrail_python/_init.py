# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Startup: one Agentrail installation per process, beside whatever tracer the application runs."""

from __future__ import annotations

import atexit
import threading

from ._client import Client
from ._options import ConfigurationError, Options
from ._processors import build

_lock = threading.Lock()
_installed: tuple[Options, Client] | None = None


def init(options: Options) -> Client:
    """Start Agentrail with its own tracer provider and export queues.

    Registers no OpenTelemetry global (tracer provider, logger provider,
    propagator or context): Sentry, Datadog or the application's own
    OpenTelemetry setup can start before or after it, unchanged. Call once at
    startup, before the application serves requests. What is still queued is
    sent when the interpreter exits; call ``shutdown`` yourself after your
    server stops accepting requests to bound it. Calling it again with the same
    options object returns the same client; other options are refused, since a
    second installation would record every request twice.
    """
    global _installed
    with _lock:
        if _installed is not None:
            if _installed[0] is not options:
                raise ConfigurationError("Agentrail is already initialized. Reuse the original options and client.")
            return _installed[1]
        client = build(options)
        atexit.register(client.shutdown)
        _installed = (options, client)
        return client
