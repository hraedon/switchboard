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
import sqlite3
import time
from typing import Any

import httpx
import pytest

from switchboard.budget import TokenBudgetConfig
from switchboard.control import RoutingConfig
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig, CachedReading, LimitState
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.speed import SpeedSampler
from switchboard.token_budget import TokenBudgetTracker
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


# --- Persistence / reboot survival (wall-clock fix) -------------------------


def _budget_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE token_usage "
        "(provider TEXT, timestamp REAL, tokens INTEGER)"
    )
    return db


def test_token_budget_persists_and_reloads_within_window() -> None:
    """Samples written to SQLite reload into a fresh tracker (process
    restart, same boot) within the window."""
    cfg = TokenBudgetConfig(cap_tokens=10000, window_seconds=3600.0)
    db = sqlite3.connect(":memory:")
    t1 = TokenBudgetTracker(configs={"p": cfg}, db=db)
    t1.record_usage("p", 100, 200, now=time.monotonic())
    db.commit()

    t2 = TokenBudgetTracker(configs={"p": cfg}, db=db)
    t2.load()
    assert t2.own_tokens("p", now=time.monotonic()) == 300


def test_token_budget_reboot_drops_stale_samples() -> None:
    """Regression: timestamps persisted as monotonic used to survive a system
    reboot (monotonic resets to ~0) and poison the window for ~25h. The
    wall_ts column now ages by the wall clock: pre-reboot rows older than the
    window are dropped on load, not reloaded as fresh."""
    cfg = TokenBudgetConfig(cap_tokens=10000, window_seconds=3600.0)
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE token_usage "
        "(provider TEXT, timestamp REAL, tokens INTEGER, wall_ts REAL)"
    )
    # A sample written "2 hours ago" (wall clock) — older than the 1h window.
    old_wall = time.time() - 7200.0
    db.execute(
        "INSERT INTO token_usage (provider, timestamp, tokens, wall_ts) "
        "VALUES (?, ?, ?, ?)",
        ("p", 999999.0, 500, old_wall),  # huge monotonic ts, old wall ts
    )
    db.commit()

    t = TokenBudgetTracker(configs={"p": cfg}, db=db)
    t.load()
    # Stale sample must NOT load.
    assert t.own_tokens("p", now=time.monotonic()) == 0


def test_token_budget_reboot_keeps_recent_samples() -> None:
    """A sample within the window (by wall clock) reloads and is translated
    into the current monotonic frame so in-memory pruning agrees."""
    cfg = TokenBudgetConfig(cap_tokens=10000, window_seconds=3600.0)
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE token_usage "
        "(provider TEXT, timestamp REAL, tokens INTEGER, wall_ts REAL)"
    )
    recent_wall = time.time() - 600.0  # 10 min ago, inside the 1h window
    db.execute(
        "INSERT INTO token_usage (provider, timestamp, tokens, wall_ts) "
        "VALUES (?, ?, ?, ?)",
        ("p", 999999.0, 400, recent_wall),
    )
    db.commit()

    t = TokenBudgetTracker(configs={"p": cfg}, db=db)
    t.load()
    assert t.own_tokens("p", now=time.monotonic()) == 400
    # And it ages out correctly thereafter.
    t._prune("p", time.monotonic() + 7200.0)
    assert t.own_tokens("p", now=time.monotonic() + 7200.0) == 0


def test_token_budget_pre_fix_rows_without_wall_ts_skipped() -> None:
    """Rows from before the wall_ts migration have NULL wall_ts and cannot be
    aged reliably — they are skipped on load (fail-safe) and purged."""
    cfg = TokenBudgetConfig(cap_tokens=10000, window_seconds=3600.0)
    db = _budget_db()  # no wall_ts column at all → migration adds it
    t = TokenBudgetTracker(configs={"p": cfg}, db=db)
    # Insert a legacy row directly (NULL wall_ts after migration).
    db.execute(
        "INSERT INTO token_usage (provider, timestamp, tokens) "
        "VALUES (?, ?, ?)",
        ("p", 12345.0, 777),
    )
    db.commit()
    t.load()
    assert t.own_tokens("p", now=time.monotonic()) == 0


def test_soft_threshold_accessor() -> None:
    """TokenBudgetTracker.soft_threshold_for feeds ProviderState."""
    cfg = TokenBudgetConfig(cap_tokens=1000, soft_threshold=0.7)
    t = TokenBudgetTracker(configs={"p": cfg})
    assert t.soft_threshold_for("p") == 0.7
    assert t.soft_threshold_for("absent") is None


# --- Speed statistics (Plan 020 Wave 3) -------------------------------------


@pytest.mark.asyncio
async def test_speed_sample_recorded_for_streamed_response() -> None:
    """A successful streamed response records TTFB + duration into the
    sampler. Completion tokens are picked up only when a budget observer is
    active — here there is none, so tokens_per_sec is None."""
    sampler = SpeedSampler()

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(prompt_tokens=100, completion_tokens=200)

    ctx = _make_mocked_ctx("ollama-cloud", handler)
    app = ProxyApp(
        providers={"ollama-cloud": ctx},
        route_table=RouteTableManager(default_providers=("ollama-cloud",)),
        speed_sampler=sampler,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200

    summary = sampler.summary("ollama-cloud")
    assert summary is not None
    assert summary["samples"] == 1
    assert summary["ttfb_ms"]["avg"] >= 0.0
    # Duration is at least the TTFB (request-open → completion).
    assert summary["duration_ms"]["avg"] >= summary["ttfb_ms"]["avg"]
    # No budget observer → no token count → tokens_per_sec is None.
    assert summary["tokens_per_sec"] is None


@pytest.mark.asyncio
async def test_speed_tokens_recorded_with_budget_observer() -> None:
    """When a token budget is configured, the usage observer is active and
    completion tokens ride along into the speed sample."""
    sampler = SpeedSampler()
    tracker = _make_budget_tracker(cap=100000)

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(prompt_tokens=100, completion_tokens=200)

    ctx = _make_mocked_ctx("ollama-cloud", handler)
    app = ProxyApp(
        providers={"ollama-cloud": ctx},
        route_table=RouteTableManager(default_providers=("ollama-cloud",)),
        budget_tracker=tracker,
        speed_sampler=sampler,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200

    summary = sampler.summary("ollama-cloud")
    assert summary is not None
    assert summary["tokens_per_sec"] is not None
    assert summary["tokens_per_sec"] > 0.0


@pytest.mark.asyncio
async def test_speed_not_recorded_for_error_response() -> None:
    """Only successful (2xx), fully-served responses feed the sampler — an
    upstream 500 must not pollute speed stats."""
    sampler = SpeedSampler()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    ctx = _make_mocked_ctx("ollama-cloud", handler)
    app = ProxyApp(
        providers={"ollama-cloud": ctx},
        route_table=RouteTableManager(default_providers=("ollama-cloud",)),
        speed_sampler=sampler,
    )

    body = json.dumps({"model": "test", "messages": []}).encode()
    await _send_request(app, body)
    assert sampler.summary("ollama-cloud") is None


# --- Conversation pinning (Plan 019 §6) -------------------------------------


@pytest.mark.asyncio
async def test_conversation_pinning_keys_affinity_by_fingerprint() -> None:
    """With pin_conversations on, the affinity entry is keyed by the
    conversation fingerprint (hash of the first user message), NOT the
    API-key hash — so two conversations through one API key pin
    independently."""
    from switchboard.control import (
        RoutingConfig as RC,
    )
    from switchboard.control import (
        extract_conversation_fingerprint,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response()

    primary_ctx = _make_mocked_ctx("umans", handler)
    app = ProxyApp(
        providers={"umans": primary_ctx},
        route_table=RouteTableManager(default_providers=("umans",)),
        routing_config=RC(pin_conversations=True, dwell_interval=30.0),
        max_request_body_bytes=1_048_576,
    )

    body = json.dumps(
        {"model": "test", "messages": [{"role": "user", "content": "hello"}]}
    ).encode()
    status, _ = await _send_request(app, body)
    assert status == 200

    expected_key = extract_conversation_fingerprint(body)
    assert expected_key is not None
    # The affinity table is keyed by the fingerprint, not the route-key hash.
    assert expected_key in app._affinity
    assert app._affinity[expected_key].provider == "umans"

    await primary_ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_conversation_pinning_falls_back_to_route_key_when_no_fingerprint() -> None:
    """A body with no user message yields no fingerprint — the affinity key
    falls back to the API-key hash (the pre-pinning behaviour)."""
    from switchboard.control import RoutingConfig as RC
    from switchboard.control import hash_route_key

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response()

    ctx = _make_mocked_ctx("umans", handler)
    app = ProxyApp(
        providers={"umans": ctx},
        route_table=RouteTableManager(default_providers=("umans",)),
        routing_config=RC(pin_conversations=True, dwell_interval=30.0),
        max_request_body_bytes=1_048_576,
    )

    # No user message → no fingerprint.
    body = json.dumps({"model": "test", "messages": []}).encode()
    status, _ = await _send_request(app, body)
    assert status == 200

    route_key = hash_route_key("test-key")
    assert route_key in app._affinity
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_two_conversations_pin_independently() -> None:
    """Two different first-user-messages through one API key produce two
    distinct affinity entries — the whole point of conversation pinning."""
    from switchboard.control import RoutingConfig as RC

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response()

    ctx = _make_mocked_ctx("umans", handler)
    app = ProxyApp(
        providers={"umans": ctx},
        route_table=RouteTableManager(default_providers=("umans",)),
        routing_config=RC(pin_conversations=True, dwell_interval=30.0),
        max_request_body_bytes=1_048_576,
    )

    for msg in ("first question", "second question"):
        body = json.dumps(
            {"model": "test", "messages": [{"role": "user", "content": msg}]}
        ).encode()
        status, _ = await _send_request(app, body)
        assert status == 200

    assert len(app._affinity) == 2
    await ctx.http_client.aclose()
