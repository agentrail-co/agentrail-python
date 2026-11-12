# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""The wire formats shared with agentrail-js and agentrail-go, checked against their vectors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentrail_python import _handoff as handoff
from agentrail_python._performance import operation_hash

DATA = Path(__file__).parent.parent / "testdata"
CONTRACT = json.loads((DATA / "sdk-contract.json").read_text("utf-8"))
JAVASCRIPT = json.loads((DATA / "interoperability" / "javascript.json").read_text("utf-8"))
MARKERS = json.loads((DATA / "interoperability" / "markers.json").read_text("utf-8"))
PERFORMANCE = json.loads((DATA / "interoperability" / "performance.json").read_text("utf-8"))


# Fails if someone changes the key ID, the handoff payload's JSON encoding or field
# order, or the identity hash: a handoff signed by the edge, Node or Go would then
# be refused here, and identities would no longer join across services.
@pytest.mark.parametrize("fixture", JAVASCRIPT, ids=[f["name"] for f in JAVASCRIPT])
def test_javascript_vectors(fixture: dict[str, object]) -> None:
    key, method, target = str(fixture["hashKey"]), str(fixture["method"]), str(fixture["target"])
    issued, parent = int(str(fixture["issuedAt"])), str(fixture["traceparent"])
    assert handoff.key_id(key) == fixture["hashKeyId"]
    assert handoff.sign(key, parent, method, target, issued) == fixture["token"]
    assert handoff.keyed_hash(key, str(fixture["hashValue"])) == fixture["hash"]
    verified = handoff.verify(key, str(fixture["token"]), parent, method, target, issued)
    assert handoff.format_traceparent(verified.trace_id, verified.span_id, verified.flags) == parent


# Fails if someone changes the contract's token format or the signed fields.
def test_collector_contract_handoff() -> None:
    contract = CONTRACT["traceHandoff"]
    assert contract["header"] == handoff.HANDOFF_HEADER
    assert contract["maxAgeMs"] == handoff.MAX_AGE_MS
    for fixture in contract["fixtures"]:
        given = fixture["input"]
        token = handoff.sign(
            fixture["hashKey"], given["traceparent"], given["method"], given["target"], given["issuedAt"]
        )
        assert token == fixture["token"]


# Fails if someone verifies a token for a different method, target, key or time,
# or accepts a token older than 60 seconds.
def test_verify_binds_request_and_time() -> None:
    fixture = CONTRACT["traceHandoff"]["fixtures"][0]
    key, given, token = fixture["hashKey"], fixture["input"], fixture["token"]
    parent, at = given["traceparent"], given["issuedAt"]

    def reason(**change: object) -> str:
        values: dict[str, object] = {
            "hash_key": key,
            "token": token,
            "traceparent": parent,
            "method": given["method"],
            "target": given["target"],
            "now": at,
        }
        values.update(change)
        with pytest.raises(handoff.Refused) as refused:
            handoff.verify(**values)  # type: ignore[arg-type]
        return refused.value.reason

    assert reason(method="GET") == "signature_invalid"
    assert reason(target=given["target"] + "&x=1") == "signature_invalid"
    assert reason(hash_key="another-key") == "key_mismatch"
    assert reason(traceparent="00-33333333333333333333333333333333-2222222222222222-01") == "context_mismatch"
    assert reason(now=at + handoff.MAX_AGE_MS + 1) == "expired"
    assert reason(now=at - 1) == "future"
    assert reason(token=token[:-1] + ("0" if token[-1] != "0" else "1")) == "signature_invalid"
    assert reason(token=None) == "missing"
    assert handoff.verify(key, token, parent, given["method"], given["target"], at + handoff.MAX_AGE_MS)


# Fails if someone changes how markers are folded or checked, so a marker the
# collector reads as valid would be stripped from URLs (or a non-marker kept).
def test_markers() -> None:
    for minted in CONTRACT["markers"]["minted"]:
        assert handoff.valid_marker(minted["marker"])
        assert handoff.valid_marker(minted["marker"].lower())
    for linked in CONTRACT["markers"]["linked"]:
        assert handoff.valid_marker(linked["linked"])
    for case in MARKERS:
        assert handoff.valid_marker(case["raw"]) is case["valid"], case["raw"]
    assert not handoff.valid_marker("getting-started")


# Fails if someone changes the operation hash input or encoding: workflow
# attempts reported by a Python service would never join those from Node or Go.
@pytest.mark.parametrize("fixture", PERFORMANCE, ids=[f["name"] for f in PERFORMANCE])
def test_operation_hash_vectors(fixture: dict[str, str]) -> None:
    assert operation_hash(fixture["hashKey"], fixture["namespaceId"], fixture["instanceRef"]) == fixture["hash"]
