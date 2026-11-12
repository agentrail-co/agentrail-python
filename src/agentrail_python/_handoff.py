# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""The signed trace handoff, keyed hashes and response markers.

Byte-compatible with agentrail-core (JavaScript) and agentrail-go: the same key
ID, the same identity hash, the same handoff payload and token, and the same
marker reading. ``tests/test_interoperability.py`` holds this to the shared
vectors in ``testdata/interoperability``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from ._text import js_json, well_formed

HANDOFF_HEADER = "x-agentrail-trace-handoff"
# Headers a managed request never forwards as received.
OWNED_HEADERS = ("traceparent", "tracestate", "baggage", "sentry-trace", HANDOFF_HEADER)
MAX_AGE_MS = 60_000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991

_HEX16 = re.compile(r"[0-9a-f]{16}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TIME = re.compile(r"0|[1-9][0-9]{0,15}")
_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_INVALID_TRACE = "0" * 32
_INVALID_SPAN = "0" * 16
# ECMAScript String.prototype.trim whitespace, which core's parser applies.
_JS_WHITESPACE = "".join(
    chr(code)
    for code in (
        *range(0x09, 0x0E),
        0x20,
        0xA0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    )
)
KELVIN_SIGN, LONG_S = chr(0x212A), chr(0x017F)
_METHOD_SYMBOLS = frozenset("!#$%&'*+.^_`|~-")


def key_id(hash_key: str) -> str:
    """The first 16 lowercase hex characters of the key's SHA-256."""
    return hashlib.sha256(hash_key.encode("utf-8")).hexdigest()[:16]


def keyed_hash(hash_key: str, value: str) -> str:
    """HMAC-SHA256 of the value under the workspace key, first 32 hex characters."""
    return _mac(hash_key, value.encode("utf-8")).hex()[:32]


def _mac(hash_key: str, payload: bytes) -> bytes:
    return hmac.new(hash_key.encode("utf-8"), payload, hashlib.sha256).digest()


def _js_trim(value: str) -> str:
    return value.strip(_JS_WHITESPACE)


@dataclass(frozen=True)
class TraceParent:
    trace_id: int
    span_id: int
    flags: int


def parse_traceparent(header: str | None) -> TraceParent | None:
    """Version-00 ``traceparent``, as core reads it: trimmed, lowercase, no zero IDs."""
    if header is None:
        return None
    match = _TRACEPARENT.fullmatch(_js_trim(header))
    if match is None:
        return None
    trace_hex, span_hex, flags = match.groups()
    if trace_hex == _INVALID_TRACE or span_hex == _INVALID_SPAN:
        return None
    return TraceParent(int(trace_hex, 16), int(span_hex, 16), int(flags, 16))


def format_traceparent(trace_id: int, span_id: int, flags: int) -> str:
    return f"00-{trace_id:032x}-{span_id:016x}-{flags & 0xFF:02x}"


def _valid_method(method: str) -> bool:
    if not 1 <= len(method) <= 64:
        return False
    for character in method:
        # JavaScript /[a-z]/iu also folds the Kelvin sign and the long s.
        if (
            character in (KELVIN_SIGN, LONG_S)
            or ("a" <= character <= "z")
            or ("A" <= character <= "Z")
            or ("0" <= character <= "9")
            or character in _METHOD_SYMBOLS
        ):
            continue
        return False
    return True


def _valid_request(method: str, target: str) -> bool:
    return (
        _valid_method(method)
        and target.startswith("/")
        and well_formed(target)
        and not any(character in target for character in "\x00\r\n#")
    )


def _payload(hash_key_id: str, issued_at: int, traceparent: str, method: str, target: str) -> bytes:
    body = js_json([1, hash_key_id, issued_at, traceparent, method, target])
    return f"agentrail.trace-handoff\x00{body}".encode()


class HandoffInputError(ValueError):
    """The values to sign do not form a valid handoff."""


def sign(hash_key: str, traceparent: str, method: str, target: str, issued_at: int) -> str:
    """A token binding the trace context to the exact method and wire target."""
    if (
        not well_formed(hash_key)
        or not _valid_request(method, target)
        or not 0 <= issued_at <= _MAX_SAFE_INTEGER
        or traceparent != _js_trim(traceparent)
        or parse_traceparent(traceparent) is None
    ):
        raise HandoffInputError
    identifier = key_id(hash_key)
    signature = _mac(hash_key, _payload(identifier, issued_at, traceparent, method, target))
    return f"ah1.{identifier}.{issued_at}.{traceparent}.{signature.hex()}"


class Refused(Exception):
    """Why a handoff did not verify. The reason is one of core's refusal names."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def verify(
    hash_key: str,
    token: str | None,
    traceparent: str | None,
    method: str,
    target: str,
    now: int,
) -> TraceParent:
    """The remote parent of a valid handoff; raises ``Refused`` otherwise."""
    if token is None or token == "":
        raise Refused("missing")
    if len(token) > 256 or not _valid_request(method, target) or not well_formed(hash_key):
        raise Refused("malformed")
    parts = token.split(".")
    if (
        len(parts) != 5
        or parts[0] != "ah1"
        or not _HEX16.fullmatch(parts[1])
        or not _TIME.fullmatch(parts[2])
        or not _HEX64.fullmatch(parts[4])
    ):
        raise Refused("malformed")
    issued_at = int(parts[2])
    parent = parse_traceparent(parts[3]) if parts[3] == _js_trim(parts[3]) else None
    if issued_at > _MAX_SAFE_INTEGER or parent is None:
        raise Refused("malformed")
    if parts[1] != key_id(hash_key):
        raise Refused("key_mismatch")
    if parts[3] != traceparent:
        raise Refused("context_mismatch")
    age = now - issued_at
    if age < 0:
        raise Refused("future")
    if age > MAX_AGE_MS:
        raise Refused("expired")
    expected = _mac(hash_key, _payload(parts[1], issued_at, parts[3], method, target))
    if not hmac.compare_digest(bytes.fromhex(parts[4]), expected):
        raise Refused("signature_invalid")
    return parent


_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def valid_marker(raw: str) -> bool:
    """The collector's marker reading: Crockford folding, then a Luhn mod-32 check."""
    # str.upper applies full Unicode case mapping, as String.prototype.toUpperCase does.
    text = raw.upper().replace("-", "").replace("I", "1").replace("L", "1").replace("O", "0")
    if len(text) not in (14, 16):
        return False
    values: list[int] = []
    for character in text:
        value = _ALPHABET.find(character)
        if value < 0:
            return False
        values.append(value)
    check = values.pop()
    total, factor = 0, 2
    for value in reversed(values):
        addend = factor * value
        total += addend // 32 + addend % 32
        factor = 1 if factor == 2 else 2
    return check == (32 - total % 32) % 32
