"""Plan 015 integration tests: headroom-ordered fallback ranking.

Uses ``httpx.MockTransport`` to stub upstreams and verify that:

1. When the primary is saturated and two fallbacks are available, enabling
   ``headroom_ranking`` routes the request to the fallback with the highest
   ``usage_headroom``.
2. With ``headroom_ranking = false`` (default), the request lands on the
   table-order first fallback.
"""

from __future__ import annotations

import asyncio
import json
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
async def test_headroom_ranking_routes_to_higher_headroom_fallback() -> None:
    """Primary saturated; headroom_ranking=true → higher-headroom fallback wins."""
    primary_calls = 0
    fallback_a_calls = 0
    fallback_b_calls = 0

    def primary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        return _sse_response()

    def fallback_a_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fallback_a_calls
        fallback_a_calls += 1
        return _sse_response()

    def fallback_b_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fallback_b_calls
        fallback_b_calls += 1
        return _sse_response()

    # Table order: fallback-a first, fallback-b second.
    # fallback-b has higher headroom (0.80 vs 0.20).
    primary_ctx = _make_mocked_ctx("umans", primary_handler, capacity=3)
    fallback_a_ctx = _make_mocked_ctx(
        "fallback-a", fallback_a_handler, requests_remaining=20, requests_limit=100
    )
    fallback_b_ctx = _make_mocked_ctx(
        "fallback-b", fallback_b_handler, requests_remaining=80, requests_limit=100
    )

    app = ProxyApp(
        providers={
            "umans": primary_ctx,
            "fallback-a": fallback_a_ctx,
            "fallback-b": fallback_b_ctx,
        },
        route_table=RouteTableManager(
            default_providers=("umans", "fallback-a", "fallback-b"),
        ),
        routing_config=RoutingConfig(headroom_ranking=True),
    )

    # Saturate the primary so it is BUSY and failover is active.
    await primary_ctx.gate.resize(0)

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200
    assert primary_calls == 0
    assert fallback_a_calls == 0
    assert fallback_b_calls == 1  # higher headroom served


@pytest.mark.asyncio
async def test_headroom_ranking_off_uses_table_order() -> None:
    """Same setup with headroom_ranking=false → table-order fallback wins."""
    primary_calls = 0
    fallback_a_calls = 0
    fallback_b_calls = 0

    def primary_handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        return _sse_response()

    def fallback_a_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fallback_a_calls
        fallback_a_calls += 1
        return _sse_response()

    def fallback_b_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fallback_b_calls
        fallback_b_calls += 1
        return _sse_response()

    primary_ctx = _make_mocked_ctx("umans", primary_handler, capacity=3)
    fallback_a_ctx = _make_mocked_ctx(
        "fallback-a", fallback_a_handler, requests_remaining=20, requests_limit=100
    )
    fallback_b_ctx = _make_mocked_ctx(
        "fallback-b", fallback_b_handler, requests_remaining=80, requests_limit=100
    )

    app = ProxyApp(
        providers={
            "umans": primary_ctx,
            "fallback-a": fallback_a_ctx,
            "fallback-b": fallback_b_ctx,
        },
        route_table=RouteTableManager(
            default_providers=("umans", "fallback-a", "fallback-b"),
        ),
        routing_config=RoutingConfig(headroom_ranking=False),
    )

    await primary_ctx.gate.resize(0)

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200
    assert primary_calls == 0
    assert fallback_a_calls == 1  # table-order first served
    assert fallback_b_calls == 0
