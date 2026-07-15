"""Plan 012 integration tests: token-cap aware switching.

Uses ``httpx.MockTransport`` to stub upstreams and verify that:

1. SSE responses with ``usage`` are observed read-only and fed to the
   budget tracker.
2. When a provider's projected token utilization crosses the threshold, it
   is demoted from immediate to queue-eligible (BUSY).
3. The primary is never demoted by token budget.
4. With ``token_budget_threshold = 0.0``, no filtering occurs.
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

from switchboard.budget import TokenBudgetConfig
from switchboard.control import RoutingConfig
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.route_table import RouteTableManager
from switchboard.token_budget import TokenBudgetTracker


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


def _sse_response(
    prompt_tokens: int = 100,
    completion_tokens: int = 200,
) -> httpx.Response:
    """Build an SSE response with a usage chunk (stream= for MockTransport)."""
    chunks = [
        b'data: {"id":"1","choices":[{"delta":{"content":"Hi"}}]}\n\n',
        (
            b'data: {"id":"1","choices":[],"usage":'
            b'{"prompt_tokens":'
            + str(prompt_tokens).encode()
            + b',"completion_tokens":'
            + str(completion_tokens).encode()
            + b'}}\n\n'
        ),
        b"data: [DONE]\n\n",
    ]

    class _SSEStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for c in chunks:
                yield c

        async def aclose(self) -> None:
            pass

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_SSEStream(),
    )


def _make_budget_tracker(
    cap: int = 1000,
    *,
    provider: str = "ollama-cloud",
) -> TokenBudgetTracker:
    return TokenBudgetTracker(
        configs={
            provider: TokenBudgetConfig(
                cap_tokens=cap,
                window_seconds=3600.0,
                soft_threshold=0.85,
            )
        },
    )


async def _send_request(
    app: ProxyApp, body: bytes
) -> tuple[int, bytes]:
    scope = _make_scope(body=body)
    receive = _MockReceive(body=body)
    messages, send = _make_send()
    await app(scope, receive, send)
    return _parse_response(messages)


@pytest.mark.asyncio
async def test_sse_usage_recorded_into_tracker() -> None:
    """A streaming SSE response with usage → tracker records the tokens."""
    tracker = _make_budget_tracker(cap=10000)

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(prompt_tokens=100, completion_tokens=200)

    ctx = _make_mocked_ctx("ollama-cloud", handler)
    app = ProxyApp(
        providers={"ollama-cloud": ctx},
        route_table=RouteTableManager(
            default_providers=("ollama-cloud",),
        ),
        budget_tracker=tracker,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200

    own = tracker.own_tokens(
        "ollama-cloud", now=time.monotonic()
    )
    assert own == 300


@pytest.mark.asyncio
async def test_token_budget_demotes_non_primary() -> None:
    """When ollama-cloud is over its token budget and umans is CLOSED,
    ollama-cloud is demoted to queue (BUSY) rather than immediate failover."""
    tracker = _make_budget_tracker(cap=100, provider="ollama-cloud")
    # Pre-fill the tracker so ollama-cloud is at 90% utilization.
    now = time.monotonic()
    tracker.record_usage(
        "ollama-cloud", 45, 50, now=now
    )  # 95 tokens / 100 cap = 0.95

    umans_503_count = 0

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_503_count
        umans_503_count += 1
        return httpx.Response(503, headers={"retry-after": "5"}, text="overload")

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(10, 10)

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx("ollama-cloud", ollama_handler)

    app = ProxyApp(
        providers={
            "umans": umans_ctx,
            "ollama-cloud": ollama_ctx,
        },
        route_table=RouteTableManager(
            default_providers=("umans", "ollama-cloud"),
        ),
        routing_config=RoutingConfig(token_budget_threshold=0.85),
        budget_tracker=tracker,
        queue_timeout=0.0,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()

    # Trigger overload cooldown on umans (3x 503).
    for _ in range(3):
        await _send_request(app, body)

    assert app._overload_tracker.is_cooling("umans", now=time.monotonic())

    # Now umans is CLOSED and ollama-cloud is over budget → 503, not failover.
    status, _ = await _send_request(app, body)
    assert status == 503


@pytest.mark.asyncio
async def test_token_budget_primary_never_demoted() -> None:
    """umans AVAILABLE but over budget → still serves (primary never demoted)."""
    tracker = _make_budget_tracker(cap=100, provider="umans")
    tracker.record_usage("umans", 50, 50, now=time.monotonic())

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(1, 1)

    umans_ctx = _make_mocked_ctx("umans", handler)

    app = ProxyApp(
        providers={"umans": umans_ctx},
        route_table=RouteTableManager(
            default_providers=("umans",),
        ),
        routing_config=RoutingConfig(token_budget_threshold=0.85),
        budget_tracker=tracker,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200


@pytest.mark.asyncio
async def test_token_budget_threshold_zero_noop() -> None:
    """token_budget_threshold=0.0 → no filtering even when over budget."""
    tracker = _make_budget_tracker(cap=100, provider="ollama-cloud")
    tracker.record_usage(
        "ollama-cloud", 50, 50, now=time.monotonic()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(1, 1)

    ollama_ctx = _make_mocked_ctx("ollama-cloud", handler)

    app = ProxyApp(
        providers={"ollama-cloud": ollama_ctx},
        route_table=RouteTableManager(
            default_providers=("ollama-cloud",),
        ),
        routing_config=RoutingConfig(token_budget_threshold=0.0),
        budget_tracker=tracker,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200


@pytest.mark.asyncio
async def test_non_streaming_json_usage_recorded() -> None:
    """A non-streaming JSON response with usage -> tracker records tokens."""
    tracker = _make_budget_tracker(cap=10000)
    json_body = json.dumps({
        "id": "1",
        "choices": [{"message": {"content": "Hi"}}],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 75,
            "total_tokens": 125,
        },
    }).encode()

    class _JSONStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json_body

        async def aclose(self) -> None:
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_JSONStream(),
        )

    ctx = _make_mocked_ctx("ollama-cloud", handler)
    app = ProxyApp(
        providers={"ollama-cloud": ctx},
        route_table=RouteTableManager(
            default_providers=("ollama-cloud",),
        ),
        budget_tracker=tracker,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200

    own = tracker.own_tokens("ollama-cloud", now=time.monotonic())
    assert own == 125
