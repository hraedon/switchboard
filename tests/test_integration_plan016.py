"""Plan 016 integration tests: opportunistic quota-burn routing.

Uses ``httpx.MockTransport`` to stub upstreams and verifies that:

1. When a healthy primary and a quota-bearing fallback with high headroom
   and a near-term reset are available, enabling ``opportunistic_enabled``
   routes the request to the fallback.
2. A fallback whose reset is outside ``opportunistic_reset_window`` does not
   trigger opportunism.
3. A fallback whose headroom is below ``opportunistic_min_headroom`` does
   not trigger opportunism.
4. With ``opportunistic_enabled = false`` the primary serves regardless.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from sluice.control import BreakerConfig, ControllerConfig, LimitState
from sluice.gate import PermitGate
from sluice.providers import NullTruthSource
from sluice.reconcile import ReconciliationLoop
from sluice.usage import CachedReading

from switchboard.control import RoutingConfig
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.route_table import RouteTableManager


def _make_scope(body: bytes = b"") -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer test-key")],
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
            return {
                "type": "http.request",
                "body": self._body,
                "more_body": False,
            }
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


def _sse_response() -> httpx.Response:
    chunks = [
        b'data: {"id":"1","choices":[{"delta":{"content":"Hi"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    class _SSEStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            for c in chunks:
                yield c

        async def aclose(self) -> None:
            pass

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_SSEStream(),
    )


def _make_mocked_ctx(
    name: str,
    handler: Any,
    *,
    capacity: int = 3,
    requests_remaining: int | None = None,
    requests_limit: int | None = None,
    bucket_reset_epoch: float | None = None,
) -> ProviderContext:
    gate = PermitGate(initial_capacity=capacity)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        controller_config=ControllerConfig(target=capacity),
        breaker_config=BreakerConfig(),
    )
    reconcile._first_poll_ok = True
    reconcile._last_reading_cached = CachedReading(
        reading=LimitState(
            provider="generic",
            age_seconds=0.0,
            requests_remaining=requests_remaining,
            requests_limit=requests_limit,
            bucket_reset_epoch=bucket_reset_epoch,
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


async def _send_request(app: ProxyApp, body: bytes) -> tuple[int, bytes]:
    scope = _make_scope(body=body)
    receive = _MockReceive(body=body)
    messages, send = _make_send()
    await app(scope, receive, send)
    return _parse_response(messages)


@pytest.mark.asyncio
async def test_opportunistic_routes_to_high_headroom_near_reset_fallback() -> None:
    """Healthy primary + fallback at 70% headroom with reset in 3h → fallback wins."""
    primary_calls = 0
    fallback_calls = 0

    def primary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        return _sse_response()

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fallback_calls
        fallback_calls += 1
        return _sse_response()

    primary_ctx = _make_mocked_ctx("umans", primary_handler, capacity=3)
    fallback_ctx = _make_mocked_ctx(
        "zai",
        fallback_handler,
        requests_remaining=70,
        requests_limit=100,
        bucket_reset_epoch=time.time() + 10800.0,
    )

    app = ProxyApp(
        providers={"umans": primary_ctx, "zai": fallback_ctx},
        route_table=RouteTableManager(default_providers=("umans", "zai")),
        routing_config=RoutingConfig(
            opportunistic_enabled=True,
            opportunistic_min_headroom=0.5,
            opportunistic_reset_window=21600.0,
            opportunistic_margin=0.10,
        ),
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200
    assert primary_calls == 0
    assert fallback_calls == 1


@pytest.mark.asyncio
async def test_opportunistic_outside_reset_window_primary_serves() -> None:
    """Reset ~20h out (outside 6h window) → primary serves."""
    primary_calls = 0
    fallback_calls = 0

    def primary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        return _sse_response()

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fallback_calls
        fallback_calls += 1
        return _sse_response()

    primary_ctx = _make_mocked_ctx("umans", primary_handler, capacity=3)
    fallback_ctx = _make_mocked_ctx(
        "zai",
        fallback_handler,
        requests_remaining=70,
        requests_limit=100,
        bucket_reset_epoch=time.time() + 72000.0,
    )

    app = ProxyApp(
        providers={"umans": primary_ctx, "zai": fallback_ctx},
        route_table=RouteTableManager(default_providers=("umans", "zai")),
        routing_config=RoutingConfig(
            opportunistic_enabled=True,
            opportunistic_min_headroom=0.5,
            opportunistic_reset_window=21600.0,
            opportunistic_margin=0.10,
        ),
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200
    assert primary_calls == 1
    assert fallback_calls == 0


@pytest.mark.asyncio
async def test_opportunistic_low_headroom_primary_serves() -> None:
    """Fallback headroom 0.3 (< 0.5 floor) → primary serves."""
    primary_calls = 0
    fallback_calls = 0

    def primary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        return _sse_response()

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fallback_calls
        fallback_calls += 1
        return _sse_response()

    primary_ctx = _make_mocked_ctx("umans", primary_handler, capacity=3)
    fallback_ctx = _make_mocked_ctx(
        "zai",
        fallback_handler,
        requests_remaining=30,
        requests_limit=100,
        bucket_reset_epoch=time.time() + 10800.0,
    )

    app = ProxyApp(
        providers={"umans": primary_ctx, "zai": fallback_ctx},
        route_table=RouteTableManager(default_providers=("umans", "zai")),
        routing_config=RoutingConfig(
            opportunistic_enabled=True,
            opportunistic_min_headroom=0.5,
            opportunistic_reset_window=21600.0,
            opportunistic_margin=0.10,
        ),
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200
    assert primary_calls == 1
    assert fallback_calls == 0


@pytest.mark.asyncio
async def test_opportunistic_disabled_primary_serves() -> None:
    """Same healthy-fallback setup with opportunism disabled → primary serves."""
    primary_calls = 0
    fallback_calls = 0

    def primary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        return _sse_response()

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fallback_calls
        fallback_calls += 1
        return _sse_response()

    primary_ctx = _make_mocked_ctx("umans", primary_handler, capacity=3)
    fallback_ctx = _make_mocked_ctx(
        "zai",
        fallback_handler,
        requests_remaining=70,
        requests_limit=100,
        bucket_reset_epoch=time.time() + 10800.0,
    )

    app = ProxyApp(
        providers={"umans": primary_ctx, "zai": fallback_ctx},
        route_table=RouteTableManager(default_providers=("umans", "zai")),
        routing_config=RoutingConfig(opportunistic_enabled=False),
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200
    assert primary_calls == 1
    assert fallback_calls == 0
