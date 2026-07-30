"""Plan 014 integration tests: failback hysteresis + affinity observability.

Uses ``httpx.MockTransport`` to stub upstreams and verifies that:

1. After a failover, a recently-healthy primary does not cause immediate
   failback when ``failback_delay`` is configured (hysteresis holds the pin).
2. When the primary's continuous healthy interval exceeds ``failback_delay``,
   traffic fails back and the affinity pin is released.
3. The affinity pin/failback counters are incremented correctly.
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


def _make_mocked_ctx(name: str, handler: Any, capacity: int = 3) -> ProviderContext:
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


async def _send_request(app: ProxyApp, body: bytes) -> tuple[int, bytes]:
    scope = _make_scope(body=body)
    receive = _MockReceive(body=body)
    messages, send = _make_send()
    await app(scope, receive, send)
    return _parse_response(messages)


@pytest.mark.asyncio
async def test_failback_hysteresis_holds_pin() -> None:
    """Primary becomes healthy after failover but continuity < failback_delay.

    The affinity pin must stay in place and the fallback keeps serving.
    """
    umans_calls = 0
    ollama_calls = 0

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_calls
        umans_calls += 1
        return _sse_response()

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        nonlocal ollama_calls
        ollama_calls += 1
        return _sse_response()

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx("ollama", ollama_handler)

    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama": ollama_ctx},
        route_table=RouteTableManager(
            default_providers=("umans", "ollama"),
        ),
        # dwell_interval=0 so the second request is post-dwell; hysteresis
        # is the only thing keeping the pin.
        routing_config=RoutingConfig(failback_delay=60.0, dwell_interval=0.0),
    )

    body = json.dumps({"model": "test", "messages": []}).encode()

    # First request: umans is BUSY (capacity 0), so traffic fails over to ollama.
    await umans_ctx.gate.resize(0)
    status, _ = await _send_request(app, body)
    assert status == 200
    assert umans_calls == 0
    assert ollama_calls == 1
    assert app.metrics.affinity_pins_total == 1
    assert app.metrics.affinity_failbacks_total == 0
    assert app._affinity

    # Second request: umans is healthy again, but only just. Hysteresis must
    # hold the pin because continuity is younger than failback_delay.
    await umans_ctx.gate.resize(3)
    status, _ = await _send_request(app, body)
    assert status == 200
    assert umans_calls == 0
    assert ollama_calls == 2
    # A RouteAffinity entry is recorded on every non-primary acquisition.
    assert app.metrics.affinity_pins_total == 2
    assert app.metrics.affinity_failbacks_total == 0


@pytest.mark.asyncio
async def test_failback_hysteresis_releases_after_delay() -> None:
    """Primary has been continuously healthy longer than failback_delay.

    Traffic should fail back to the primary and the affinity pin released.
    """
    umans_calls = 0
    ollama_calls = 0

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_calls
        umans_calls += 1
        return _sse_response()

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        nonlocal ollama_calls
        ollama_calls += 1
        return _sse_response()

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx("ollama", ollama_handler)

    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama": ollama_ctx},
        route_table=RouteTableManager(
            default_providers=("umans", "ollama"),
        ),
        routing_config=RoutingConfig(failback_delay=60.0, dwell_interval=0.0),
    )

    body = json.dumps({"model": "test", "messages": []}).encode()

    # First request: failover to ollama.
    await umans_ctx.gate.resize(0)
    status, _ = await _send_request(app, body)
    assert status == 200
    assert umans_calls == 0
    assert ollama_calls == 1
    assert app.metrics.affinity_pins_total == 1
    assert app.metrics.affinity_failbacks_total == 0

    # Simulate that umans has been continuously healthy for longer than the
    # configured failback delay.
    await umans_ctx.gate.resize(3)
    app._provider_healthy_since["umans"] = time.monotonic() - 120.0
    status, _ = await _send_request(app, body)
    assert status == 200
    assert umans_calls == 1
    assert ollama_calls == 1
    assert app.metrics.affinity_pins_total == 1
    assert app.metrics.affinity_failbacks_total == 1
    assert not app._affinity
