"""Plan 011 integration tests: usage-aware failover.

Uses ``httpx.MockTransport`` to stub upstreams and verify that
``headroom_threshold`` causes low-headroom providers to be demoted from
immediate failover to queue-eligible.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from switchboard.control import RoutingConfig
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig, CachedReading, LimitState
from switchboard.overload import OverloadConfig
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource


def _make_scope(
    method: str = "POST",
    path: str = "/v1/chat/completions",
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [(b"authorization", b"Bearer test-key")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }


class _MockReceive:
    def __init__(self, body: bytes = b"") -> None:
        self._body = body
        self._sent = False

    async def __call__(self) -> dict[str, Any]:
        if not self._sent:
            self._sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await asyncio.Future()
        return {"type": "http.disconnect"}


def _make_send() -> tuple[list[dict], Any]:
    messages: list[dict] = []

    async def send(msg: dict) -> None:
        messages.append(msg)

    return messages, send


def _parse_response(messages: list[dict]) -> tuple[int, bytes]:
    status = 0
    body = b""
    for msg in messages:
        if msg["type"] == "http.response.start":
            status = msg["status"]
        elif msg["type"] == "http.response.body":
            body += msg.get("body", b"")
    return status, body


def _make_mocked_ctx(
    name: str,
    handler: Any,
    capacity: int = 3,
    *,
    requests_remaining: int | None = None,
    requests_limit: int | None = None,
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
    reconcile._first_poll_ok = True
    reconcile._last_reading_cached = CachedReading(
        reading=LimitState(
            provider="generic",
            age_seconds=0.0,
            requests_remaining=requests_remaining,
            requests_limit=requests_limit,
        ),
        fetched_at_monotonic=0.0,
        ok=True,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ProviderContext(
        name=name,
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=client,
    )


def _make_chat_body(model: str = "umans-kimi-k2.7") -> bytes:
    return json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    ).encode()


async def _send_request(
    app: ProxyApp, body: bytes
) -> tuple[int, bytes]:
    scope = _make_scope(body=body)
    receive = _MockReceive(body=body)
    messages, send = _make_send()
    await app(scope, receive, send)
    return _parse_response(messages)


async def _trigger_overload_cooldown(app: ProxyApp, body: bytes) -> None:
    for _ in range(3):
        await _send_request(app, body)


@pytest.mark.asyncio
async def test_low_headroom_demotes_failover_to_queue() -> None:
    """umans CLOSED (overload cooldown) + ollama-cloud AVAILABLE with low
    headroom → ollama-cloud demoted to queue-eligible. With queue_timeout=0
    the request gets a 503 instead of immediate failover."""
    umans_503_count = 0
    ollama_hit = False

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_503_count
        if umans_503_count < 3:
            umans_503_count += 1
            return httpx.Response(
                503, headers={"retry-after": "5"}, text="overloaded"
            )
        return httpx.Response(200, text="ok")

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        nonlocal ollama_hit
        ollama_hit = True
        return httpx.Response(200, text="ok")

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx(
        "ollama-cloud",
        ollama_handler,
        requests_remaining=5,
        requests_limit=100,
    )

    route_table = RouteTableManager(
        default_providers=("umans", "ollama-cloud"),
    )
    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama-cloud": ollama_ctx},
        route_table=route_table,
        routing_config=RoutingConfig(headroom_threshold=0.15),
        overload_config=OverloadConfig(threshold=3, cooldown_default=300.0),
        queue_timeout=0.0,
    )

    body = _make_chat_body()

    await _trigger_overload_cooldown(app, body)
    assert app._overload_tracker.is_cooling("umans", now=time.monotonic())

    status, _ = await _send_request(app, body)
    assert status == 503
    assert not ollama_hit

    await umans_ctx.http_client.aclose()
    await ollama_ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_good_headroom_allows_immediate_failover() -> None:
    """umans CLOSED + ollama-cloud AVAILABLE with good headroom →
    immediate failover to ollama-cloud."""
    umans_503_count = 0
    ollama_hit = False

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_503_count
        if umans_503_count < 3:
            umans_503_count += 1
            return httpx.Response(
                503, headers={"retry-after": "5"}, text="overloaded"
            )
        return httpx.Response(200, text="ok")

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        nonlocal ollama_hit
        ollama_hit = True
        return httpx.Response(200, text="ok")

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx(
        "ollama-cloud",
        ollama_handler,
        requests_remaining=50,
        requests_limit=100,
    )

    route_table = RouteTableManager(
        default_providers=("umans", "ollama-cloud"),
    )
    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama-cloud": ollama_ctx},
        route_table=route_table,
        routing_config=RoutingConfig(headroom_threshold=0.15),
        overload_config=OverloadConfig(threshold=3, cooldown_default=300.0),
        queue_timeout=0.0,
    )

    body = _make_chat_body()

    await _trigger_overload_cooldown(app, body)
    assert app._overload_tracker.is_cooling("umans", now=time.monotonic())

    status, _ = await _send_request(app, body)
    assert status == 200
    assert ollama_hit

    await umans_ctx.http_client.aclose()
    await ollama_ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_primary_unaffected_by_headroom() -> None:
    """umans AVAILABLE + ollama-cloud low headroom → routes to umans
    (primary is never demoted by headroom)."""
    umans_hit = False
    ollama_hit = False

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_hit
        umans_hit = True
        return httpx.Response(200, text="ok")

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        nonlocal ollama_hit
        ollama_hit = True
        return httpx.Response(200, text="ok")

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx(
        "ollama-cloud",
        ollama_handler,
        requests_remaining=5,
        requests_limit=100,
    )

    route_table = RouteTableManager(
        default_providers=("umans", "ollama-cloud"),
    )
    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama-cloud": ollama_ctx},
        route_table=route_table,
        routing_config=RoutingConfig(headroom_threshold=0.15),
    )

    body = _make_chat_body()

    status, _ = await _send_request(app, body)
    assert status == 200
    assert umans_hit
    assert not ollama_hit

    await umans_ctx.http_client.aclose()
    await ollama_ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_headroom_threshold_zero_no_filtering() -> None:
    """headroom_threshold=0.0 → no filtering (today's behaviour).
    Even with low headroom, ollama-cloud is AVAILABLE and gets failover
    traffic."""
    umans_503_count = 0
    ollama_hit = False

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_503_count
        if umans_503_count < 3:
            umans_503_count += 1
            return httpx.Response(
                503, headers={"retry-after": "5"}, text="overloaded"
            )
        return httpx.Response(200, text="ok")

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        nonlocal ollama_hit
        ollama_hit = True
        return httpx.Response(200, text="ok")

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx(
        "ollama-cloud",
        ollama_handler,
        requests_remaining=5,
        requests_limit=100,
    )

    route_table = RouteTableManager(
        default_providers=("umans", "ollama-cloud"),
    )
    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama-cloud": ollama_ctx},
        route_table=route_table,
        routing_config=RoutingConfig(headroom_threshold=0.0),
        overload_config=OverloadConfig(threshold=3, cooldown_default=300.0),
        queue_timeout=0.0,
    )

    body = _make_chat_body()

    await _trigger_overload_cooldown(app, body)
    assert app._overload_tracker.is_cooling("umans", now=time.monotonic())

    status, _ = await _send_request(app, body)
    assert status == 200
    assert ollama_hit

    await umans_ctx.http_client.aclose()
    await ollama_ctx.http_client.aclose()
