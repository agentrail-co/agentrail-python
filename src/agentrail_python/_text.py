# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Text limits counted the way the collector and the JavaScript SDKs count them.

Limits are UTF-16 code units, because the collector and agentrail-node measure
JavaScript string length. JSON is encoded exactly as ``JSON.stringify`` does for
well-formed strings, so hashes and signatures agree across the SDKs.
"""

from __future__ import annotations

import json


def utf16_length(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def well_formed(value: str) -> bool:
    """True when the string has no lone surrogate, so it encodes as UTF-8."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def valid_text(value: str, limit: int) -> bool:
    """Well-formed, without NUL, and at most ``limit`` UTF-16 units."""
    return well_formed(value) and "\x00" not in value and utf16_length(value) <= limit


def bounded(value: str, limit: int) -> str:
    """Drop NUL, replace lone surrogates and cut to ``limit`` UTF-16 units.

    A cut never splits a code point.
    """
    cleaned = value.encode("utf-8", "replace").decode("utf-8").replace("\x00", "")
    units = 0
    for index, character in enumerate(cleaned):
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > limit:
            return cleaned[:index]
        units += width
    return cleaned


def js_json(value: object) -> str:
    """``JSON.stringify`` for strings, finite numbers, booleans, null and lists of them."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
