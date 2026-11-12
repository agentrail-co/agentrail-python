# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Work the SDK does for itself is never captured.

Error reporting and export run under this flag so a callback that logs, or an
exporter that logs its own failures, cannot feed the queue it reports on.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry import context as otel_context

_flag: ContextVar[bool] = ContextVar("agentrail_python_suppressed", default=False)
_OTEL_KEY = otel_context._SUPPRESS_INSTRUMENTATION_KEY  # pyright: ignore[reportPrivateUsage]


def suppressed() -> bool:
    return _flag.get()


@contextmanager
def reporting() -> Generator[None]:
    """Run SDK-internal work with capture and OTel instrumentation suppressed."""
    flag = _flag.set(True)
    token = otel_context.attach(otel_context.set_value(_OTEL_KEY, True))
    try:
        yield
    finally:
        otel_context.detach(token)
        _flag.reset(flag)
