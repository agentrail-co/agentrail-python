# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Workflow and plugin evidence on a milestone, as agentrail-node emits it."""

from __future__ import annotations

import hashlib
import hmac

from ._options import ConfigurationError, PerformanceContext, valid_event_name, valid_uuid
from ._runtime import Attributes
from ._text import js_json, utf16_length, well_formed


def operation_hash(hash_key: str, namespace_id: str, instance_ref: str) -> str:
    """HMAC-SHA256 of ``["agentrail.operation", namespace, reference]`` as JSON, full hex."""
    payload = js_json(["agentrail.operation", namespace_id, instance_ref]).encode("utf-8")
    return hmac.new(hash_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def performance_attributes(performance: PerformanceContext, hash_key: str) -> Attributes:
    """Only normalized, allowlisted evidence. Raises ``ConfigurationError`` for invalid input."""
    if performance.environment not in ("production", "test"):
        raise ConfigurationError("Use the production or test environment.")
    operation, plugin = performance.operation, performance.plugin
    if plugin is None and (operation is None or performance.event_name is None):
        raise ConfigurationError("Supply an operation with its event name, a plugin invocation, or both.")
    if operation is None and performance.event_name is not None:
        raise ConfigurationError("An event name needs its authorized operation.")
    attributes: Attributes = {"agentrail.environment": performance.environment}
    if plugin is not None:
        invocation = plugin.invocation_id
        if (
            not valid_uuid(plugin.binding_id)
            or not 1 <= utf16_length(invocation) <= 256
            or not well_formed(invocation)
            or "\x00" in invocation
        ):
            raise ConfigurationError("Use a canonical binding ID and an opaque invocation ID of 1-256 characters.")
        attributes["agentrail.plugin.binding_id"] = plugin.binding_id
        attributes["agentrail.plugin.invocation_id"] = invocation
    if operation is not None:
        if (
            not valid_uuid(operation.namespace_id)
            or not operation.instance_ref
            or not well_formed(operation.instance_ref)
            or operation.role not in ("start", "progress", "terminal")
        ):
            raise ConfigurationError(
                "Use a canonical namespace ID, a nonempty well-formed operation reference and a role."
            )
        attributes["agentrail.operation.namespace_id"] = operation.namespace_id
        attributes["agentrail.operation.hash"] = operation_hash(
            hash_key, operation.namespace_id, operation.instance_ref
        )
        attributes["agentrail.workflow.role"] = operation.role
        if performance.event_name is not None:
            if not valid_event_name(performance.event_name):
                raise ConfigurationError(
                    "Use a business event name of 1-120 letters, digits, dots, underscores, "
                    "colons or hyphens, starting with a letter."
                )
            attributes["agentrail.workflow.event_name"] = performance.event_name
    return attributes
