"""Plan 013 integration tests: trailing-24h usage routing.

Uses ``httpx.MockTransport`` to stub upstreams and verify that:

1. When the primary's trailing-24h utilization crosses the configured
   threshold, traffic fails over to the fallback — the primary is
   demoted (the ONE proactive signal allowed to do so, Plan 013 §2).
2. With ``usage_24h_threshold = 0.0`` (default), no filtering occurs.
3. No data (no successful fetch) → fail safe, no filtering.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from switchboard.control import RoutingConfig
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig, CachedReading, LimitState
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource
from switchboard.usage_history import UsageHistoryTracker


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


def _make_mocked_ctx(name: str, handler: Any, capacity: int = 3) -> ProviderContext:
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


def _make_tracker(
    *,
    provider: str = "umans",
    cap: int = 1_000_000,
    tokens_24h: int | None = None,
) -> UsageHistoryTracker:
    """A UsageHistoryTracker pre-loaded with a trailing-24h total."""
    tracker = UsageHistoryTracker()
    tracker.register(
        provider,
        base_url="https://api.example.com",
        api_key="k",
        cap_tokens=cap,
    )
    if tokens_24h is not None:
        snap = tracker.snapshot(provider)
        assert snap is not None
        snap.tokens_24h = tokens_24h
    return tracker


async def _send_request(app: ProxyApp, body: bytes) -> tuple[int, bytes]:
    scope = _make_scope(body=body)
    receive = _MockReceive(body=body)
    messages, send = _make_send()
    await app(scope, receive, send)
    return _parse_response(messages)


@pytest.mark.asyncio
async def test_primary_over_24h_threshold_fails_over() -> None:
    """umans at 90% of its trailing-24h cap → traffic bleeds to
    ollama-cloud; umans never sees the request."""
    umans_calls = 0

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_calls
        umans_calls += 1
        return _sse_response()

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        return _sse_response()

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx("ollama-cloud", ollama_handler)
    tracker = _make_tracker(cap=1_000_000, tokens_24h=900_000)

    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama-cloud": ollama_ctx},
        route_table=RouteTableManager(
            default_providers=("umans", "ollama-cloud"),
        ),
        routing_config=RoutingConfig(usage_24h_threshold=0.85),
        usage_history_tracker=tracker,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200
    assert umans_calls == 0  # primary de-preferred — fallback served


@pytest.mark.asyncio
async def test_primary_under_threshold_serves() -> None:
    """umans at 50% of cap → serves as normal (no rerouting)."""
    umans_calls = 0

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_calls
        umans_calls += 1
        return _sse_response()

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx("ollama-cloud", lambda r: _sse_response())
    tracker = _make_tracker(cap=1_000_000, tokens_24h=500_000)

    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama-cloud": ollama_ctx},
        route_table=RouteTableManager(
            default_providers=("umans", "ollama-cloud"),
        ),
        routing_config=RoutingConfig(usage_24h_threshold=0.85),
        usage_history_tracker=tracker,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200
    assert umans_calls == 1


@pytest.mark.asyncio
async def test_threshold_zero_noop() -> None:
    """usage_24h_threshold=0.0 (default) → no filtering even at 99%."""
    umans_calls = 0

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_calls
        umans_calls += 1
        return _sse_response()

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx("ollama-cloud", lambda r: _sse_response())
    tracker = _make_tracker(cap=1_000_000, tokens_24h=990_000)

    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama-cloud": ollama_ctx},
        route_table=RouteTableManager(
            default_providers=("umans", "ollama-cloud"),
        ),
        routing_config=RoutingConfig(),  # default: feature off
        usage_history_tracker=tracker,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200
    assert umans_calls == 1


@pytest.mark.asyncio
async def test_no_data_no_filtering() -> None:
    """Fail safe: tracker registered but no successful fetch → umans
    serves (never route on bad information)."""
    umans_calls = 0

    def umans_handler(request: httpx.Request) -> httpx.Response:
        nonlocal umans_calls
        umans_calls += 1
        return _sse_response()

    umans_ctx = _make_mocked_ctx("umans", umans_handler)
    ollama_ctx = _make_mocked_ctx("ollama-cloud", lambda r: _sse_response())
    # tokens_24h=None — no data landed yet.
    tracker = _make_tracker(cap=1_000_000, tokens_24h=None)

    app = ProxyApp(
        providers={"umans": umans_ctx, "ollama-cloud": ollama_ctx},
        route_table=RouteTableManager(
            default_providers=("umans", "ollama-cloud"),
        ),
        routing_config=RoutingConfig(usage_24h_threshold=0.85),
        usage_history_tracker=tracker,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200
    assert umans_calls == 1
