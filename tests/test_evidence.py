# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Identity, milestones and workflow evidence."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentrail_python import (
    AuthorizedOperation,
    ConfigurationError,
    MilestoneContext,
    PerformanceContext,
    PluginInvocation,
    VerifiedIdentity,
)
from agentrail_python._handoff import keyed_hash
from agentrail_python._performance import operation_hash
from support import HASH_KEY, by_name, recording

STEP = MilestoneContext(surface="custom", kind="checkout.confirm")
NAMESPACE = "11111111-1111-4111-8111-111111111111"
BINDING = "22222222-2222-4222-8222-222222222222"


def app_with(client: Any, handler: Any) -> TestClient:
    async def endpoint(request: Request) -> PlainTextResponse:
        await handler(request)
        return PlainTextResponse("ok")

    return TestClient(client.asgi(Starlette(routes=[Route("/{name}", endpoint)])))


# Fails if someone lets a later, different identity replace the first one, or
# drops the second assertion instead of recording it as its own auth step.
def test_conflicting_identity_keeps_the_first() -> None:
    with recording() as rec:

        async def handler(request: Request) -> None:
            rec.client.identify(VerifiedIdentity(kind="user", id="first-user", acting_user_id="acting"))
            rec.client.identify(VerifiedIdentity(kind="user", id="first-user", acting_user_id="acting"))
            rec.client.identify(VerifiedIdentity(kind="agent_registration", id="second-agent"))

        app_with(rec.client, handler).get("/x")
        spans = rec.spans()
        entry = by_name(spans, "HTTP GET")["attributes"]
        assert entry["agentrail.principal.kind"] == "user"
        assert entry["agentrail.principal.id_hash"] == keyed_hash(HASH_KEY, "first-user")
        assert entry["agentrail.principal.acting_user_hash"] == keyed_hash(HASH_KEY, "acting")
        asserted = by_name(spans, "identity.asserted")["attributes"]
        assert asserted["agentrail.step.surface"] == "auth"
        assert asserted["agentrail.principal.id_hash"] == keyed_hash(HASH_KEY, "second-agent")
        assert "first-user" not in repr(spans)


# Fails if someone shares identity state between concurrent requests.
def test_concurrent_requests_keep_their_own_identity() -> None:
    with recording() as rec:
        barrier = threading.Barrier(2)

        async def handler(request: Request) -> None:
            name = request.path_params["name"]
            rec.client.identify(VerifiedIdentity(kind="user", id=name))
            await asyncio.to_thread(barrier.wait, 5)
            rec.client.milestone("after", STEP)

        client = app_with(rec.client, handler)
        threads = [threading.Thread(target=client.get, args=(f"/{name}",)) for name in ("alice", "bob")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        spans = rec.spans()
        for span in spans:
            name = span["attributes"]["url.full"].rsplit("/", 1)[1] if span["name"] == "HTTP GET" else None
            if name is not None:
                assert span["attributes"]["agentrail.principal.id_hash"] == keyed_hash(HASH_KEY, name)
        by_trace = {span["trace_id"]: span for span in spans if span["name"] == "after"}
        entries = {span["trace_id"]: span for span in spans if span["name"] == "HTTP GET"}
        assert len(by_trace) == 2
        for trace_id, milestone in by_trace.items():
            assert (
                milestone["attributes"]["agentrail.principal.id_hash"]
                == entries[trace_id]["attributes"]["agentrail.principal.id_hash"]
            )


# Fails if someone accepts identity outside a managed operation instead of reporting it.
def test_identity_outside_a_request_is_reported() -> None:
    with recording() as rec:
        rec.client.identify(VerifiedIdentity(kind="user", id="u"))
        assert rec.codes() == ["identity_outside_request"]
        with pytest.raises(ConfigurationError):
            rec.client.identify(VerifiedIdentity(kind="admin", id="u"))  # type: ignore[arg-type]


class PaymentDeclinedError(Exception):
    pass


# Fails if someone swallows or replaces the application's exception or result,
# or stops recording the failure on the step.
def test_milestone_keeps_results_and_exceptions() -> None:
    with recording() as rec:
        with rec.client.start_milestone("ok", STEP):
            pass

        @rec.client.with_milestone("decorated", STEP)
        def decorated(value: int) -> int:
            return value * 2

        assert decorated(21) == 42
        assert decorated.__name__ == "decorated"
        error = PaymentDeclinedError("card 4242 declined")
        with pytest.raises(PaymentDeclinedError) as raised, rec.client.start_milestone("pay", STEP):
            raise error
        assert raised.value is error

        @rec.client.with_milestone("async", STEP)
        async def later() -> str:
            raise PaymentDeclinedError("async")

        with pytest.raises(PaymentDeclinedError, match="async"):
            asyncio.run(later())
        spans = rec.spans()
        assert by_name(spans, "decorated")["status"] == 0
        for name in ("pay", "async"):
            failed = by_name(spans, name)
            assert failed["status"] == 2
            assert failed["attributes"]["error.type"] == "PaymentDeclinedError"
        assert "4242" not in repr(spans)


# Fails if someone parents a standalone milestone on an unrelated caller's span
# instead of starting its own scope with a link, or nests supporting spans wrong.
def test_standalone_milestone_scope() -> None:
    with recording() as rec:
        outside = rec.provider.get_tracer("customer")
        with outside.start_as_current_span("customer job") as job, rec.client.start_milestone("job.step", STEP):
            with outside.start_as_current_span("supporting"):
                pass
            rec.client.milestone("instant", STEP)
        spans = rec.spans()
        step = by_name(spans, "job.step")
        context = job.get_span_context()
        assert step["parent_span_id"] == ""
        assert step["links"] == [(f"{context.trace_id:032x}", f"{context.span_id:016x}", "")]
        assert by_name(spans, "backend.operation")["parent_span_id"] == step["span_id"]
        assert by_name(spans, "instant")["parent_span_id"] == step["span_id"]
        assert "customer job" not in [span["name"] for span in spans]


# Fails if someone accepts an invalid milestone label.
@pytest.mark.parametrize("name", ["", "x" * 257, "bad\x00name", "\ud800"])
def test_invalid_milestone_names(name: str) -> None:
    with recording() as rec, pytest.raises(ConfigurationError):
        rec.client.milestone(name, STEP)


# Fails if someone changes the exported performance attributes, exports the raw
# operation reference, or lets workflow evidence spread to child spans.
def test_performance_evidence() -> None:
    with recording() as rec:
        performance = PerformanceContext(
            environment="production",
            operation=AuthorizedOperation(namespace_id=NAMESPACE, instance_ref="attempt-secret-77", role="start"),
            event_name="delivery.requested",
            plugin=PluginInvocation(binding_id=BINDING, invocation_id="opaque-invocation"),
        )
        with (
            rec.client.start_milestone(
                "delivery.requested", MilestoneContext(surface="custom", kind="business.event", performance=performance)
            ),
            rec.provider.get_tracer("db").start_as_current_span("child"),
        ):
            pass
        spans = rec.spans()
        assert (
            by_name(spans, "delivery.requested")["attributes"].items()
            >= {
                "agentrail.environment": "production",
                "agentrail.operation.namespace_id": NAMESPACE,
                "agentrail.operation.hash": operation_hash(HASH_KEY, NAMESPACE, "attempt-secret-77"),
                "agentrail.workflow.role": "start",
                "agentrail.workflow.event_name": "delivery.requested",
                "agentrail.plugin.binding_id": BINDING,
                "agentrail.plugin.invocation_id": "opaque-invocation",
            }.items()
        )
        assert not any(key.startswith("agentrail.") for key in by_name(spans, "backend.operation")["attributes"])
        assert "attempt-secret-77" not in repr(spans)


@pytest.mark.parametrize(
    "performance",
    [
        PerformanceContext(
            environment="production", plugin=PluginInvocation(binding_id="not-a-uuid", invocation_id="i")
        ),
        PerformanceContext(
            environment="production",
            operation=AuthorizedOperation(namespace_id=NAMESPACE, instance_ref="a", role="start"),
        ),
        PerformanceContext(
            environment="production",
            operation=AuthorizedOperation(namespace_id=NAMESPACE, instance_ref="a", role="start"),
            event_name="1-starts-with-digit",
        ),
    ],
)
# Fails if someone exports evidence the collector refuses (which loses the whole
# batch), or lets invalid evidence stop the milestone or the application.
def test_invalid_performance_is_dropped_and_reported(performance: PerformanceContext) -> None:
    with recording() as rec:
        ran = []
        with rec.client.start_milestone(
            "event", MilestoneContext(surface="custom", kind="business.event", performance=performance)
        ):
            ran.append(True)
        step = by_name(rec.spans(), "event")
        assert ran == [True]
        assert not any(key.startswith(("agentrail.operation", "agentrail.plugin")) for key in step["attributes"])
        assert rec.codes() == ["invalid_performance"]


# Fails if someone loses the managed scope across asyncio tasks or contextvars copies.
def test_scope_follows_tasks_and_copied_contexts() -> None:
    with recording() as rec:

        async def main() -> None:
            async with rec.client.start_milestone("parent", STEP):
                await asyncio.gather(asyncio.create_task(child("task")))
                context = contextvars.copy_context()
                await asyncio.to_thread(context.run, rec.client.milestone, "thread", STEP)

        async def child(name: str) -> None:
            rec.client.milestone(name, STEP)

        asyncio.run(main())
        spans = rec.spans()
        parent = by_name(spans, "parent")["span_id"]
        assert by_name(spans, "task")["parent_span_id"] == parent
        assert by_name(spans, "thread")["parent_span_id"] == parent
