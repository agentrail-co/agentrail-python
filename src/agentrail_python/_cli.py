# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""The header a customer's CLI sends its own API, as a server reads it.

``agentrail-cli-sdk`` writes ``x-agentrail-cli``, an RFC 9651 Dictionary naming
the command an agent ran, the run, the session and the agent. This reads it as
agentrail-core's ``readCliHeader`` and agentrail-go do (the contract's
``cliHeader``), and turns it into the span attributes the collector reads
(``otlp.cliAttributes``), the session only as its keyed hash.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import http_sf

from ._runtime import Attributes

CLI_HEADER = "x-agentrail-cli"

_TEXT = re.compile(r"[\x20-\x7e]{1,64}")
_COMMAND = re.compile(r"[\x20-\x7e]{1,128}")
_RUN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SESSION = re.compile(r"[0-9a-f]{32}")
_AGENT = re.compile(r"[a-z0-9._-]{1,64}")
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SESSION_SOURCES = ("agent", "ci", "local", "run")
_MODES = ("agent", "ci", "interactive", "non-interactive")
# The largest Integer RFC 9651 holds, and the most evidence a header names.
_MAX_INTEGER = 999_999_999_999_999
_MAX_EVIDENCE = 8


@dataclass(frozen=True)
class CliHeader:
    sdk_version: str
    cli: str
    run: str
    sequence: int
    session: str
    session_from: str
    mode: str
    cli_version: str | None = None
    agent: str | None = None
    agent_version: str | None = None
    evidence: tuple[str, ...] | None = None
    command: str | None = None


class _Unread(Exception):
    """A header no CLI SDK writes: it reads as nothing."""


def _item(members: Mapping[str, object], key: str) -> object:
    member = members.get(key)
    if not isinstance(member, tuple) or len(member) != 2:  # pyright: ignore[reportUnknownArgumentType]
        raise _Unread(key)
    return member[0]  # pyright: ignore[reportUnknownVariableType]


def _string(members: Mapping[str, object], key: str, pattern: re.Pattern[str]) -> str:
    value = _item(members, key)
    # A Token or a Display String is no String, though each holds text.
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise _Unread(key)
    return value


def _optional(members: Mapping[str, object], key: str, pattern: re.Pattern[str]) -> str | None:
    return _string(members, key, pattern) if key in members else None


def _token(members: Mapping[str, object], key: str, allowed: tuple[str, ...]) -> str:
    value = _item(members, key)
    if not isinstance(value, http_sf.Token) or str(value) not in allowed:
        raise _Unread(key)
    return str(value)


def _integer(members: Mapping[str, object], key: str, low: int, high: int) -> int:
    value = _item(members, key)
    # bool is an int in Python; a Boolean is no Integer.
    if type(value) is not int or not low <= value <= high:
        raise _Unread(key)
    return value


def _evidence(members: Mapping[str, object]) -> tuple[str, ...] | None:
    if "ev" not in members:
        return None
    items = _item(members, "ev")
    if not isinstance(items, list):
        raise _Unread("ev")
    entries: list[object] = items  # pyright: ignore[reportUnknownVariableType]
    if not 1 <= len(entries) <= _MAX_EVIDENCE:
        raise _Unread("ev")
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, tuple):
            raise _Unread("ev")
        parts: tuple[object, ...] = entry  # pyright: ignore[reportUnknownVariableType]
        name = parts[0]
        if type(name) is not str or _VARIABLE.fullmatch(name) is None:
            raise _Unread("ev")
        names.append(name)
    return tuple(names)


def read_cli_header(value: str | None) -> CliHeader | None:
    """What the header says, or None for none, or one that is not a CLI header of
    any version: not a Dictionary, a member missing, one outside the contract's
    rules, or one given twice, as two header lines a server joined are. Any ``v``
    from 1 is read, and members this version does not know are ignored."""
    if value is None:
        return None
    repeated: list[str] = []
    try:
        members = http_sf.parse(
            value.encode("latin-1"),
            tltype="dictionary",
            on_duplicate_key=lambda key, _context: repeated.append(key),
        )
    except (http_sf.StructuredFieldError, UnicodeEncodeError):
        return None
    if repeated or not isinstance(members, dict):
        return None
    fields: Mapping[str, object] = members
    try:
        _integer(fields, "v", 1, _MAX_INTEGER)
        return CliHeader(
            sdk_version=_string(fields, "sdk", _TEXT),
            cli=_string(fields, "cli", _TEXT),
            run=_string(fields, "run", _RUN),
            sequence=_integer(fields, "seq", 1, _MAX_INTEGER),
            session=_string(fields, "ses", _SESSION),
            session_from=_token(fields, "sesfrom", _SESSION_SOURCES),
            mode=_token(fields, "mode", _MODES),
            cli_version=_optional(fields, "cliv", _TEXT),
            agent=_optional(fields, "agent", _AGENT),
            agent_version=_optional(fields, "agentv", _TEXT),
            evidence=_evidence(fields),
            command=_optional(fields, "cmd", _COMMAND),
        )
    except _Unread:
        return None


def cli_attributes(value: str | None, keyed_hash: Callable[[str], str]) -> Attributes:
    """The request's CLI facts as span attributes; empty for a header that does not read."""
    header = read_cli_header(value)
    if header is None:
        return {}
    attributes: Attributes = {
        "agentrail.cli.name": header.cli,
        "agentrail.cli.run_id": header.run,
        "agentrail.cli.sequence": header.sequence,
        "agentrail.cli.mode": header.mode,
        "agentrail.cli.session.id_hash": keyed_hash(header.session),
        "agentrail.cli.session.source": header.session_from,
        "agentrail.cli.sdk.version": header.sdk_version,
    }
    for key, optional in (
        ("agentrail.cli.version", header.cli_version),
        ("agentrail.cli.command", header.command),
        ("agentrail.cli.agent.name", header.agent),
        ("agentrail.cli.agent.version", header.agent_version),
        ("agentrail.cli.agent.evidence", None if header.evidence is None else ",".join(header.evidence)),
    ):
        if optional is not None:
            attributes[key] = optional
    return attributes
