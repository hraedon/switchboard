from __future__ import annotations

from typing import Any

import httpx
import pytest
from sluice.control import BreakerConfig, ControllerConfig
from sluice.gate import PermitGate
from sluice.providers import NullTruthSource
from sluice.reconcile import ReconciliationLoop

from switchboard.control import RoutingConfig
from switchboard.providers import ProviderContext
from switchboard.proxy import (
    ProxyApp,
    RoutingMetrics,
    _classify_429,
    _extract_route_key,
)
from switchboard.route_table import RouteTableManager


def _make_provider_context(
    name: str = "test",
    upstream_url: str = "https://upstream.example.com",
    capacity: int = 0,
) -> ProviderContext:
    gate = PermitGate(initial_capacity=capacity)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        controller_config=ControllerConfig(target=1),
        breaker_config=BreakerConfig(),
    )
    return ProviderContext(
        name=name,
        upstream_url=upstream_url,
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=httpx.AsyncClient(),
    )


def _make_scope(
    method: str = "GET",
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }


class _MockReceive:
    def __init__(self, body: bytes = b"", http_disconnect: bool = False) -> None:
        self._body = body
        self._sent = False
        self._http_disconnect = http_disconnect

    async def __call__(self) -> dict[str, Any]:
        if not self._sent:
            self._sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        if self._http_disconnect:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": b"", "more_body": False}


def _make_send() -> tuple[list[dict], Any]:
    messages: list[dict] = []

    async def send(msg: dict) -> None:
        messages.append(msg)

    return messages, send


def _parse_response(messages: list[dict]) -> tuple[int, bytes, list[tuple[bytes, bytes]]]:
    status = 0
    body = b""
    headers: list[tuple[bytes, bytes]] = []
    for msg in messages:
        if msg["type"] == "http.response.start":
            status = msg["status"]
            headers = msg.get("headers", [])
        elif msg["type"] == "http.response.body":
            body += msg.get("body", b"")
    return status, body, headers


def _make_app(
    providers: dict[str, ProviderContext] | None = None,
    admin_token: str | None = None,
    default_providers: tuple[str, ...] = ("test",),
) -> ProxyApp:
    if providers is None:
        providers = {"test": _make_provider_context()}
    route_table = RouteTableManager(default_providers=default_providers)
    return ProxyApp(
        providers=providers,
        route_table=route_table,
        routing_config=RoutingConfig(),
        admin_token=admin_token,
    )


@pytest.mark.asyncio
async def test_healthz_returns_200() -> None:
    app = _make_app()
    scope = _make_scope(path="/healthz")
    receive = _MockReceive()
    messages, send = _make_send()
    await app(scope, receive, send)
    status, body, _ = _parse_response(messages)
    assert status == 200
    assert b"ok" in body


@pytest.mark.asyncio
async def test_readyz_returns_503_when_providers_not_ready() -> None:
    app = _make_app()
    scope = _make_scope(path="/readyz")
    receive = _MockReceive()
    messages, send = _make_send()
    await app(scope, receive, send)
    status, body, _ = _parse_response(messages)
    assert status == 503
    assert b"not ready" in body


def test_extract_route_key_from_authorization_bearer() -> None:
    scope = _make_scope(headers=[(b"authorization", b"Bearer sk-test-key")])
    assert _extract_route_key(scope) == "sk-test-key"


def test_extract_route_key_from_x_api_key() -> None:
    scope = _make_scope(headers=[(b"x-api-key", b"sk-test-key")])
    assert _extract_route_key(scope) == "sk-test-key"


def test_extract_route_key_empty_when_no_auth_header() -> None:
    scope = _make_scope()
    assert _extract_route_key(scope) == ""


def test_extract_route_key_authorization_without_bearer_returns_empty() -> None:
    scope = _make_scope(headers=[(b"authorization", b"Basic dXNlcjpwYXNz")])
    assert _extract_route_key(scope) == ""


def test_extract_route_key_bearer_is_case_insensitive() -> None:
    scope = _make_scope(headers=[(b"authorization", b"bearer sk-test-key")])
    assert _extract_route_key(scope) == "sk-test-key"


def test_routing_metrics_record_decision_tracks_failovers() -> None:
    metrics = RoutingMetrics()
    metrics.record_decision("key1", "umans", "umans")
    assert metrics.failovers == 0
    assert metrics.routing_decisions == 1
    metrics.record_decision("key1", "ollama", "umans")
    assert metrics.failovers == 1
    assert metrics.routing_decisions == 2
    assert len(metrics.recent_decisions) == 2


def test_routing_metrics_record_forwarded_tracks_per_provider() -> None:
    metrics = RoutingMetrics()
    metrics.record_forwarded("umans")
    metrics.record_forwarded("umans")
    metrics.record_forwarded("ollama")
    assert metrics.forwarded_per_provider["umans"] == 2
    assert metrics.forwarded_per_provider["ollama"] == 1


def test_routing_metrics_bounded_recent_decisions() -> None:
    """WI-006.4: recent_decisions is bounded."""
    from switchboard.proxy import _RECENT_DECISIONS_MAX
    metrics = RoutingMetrics()
    for i in range(_RECENT_DECISIONS_MAX + 50):
        metrics.record_decision(f"key{i}", "umans", "umans")
    assert len(metrics.recent_decisions) == _RECENT_DECISIONS_MAX
    assert metrics.evicted_decisions == 50


def test_classify_429_concurrency_when_no_retry_after() -> None:
    assert _classify_429(None, {}) == "concurrency"


def test_classify_429_rate_limit_with_positive_retry_after() -> None:
    assert _classify_429("60", {}) == "rate_limit"


def test_classify_429_concurrency_with_zero_retry_after() -> None:
    assert _classify_429("0", {}) == "concurrency"


def test_classify_429_gateway_with_cdn_header() -> None:
    assert _classify_429("10", {"cf-ray": "abc123"}) == "gateway"


def test_classify_429_gateway_with_cdn_server() -> None:
    assert _classify_429("10", {"server": "cloudflare"}) == "gateway"


def test_classify_429_rate_limit_with_http_date() -> None:
    assert _classify_429("Wed, 21 Oct 2026 07:28:00 GMT", {}) == "rate_limit"


def test_classify_429_concurrency_with_invalid_retry_after() -> None:
    assert _classify_429("not-a-number", {}) == "concurrency"


def test_proxy_imports_control() -> None:
    import switchboard.control as control_mod
    import switchboard.proxy as proxy_mod
    assert hasattr(proxy_mod, "route_decision")
    assert hasattr(control_mod, "route_decision")


@pytest.mark.asyncio
async def test_proxy_healthz_via_app_call() -> None:
    app = _make_app()
    scope = _make_scope(path="/healthz")
    receive = _MockReceive()
    messages, send = _make_send()
    await app(scope, receive, send)
    status, _, _ = _parse_response(messages)
    assert status == 200


@pytest.mark.asyncio
async def test_proxy_readyz_503_when_not_ready() -> None:
    app = _make_app()
    scope = _make_scope(path="/readyz")
    receive = _MockReceive()
    messages, send = _make_send()
    await app(scope, receive, send)
    status, _, _ = _parse_response(messages)
    assert status == 503


@pytest.mark.asyncio
async def test_proxy_request_no_providers_returns_503() -> None:
    app = _make_app(providers={"test": _make_provider_context()}, default_providers=())
    scope = _make_scope(method="POST", path="/v1/chat/completions")
    receive = _MockReceive(body=b'{"model": "test"}')
    messages, send = _make_send()
    await app(scope, receive, send)
    status, body, _ = _parse_response(messages)
    assert status == 503
    assert b"no providers" in body


@pytest.mark.asyncio
async def test_proxy_draining_returns_503() -> None:
    app = _make_app()
    app._draining = True
    scope = _make_scope(method="POST", path="/v1/chat/completions")
    receive = _MockReceive(body=b'{"model": "test"}')
    messages, send = _make_send()
    await app(scope, receive, send)
    status, body, _ = _parse_response(messages)
    assert status == 503
    assert b"draining" in body


@pytest.mark.asyncio
async def test_proxy_metrics_property_returns_routing_metrics() -> None:
    app = _make_app()
    assert isinstance(app.metrics, RoutingMetrics)


@pytest.mark.asyncio
async def test_admit_immediate_failover_to_idle_fallback() -> None:
    """WI-006.3: full primary + idle fallback → immediate failover, no queue."""
    from switchboard.control import AdmissionPlan
    primary_ctx = _make_provider_context("primary", capacity=1)
    fallback_ctx = _make_provider_context("fallback", capacity=1)

    # Acquire the primary's only permit, making it BUSY at snapshot time.
    await primary_ctx.gate.acquire(timeout=0.0)

    app = _make_app(
        providers={"primary": primary_ctx, "fallback": fallback_ctx},
        default_providers=("primary", "fallback"),
    )
    plan = AdmissionPlan(
        immediate_candidates=("fallback",),
        queue_candidate="primary",
        terminal_fallback="primary",
        reason="failover",
    )
    result = await app._admit(plan, ("primary", "fallback"))
    assert result == "fallback"

    await primary_ctx.gate.release()
    await primary_ctx.http_client.aclose()
    await fallback_ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_admit_race_fills_primary_tries_next() -> None:
    """WI-006.3: race filling the preferred gate still tries the next."""
    from switchboard.control import AdmissionPlan
    ctx1 = _make_provider_context("p1", capacity=1)
    ctx2 = _make_provider_context("p2", capacity=1)

    app = _make_app(
        providers={"p1": ctx1, "p2": ctx2},
        default_providers=("p1", "p2"),
    )
    plan = AdmissionPlan(
        immediate_candidates=("p1", "p2"),
        queue_candidate=None,
        terminal_fallback="p1",
        reason="primary_available",
    )
    # Acquire p1's permit before _admit tries it.
    held = await ctx1.gate.acquire(timeout=0.0)
    assert held

    result = await app._admit(plan, ("p1", "p2"))
    assert result == "p2"

    await ctx1.gate.release()
    await ctx1.http_client.aclose()
    await ctx2.http_client.aclose()


@pytest.mark.asyncio
async def test_admit_all_fail_returns_none() -> None:
    """When all candidates fail, _admit returns None → 503."""
    from switchboard.control import AdmissionPlan
    ctx1 = _make_provider_context("p1", capacity=1)
    ctx2 = _make_provider_context("p2", capacity=1)

    app = _make_app(
        providers={"p1": ctx1, "p2": ctx2},
        default_providers=("p1", "p2"),
    )
    # Hold all permits.
    await ctx1.gate.acquire(timeout=0.0)
    await ctx2.gate.acquire(timeout=0.0)

    plan = AdmissionPlan(
        immediate_candidates=(),
        queue_candidate="p1",
        terminal_fallback="p1",
        reason="queue_only",
    )
    # Use a very short queue timeout so the test doesn't hang.
    app._queue_timeout = 0.1
    result = await app._admit(plan, ("p1", "p2"))
    assert result is None

    await ctx1.gate.release()
    await ctx2.gate.release()
    await ctx1.http_client.aclose()
    await ctx2.http_client.aclose()


@pytest.mark.asyncio
async def test_admit_queue_wait_uses_remaining_budget() -> None:
    """WI-006.3: queue wait uses remaining budget, not full timeout."""
    from switchboard.control import AdmissionPlan
    ctx = _make_provider_context("p1", capacity=1)

    app = _make_app(
        providers={"p1": ctx},
        default_providers=("p1",),
    )
    # Hold the only permit.
    await ctx.gate.acquire(timeout=0.0)

    plan = AdmissionPlan(
        immediate_candidates=(),
        queue_candidate="p1",
        terminal_fallback="p1",
        reason="queue_only",
    )
    app._queue_timeout = 0.3
    import time as _time
    start = _time.monotonic()
    result = await app._admit(plan, ("p1",))
    elapsed = _time.monotonic() - start
    assert result is None
    # Should have waited roughly the queue_timeout, not significantly more.
    assert elapsed < 0.5

    await ctx.gate.release()
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_admit_permit_released_during_queue_wait() -> None:
    """Both providers full; one permit releases during queue window."""
    import asyncio

    from switchboard.control import AdmissionPlan

    ctx1 = _make_provider_context("p1", capacity=1)
    ctx2 = _make_provider_context("p2", capacity=1)

    app = _make_app(
        providers={"p1": ctx1, "p2": ctx2},
        default_providers=("p1", "p2"),
    )
    # Hold all permits.
    await ctx1.gate.acquire(timeout=0.0)
    await ctx2.gate.acquire(timeout=0.0)

    # Schedule a release of ctx1's permit after 0.1s.
    async def release_later():
        await asyncio.sleep(0.1)
        await ctx1.gate.release()

    release_task = asyncio.create_task(release_later())

    plan = AdmissionPlan(
        immediate_candidates=(),
        queue_candidate="p1",
        terminal_fallback="p1",
        reason="queue_only",
    )
    app._queue_timeout = 1.0
    # The queue wait on p1 should succeed after the permit is released.
    result = await app._admit(plan, ("p1", "p2"))
    assert result == "p1"

    await release_task
    await ctx1.gate.release()
    await ctx1.http_client.aclose()
    await ctx2.http_client.aclose()


@pytest.mark.asyncio
async def test_routing_metrics_bounded_with_many_unique_keys() -> None:
    """WI-006.4: 10k unique route credentials leave bounded observation state."""
    from switchboard.proxy import _RECENT_DECISIONS_MAX
    metrics = RoutingMetrics()
    for i in range(10000):
        metrics.record_decision(f"key_{i}", "umans", "umans")
    assert len(metrics.recent_decisions) == _RECENT_DECISIONS_MAX
    assert metrics.evicted_decisions == 10000 - _RECENT_DECISIONS_MAX


def test_proxy_filter_request_headers_strips_hop_by_hop() -> None:
    app = _make_app()
    headers = [
        (b"connection", b"keep-alive"),
        (b"keep-alive", b"timeout=30"),
        (b"transfer-encoding", b"chunked"),
        (b"content-type", b"application/json"),
    ]
    filtered = app._filter_request_headers(headers)
    names = [k.lower() for k, _ in filtered]
    assert "connection" not in names
    assert "keep-alive" not in names
    assert "transfer-encoding" not in names
    assert "content-type" in names


def test_proxy_filter_request_headers_strips_switchboard_control_headers() -> None:
    app = _make_app()
    headers = [
        (b"x-switchboard-route-key", b"secret"),
        (b"x-switchboard-qos", b"high"),
        (b"x-switchboard-custom", b"value"),
        (b"content-type", b"application/json"),
    ]
    filtered = app._filter_request_headers(headers)
    names = [k.lower() for k, _ in filtered]
    assert "x-switchboard-route-key" not in names
    assert "x-switchboard-qos" not in names
    assert "x-switchboard-custom" not in names
    assert "content-type" in names


def test_proxy_filter_request_headers_strips_sluice_headers() -> None:
    app = _make_app()
    headers = [
        (b"x-sluice-debug", b"true"),
        (b"x-sluice-custom", b"value"),
        (b"content-type", b"application/json"),
    ]
    filtered = app._filter_request_headers(headers)
    names = [k.lower() for k, _ in filtered]
    assert "x-sluice-debug" not in names
    assert "x-sluice-custom" not in names
    assert "content-type" in names


def test_proxy_filter_request_headers_strips_host() -> None:
    app = _make_app()
    headers = [
        (b"host", b"switchboard.example.com"),
        (b"content-type", b"application/json"),
    ]
    filtered = app._filter_request_headers(headers)
    names = [k.lower() for k, _ in filtered]
    assert "host" not in names
    assert "content-type" in names


def test_proxy_filter_request_headers_strips_admin_auth() -> None:
    token = "super-secret-admin-token"
    app = _make_app(admin_token=token)
    headers = [
        (b"authorization", f"Bearer {token}".encode()),
        (b"content-type", b"application/json"),
    ]
    filtered = app._filter_request_headers(headers)
    names = [k.lower() for k, _ in filtered]
    assert "authorization" not in names
    assert "content-type" in names


def test_proxy_filter_request_headers_keeps_non_admin_authorization() -> None:
    token = "super-secret-admin-token"
    app = _make_app(admin_token=token)
    headers = [
        (b"authorization", b"Bearer sk-user-api-key"),
        (b"content-type", b"application/json"),
    ]
    filtered = app._filter_request_headers(headers)
    names = [k.lower() for k, _ in filtered]
    assert "authorization" in names
    assert "content-type" in names


def test_proxy_filter_request_headers_strips_connection_listed_headers() -> None:
    app = _make_app()
    headers = [
        (b"connection", b"x-custom-hop, keep-alive"),
        (b"x-custom-hop", b"value"),
        (b"content-type", b"application/json"),
    ]
    filtered = app._filter_request_headers(headers)
    names = [k.lower() for k, _ in filtered]
    assert "connection" not in names
    assert "x-custom-hop" not in names
    assert "content-type" in names
