# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Standalone startup: Agentrail owns the process's OpenTelemetry providers."""

from __future__ import annotations

import threading
from dataclasses import replace

from opentelemetry import _logs as otel_logs
from opentelemetry import propagate, trace
from opentelemetry._logs._internal import ProxyLoggerProvider  # pyright: ignore[reportPrivateImportUsage]
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from ._client import Client
from ._options import ConfigurationError, Options
from ._processors import Processors

_lock = threading.Lock()
_installed: tuple[Options, Client] | None = None


def init(options: Options) -> Client:
    """Install Agentrail's tracer provider, logger provider and propagator globally.

    Call once at startup, before the application serves requests. The
    providers drain when the interpreter exits; call ``shutdown`` yourself after
    your server stops accepting requests to bound it. Calling it
    again with the same options object returns the same client. If the process
    already has an OpenTelemetry provider, build it with ``create_processors``
    instead.
    """
    global _installed
    with _lock:
        if _installed is not None:
            if _installed[0] is not options:
                raise ConfigurationError("Agentrail is already initialized. Reuse the original options and client.")
            return _installed[1]
        if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            raise ConfigurationError(
                "A tracer provider is already installed. Wire create_processors into its construction instead."
            )
        if not isinstance(otel_logs.get_logger_provider(), ProxyLoggerProvider):
            raise ConfigurationError(
                "A logger provider is already installed. Use create_processors with the existing setup."
            )
        processors = Processors(options if options.propagator is not None else _with_base(options))
        provider = TracerProvider(sampler=processors.sampler(ALWAYS_ON))
        provider.add_span_processor(processors.span_processor)
        logger = LoggerProvider()
        logger.add_log_record_processor(processors.log_processor)
        client = processors.bind(provider)
        trace.set_tracer_provider(provider)
        otel_logs.set_logger_provider(logger)
        propagate.set_global_textmap(processors.propagator)
        client._on_shutdown.extend((provider.shutdown, logger.shutdown))  # pyright: ignore[reportPrivateUsage]
        _installed = (options, client)
        return client


def _with_base(options: Options) -> Options:
    """Unmanaged work keeps the process's existing propagator."""
    return replace(options, propagator=propagate.get_global_textmap())
