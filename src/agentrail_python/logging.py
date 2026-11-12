# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""A ``logging.Handler`` that records structured logs, correlated with the request.

``logging.getLogger().addHandler(AgentrailHandler(agentrail))``. The record is
captured where it is emitted, so it carries that request's trace. Other
handlers' output is unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ._privacy import StructuredLog

if TYPE_CHECKING:
    from ._client import Client

# Attributes every LogRecord has; anything else came from ``extra=``.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime", "taskName"})


def severity_number(level: int) -> int:
    """OpenTelemetry's mapping of a logging level to a severity number."""
    if level >= logging.CRITICAL:
        return 21
    if level >= logging.ERROR:
        return 17
    if level >= logging.WARNING:
        return 13
    if level >= logging.INFO:
        return 9
    if level >= logging.DEBUG:
        return 5
    return 1


class AgentrailHandler(logging.Handler):
    """Message text and ``extra`` fields leave only when named in ``logs.include_fields``.

    ``message`` is the formatted message. ``event.name`` (or ``event_name``) in
    ``extra`` names the event; an exception's class name becomes ``error.type``.
    """

    def __init__(self, client: Client, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self._client = client

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields: dict[str, object] = {"message": record.getMessage()}
            for key, value in vars(record).items():
                if key not in _STANDARD:
                    fields[key] = value
            event = fields.get("event.name", fields.get("event_name"))
            error = record.exc_info[1] if record.exc_info else None
            explicit = fields.get("error.type")
            self._client.log(
                StructuredLog(
                    severity_number=severity_number(record.levelno),
                    severity_text=record.levelname,
                    error_type=explicit
                    if isinstance(explicit, str)
                    else (type(error).__qualname__ if error is not None else None),
                    event_name=event if isinstance(event, str) else None,
                    fields=fields,
                )
            )
        except Exception:  # noqa: BLE001 - logging's own contract: report through handleError
            self.handleError(record)
