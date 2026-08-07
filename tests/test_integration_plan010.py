"""Plan 010 integration tests: overload failover + model rewrite.

Uses ``httpx.MockTransport`` to stub upstreams and verify the full
request path: routing decision → body buffering → model rewrite →
overload classification → failover → cooldown clearing.
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
from switchboard.model_map import ModelMapManager
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
    """ASGI receive that returns the body, then blocks (client waiting).

    After the body is consumed, subsequent calls block indefinitely —
    simulating a client waiting for the response.  The disconnect_watcher
    task is cancelled when ``_forward`` returns.
    """

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
        reading=LimitState(provider="generic", age_seconds=0.0),
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


def _make_model_map_mgr() -> ModelMapManager:
    mgr = ModelMapManager()
    mgr.set_model(
        "umans-kimi-k2.7",
        {"umans": "umans-kimi-k2.7", "ollama-cloud": "kimi-k2.7-code"},
    )
    return mgr


@pytest.mark.asyncio
async def test_overload_failover_with_model_rewrite() -> None:
    """3x503 from umans -> overload cooldown -> failover to ollama-cloud
    with the model field rewritten from umans-kimi-k2.7 to kimi-k2.7-code."""
    umans_bodies: list[bytes] = []
    ollama_bodies: list[bytes] = []
    umans_503_count = 0

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_503_count
        umans_bodies.append(request.content)
        if umans_503_count < 3:
            umans_503_count += 1
            return httpx.Response(
                503, headers={"retry-after": "5"}, text="overloaded"
            )
        return httpx.Response(200, text="ok")

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        ollama_bodies.append(request.content)
        return httpx.Response(200, text="ok")

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx("ollama-cloud", ollama_handler)

    route_table = RouteTableManager(
        default_providers=("umans", "ollama-cloud"),
    )
    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama-cloud": ollama_ctx},
        route_table=route_table,
        routing_config=RoutingConfig(),
        overload_config=OverloadConfig(threshold=3, cooldown_default=30.0),
        model_map_mgr=_make_model_map_mgr(),
    )

    body = _make_chat_body()

    # Send 3 requests that get 503 from umans.
    for _ in range(3):
        scope = _make_scope(body=body)
        receive = _MockReceive(body=body)
        messages, send = _make_send()
        await app(scope, receive, send)
        status, _ = _parse_response(messages)
        assert status == 503

    # Umans should now be in overload cooldown.
    assert app._overload_tracker.is_cooling("umans", now=time.monotonic())

    # 4th request should failover to ollama-cloud with model rewritten.
    scope = _make_scope(body=body)
    receive = _MockReceive(body=body)
    messages, send = _make_send()
    await app(scope, receive, send)
    status, _ = _parse_response(messages)
    assert status == 200

    # Verify ollama-cloud received the request with rewritten model.
    assert len(ollama_bodies) == 1
    ollama_json = json.loads(ollama_bodies[0])
    assert ollama_json["model"] == "kimi-k2.7-code"

    # Umans received 3 requests with the original model (no rewrite on primary).
    assert len(umans_bodies) == 3
    for ub in umans_bodies:
        uj = json.loads(ub)
        assert uj["model"] == "umans-kimi-k2.7"

    await umans_ctx.http_client.aclose()
    await ollama_ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_200_clears_overload_cooldown() -> None:
    """After cooldown lapses, a 200 from umans clears the overload state."""
    umans_503_count = 0

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_503_count
        if umans_503_count < 3:
            umans_503_count += 1
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, text="ok")

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx("ollama-cloud", ollama_handler)

    route_table = RouteTableManager(
        default_providers=("umans", "ollama-cloud"),
    )
    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama-cloud": ollama_ctx},
        route_table=route_table,
        routing_config=RoutingConfig(),
        overload_config=OverloadConfig(
            threshold=3, cooldown_default=0.1, cooldown_min=0.01
        ),
        model_map_mgr=_make_model_map_mgr(),
    )

    body = _make_chat_body()

    # Trigger cooldown with 3x503.
    for _ in range(3):
        scope = _make_scope(body=body)
        receive = _MockReceive(body=body)
        messages, send = _make_send()
        await app(scope, receive, send)

    assert app._overload_tracker.is_cooling("umans", now=time.monotonic())

    # Wait for cooldown to lapse.
    await asyncio.sleep(0.2)

    assert not app._overload_tracker.is_cooling("umans", now=time.monotonic())

    # Next request should go to umans (primary, available again).
    # Umans handler returns 200 now, which calls record_ok.
    scope = _make_scope(body=body)
    receive = _MockReceive(body=body)
    messages, send = _make_send()
    await app(scope, receive, send)
    status, _ = _parse_response(messages)
    assert status == 200

    # Counter should be reset.
    assert app._overload_tracker.consecutive("umans") == 0

    await umans_ctx.http_client.aclose()
    await ollama_ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_model_not_in_map_no_filtering() -> None:
    """Request with an unmapped model → no filtering, no rewrite, normal routing."""
    umans_bodies: list[bytes] = []

    def umans_handler(request: httpx.Request) -> httpx.Response:
        umans_bodies.append(request.content)
        return httpx.Response(200, text="ok")

    umans_ctx = _make_mocked_ctx("umans", umans_handler)

    route_table = RouteTableManager(default_providers=("umans",))
    app = ProxyApp(
        providers={"umans": umans_ctx},
        route_table=route_table,
        routing_config=RoutingConfig(),
        model_map_mgr=_make_model_map_mgr(),
    )

    body = json.dumps(
        {"model": "some-unmapped-model", "messages": []}
    ).encode()

    scope = _make_scope(body=body)
    receive = _MockReceive(body=body)
    messages, send = _make_send()
    await app(scope, receive, send)
    status, _ = _parse_response(messages)
    assert status == 200

    # Model should NOT be rewritten (not in map).
    assert len(umans_bodies) == 1
    data = json.loads(umans_bodies[0])
    assert data["model"] == "some-unmapped-model"

    await umans_ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_no_model_map_full_passthrough() -> None:
    """No model_map configured → body streamed, not buffered (byte-transparent)."""
    umans_bodies: list[bytes] = []

    def umans_handler(request: httpx.Request) -> httpx.Response:
        umans_bodies.append(request.content)
        return httpx.Response(200, text="ok")

    umans_ctx = _make_mocked_ctx("umans", umans_handler)

    route_table = RouteTableManager(default_providers=("umans",))
    app = ProxyApp(
        providers={"umans": umans_ctx},
        route_table=route_table,
        routing_config=RoutingConfig(),
    )
    assert app._model_map_mgr is not None
    assert app._model_map_mgr.get_model_map().routes == {}

    body = _make_chat_body()

    scope = _make_scope(body=body)
    receive = _MockReceive(body=body)
    messages, send = _make_send()
    await app(scope, receive, send)
    status, _ = _parse_response(messages)
    assert status == 200

    # Body should be forwarded as-is (byte-transparent).
    assert len(umans_bodies) == 1
    assert umans_bodies[0] == body

    await umans_ctx.http_client.aclose()
