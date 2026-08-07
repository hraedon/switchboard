"""ProviderManager lifecycle tests (Plan 020 WI-2).

The load-bearing behaviors: removal deregisters immediately but never
kills in-flight work; drains are bounded; replace is atomic; and the
routing core shrugs when an affinity pin names a provider that no longer
exists.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from switchboard.control import (
    Availability,
    ProviderState,
    RouteAffinity,
    RouteTable,
    RoutingConfig,
    SignalFreshness,
    route_decision,
)
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.provider_manager import ProviderManager
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource


def _make_ctx(
    name: str = "test",
    capacity: int = 1,
    http_client: httpx.AsyncClient | None = None,
) -> ProviderContext:
    gate = PermitGate(initial_capacity=capacity)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        max_concurrency=capacity,
        provider_type="generic",
        breaker_config=BreakerConfig(),
    )
    return ProviderContext(
        name=name,
        upstream_url=f"https://{name}.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=http_client or httpx.AsyncClient(),
    )


async def _ready(ctx: ProviderContext) -> None:
    await ctx.reconcile.tick()


# ---------------------------------------------------------------- manager


@pytest.mark.asyncio
async def test_add_makes_provider_visible() -> None:
    mgr = ProviderManager({})
    ctx = _make_ctx("alpha")
    await mgr.add("alpha", ctx)
    try:
        assert mgr.providers["alpha"] is ctx
    finally:
        await mgr.remove("alpha")
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_add_duplicate_raises() -> None:
    ctx = _make_ctx("alpha")
    mgr = ProviderManager({"alpha": ctx})
    with pytest.raises(ValueError):
        await mgr.add("alpha", _make_ctx("alpha"))
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_remove_deregisters_immediately_and_closes() -> None:
    ctx = _make_ctx("alpha")
    mgr = ProviderManager({"alpha": ctx})
    assert await mgr.remove("alpha") is True
    # Deregistration is synchronous with the call; teardown is not.
    assert "alpha" not in mgr.providers
    await mgr.shutdown()
    assert ctx.http_client.is_closed


@pytest.mark.asyncio
async def test_double_remove_is_noop() -> None:
    ctx = _make_ctx("alpha")
    mgr = ProviderManager({"alpha": ctx})
    assert await mgr.remove("alpha") is True
    assert await mgr.remove("alpha") is False
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_drain_waits_for_held_permit() -> None:
    ctx = _make_ctx("alpha", capacity=2)
    mgr = ProviderManager({"alpha": ctx}, drain_timeout=5.0)
    assert await ctx.gate.acquire(timeout=0.5)
    await mgr.remove("alpha")
    await asyncio.sleep(0.3)
    # The permit is still held: the client must not be closed yet.
    assert not ctx.http_client.is_closed
    await ctx.gate.release()
    await mgr.shutdown()
    assert ctx.http_client.is_closed


@pytest.mark.asyncio
async def test_drain_timeout_closes_anyway(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = _make_ctx("alpha", capacity=2)
    mgr = ProviderManager({"alpha": ctx}, drain_timeout=0.2)
    assert await ctx.gate.acquire(timeout=0.5)
    with caplog.at_level(logging.WARNING, "switchboard.provider_manager"):
        await mgr.remove("alpha")
        await mgr.shutdown()
    assert ctx.http_client.is_closed
    assert any("drain timeout" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_removed_gate_stops_new_grants() -> None:
    ctx = _make_ctx("alpha", capacity=3)
    mgr = ProviderManager({"alpha": ctx}, drain_timeout=5.0)
    assert await ctx.gate.acquire(timeout=0.5)
    await mgr.remove("alpha")
    await asyncio.sleep(0.15)  # let the drain task resize the gate
    # Capacity was 3 with 1 held; after resize(0) nothing new is granted.
    assert await ctx.gate.acquire(timeout=0.0) is False
    await ctx.gate.release()
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_replace_swaps_atomically_and_drains_old() -> None:
    old = _make_ctx("alpha")
    new = _make_ctx("alpha")
    mgr = ProviderManager({"alpha": old}, drain_timeout=5.0)
    await mgr.replace("alpha", new)
    assert mgr.providers["alpha"] is new
    await mgr.shutdown()
    assert old.http_client.is_closed
    assert not new.http_client.is_closed
    await mgr.remove("alpha")
    await mgr.shutdown()


# ---------------------------------------------------------------- routing core


def _fresh_state(name: str) -> ProviderState:
    return ProviderState(
        name=name,
        availability=Availability.AVAILABLE,
        available_permits=2,
        queue_depth=0,
        retry_after_seconds=None,
        signal_freshness=SignalFreshness.FRESH,
    )


def test_affinity_to_missing_provider_is_ignored() -> None:
    """An affinity pin naming a removed provider must not front it or crash."""
    states = {"beta": _fresh_state("beta")}
    table = RouteTable(default_providers=("beta",))
    plan = route_decision(
        states,
        table,
        "route-key",
        RoutingConfig(),
        now=1000.0,
        affinity=RouteAffinity(provider="gone", selected_at=999.0),
    )
    assert plan.immediate_candidates
    assert plan.immediate_candidates[0] == "beta"


# ---------------------------------------------------------------- proxy level


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], gate: asyncio.Event | None = None):
        self._chunks = chunks
        self._gate = gate

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i, chunk in enumerate(self._chunks):
            if i > 0 and self._gate is not None:
                await self._gate.wait()
            yield chunk

    async def aclose(self) -> None:
        return None


def _make_scope(path: str = "/v1/chat/completions") -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }


def _make_receive(body: bytes = b"{}") -> Any:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    return receive


def _make_send() -> tuple[list[dict[str, Any]], Any]:
    messages: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        messages.append(msg)

    return messages, send


@pytest.mark.asyncio
async def test_inflight_stream_survives_removal() -> None:
    """A response mid-stream keeps streaming after its provider is removed."""
    release = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_Stream([b"data: one\n", b"data: two\n"], gate=release),
            headers={"content-type": "text/event-stream"},
        )

    ctx = _make_ctx(
        "alpha",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=None
        ),
    )
    await _ready(ctx)
    app = ProxyApp(
        providers={"alpha": ctx},
        route_table=RouteTableManager(default_providers=("alpha",)),
        routing_config=RoutingConfig(),
    )

    messages, send = _make_send()
    task = asyncio.create_task(app(_make_scope(), _make_receive(), send))

    # Wait until the first body chunk reached the client.
    for _ in range(100):
        if any(
            m["type"] == "http.response.body" and m.get("body")
            for m in messages
        ):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("first chunk never arrived")

    assert await app.provider_manager.remove("alpha") is True
    assert "alpha" not in app.provider_manager.providers

    release.set()
    await asyncio.wait_for(task, timeout=5.0)

    body = b"".join(
        m.get("body", b"")
        for m in messages
        if m["type"] == "http.response.body"
    )
    assert body == b"data: one\ndata: two\n"
    status = next(
        m["status"] for m in messages if m["type"] == "http.response.start"
    )
    assert status == 200
    await app.provider_manager.shutdown()


@pytest.mark.asyncio
async def test_no_admission_after_removal() -> None:
    """A request arriving after removal is served by the surviving provider."""
    served_by: list[str] = []

    def make_handler(name: str) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            served_by.append(name)
            return httpx.Response(
                200,
                stream=_Stream([b'{"ok": true}']),
                headers={"content-type": "application/json"},
            )

        return handler

    alpha = _make_ctx(
        "alpha",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(make_handler("alpha")), timeout=None
        ),
    )
    beta = _make_ctx(
        "beta",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(make_handler("beta")), timeout=None
        ),
    )
    await _ready(alpha)
    await _ready(beta)
    app = ProxyApp(
        providers={"alpha": alpha, "beta": beta},
        route_table=RouteTableManager(default_providers=("alpha", "beta")),
        routing_config=RoutingConfig(),
    )

    await app.provider_manager.remove("alpha")

    messages, send = _make_send()
    await app(_make_scope(), _make_receive(), send)
    status = next(
        m["status"] for m in messages if m["type"] == "http.response.start"
    )
    assert status == 200
    assert served_by == ["beta"]
    await app.provider_manager.shutdown()
