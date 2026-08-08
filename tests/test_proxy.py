from __future__ import annotations

from typing import Any

import httpx
import pytest

from switchboard.control import RoutingConfig
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.model_map import ModelMapManager
from switchboard.providers import ProviderContext
from switchboard.proxy import (
    ProxyApp,
    RoutingMetrics,
    _classify_429,
    _extract_model,
    _extract_route_key,
    _parse_retry_after_seconds,
    _rewrite_model_field,
)
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource


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
        max_concurrency=1,
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
    model_map_mgr: ModelMapManager | None = None,
) -> ProxyApp:
    if providers is None:
        providers = {"test": _make_provider_context()}
    route_table = RouteTableManager(default_providers=default_providers)
    return ProxyApp(
        providers=providers,
        route_table=route_table,
        routing_config=RoutingConfig(),
        admin_token=admin_token,
        model_map_mgr=model_map_mgr,
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


# ── HMAC route-key rotation (Plan 008 §3) ──────────────────────────────────
def _make_app_with_secrets(
    secrets: tuple[str, ...],
    default_providers: tuple[str, ...] = ("default-prov",),
) -> ProxyApp:
    route_table = RouteTableManager(default_providers=default_providers)
    return ProxyApp(
        providers={"test": _make_provider_context()},
        route_table=route_table,
        routing_config=RoutingConfig(),
        route_key_secrets=secrets,
    )


def test_match_route_no_secrets_is_plain_sha256_lookup() -> None:
    """No secrets = the pre-HMAC path: an entry hashed with plain SHA-256
    matches, and an unknown key falls to the default (backward compat)."""
    from switchboard.control import hash_route_key

    app = _make_app_with_secrets(())
    app._route_table.add_entry(hash_route_key("sk-keyed"), ["a", "b"])
    providers, matched_hash = app._match_route("sk-keyed")
    assert providers == ("a", "b")
    assert matched_hash == hash_route_key("sk-keyed")
    providers, matched_hash = app._match_route("sk-unknown")
    assert providers == ("default-prov",)
    # No keyed match → default; the returned hash is the plain digest.
    assert matched_hash == hash_route_key("sk-unknown")


def test_match_route_hmac_entry_matches_under_current_secret() -> None:
    from switchboard.control import hash_route_key

    app = _make_app_with_secrets(("current-secret",))
    app._route_table.add_entry(
        hash_route_key("sk-keyed", "current-secret"), ["a", "b"]
    )
    # A plain-SHA-256 entry would NOT match (the secret changes the digest),
    # proving the secret actually participates in the lookup.
    providers, matched_hash = app._match_route("sk-keyed")
    assert providers == ("a", "b")
    assert matched_hash == hash_route_key("sk-keyed", "current-secret")


def test_match_route_rotation_dual_read_matches_previous_secret() -> None:
    """The bounded dual-read window: an entry hashed under the PREVIOUS
    secret still routes while both secrets are active, so rotation does not
    drop traffic for keys not yet re-added under the new secret."""
    from switchboard.control import hash_route_key

    app = _make_app_with_secrets(("new-secret", "old-secret"))
    legacy_hash = hash_route_key("sk-legacy", "old-secret")
    app._route_table.add_entry(legacy_hash, ["legacy-prov"])
    providers, matched_hash = app._match_route("sk-legacy")
    assert providers == ("legacy-prov",)
    # The matched digest is the one under which the entry was actually found
    # (the PREVIOUS secret's), NOT the current secret's — route_decision
    # re-resolves by this digest, so returning the current-secret hash here
    # would miss and silently fall to the default (review finding 1).
    assert matched_hash == legacy_hash
    assert matched_hash != hash_route_key("sk-legacy", "new-secret")


def test_match_route_current_secret_takes_priority_over_previous() -> None:
    """When a key exists under both secrets (mid-rotation, re-added), the
    current-secret entry wins — so the operator's re-add is authoritative."""
    from switchboard.control import hash_route_key

    app = _make_app_with_secrets(("new-secret", "old-secret"))
    app._route_table.add_entry(
        hash_route_key("sk-shared", "old-secret"), ["old-prov"]
    )
    new_hash = hash_route_key("sk-shared", "new-secret")
    app._route_table.add_entry(new_hash, ["new-prov"])
    providers, matched_hash = app._match_route("sk-shared")
    assert providers == ("new-prov",)
    assert matched_hash == new_hash


def test_match_route_falls_to_default_when_no_keyed_match_under_any_secret() -> None:
    from switchboard.control import hash_route_key

    app = _make_app_with_secrets(("new-secret", "old-secret"))
    app._route_table.add_entry(
        hash_route_key("sk-someone-else", "new-secret"), ["a"]
    )
    # A key that matches no entry under either secret hits the default route.
    providers, matched_hash = app._match_route("sk-unmatched")
    assert providers == ("default-prov",)
    assert matched_hash == hash_route_key("sk-unmatched", "new-secret")


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


def test_parse_retry_after_none_returns_none() -> None:
    assert _parse_retry_after_seconds(None) is None


def test_parse_retry_after_integer_seconds() -> None:
    assert _parse_retry_after_seconds("30") == 30.0


def test_parse_retry_after_zero_returns_zero() -> None:
    assert _parse_retry_after_seconds("0") == 0.0


def test_parse_retry_after_garbage_returns_none() -> None:
    assert _parse_retry_after_seconds("not-a-date-or-number") is None


def test_parse_retry_after_http_date_returns_positive() -> None:
    import datetime

    future = datetime.datetime.now(
        datetime.UTC
    ) + datetime.timedelta(seconds=60)
    http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    result = _parse_retry_after_seconds(http_date)
    assert result is not None
    assert 50 < result < 70


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
    result = await app._admit(plan)
    assert result is not None and result[0] == "fallback"

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

    result = await app._admit(plan)
    assert result is not None and result[0] == "p2"

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
    result = await app._admit(plan)
    assert result is None

    await ctx1.gate.release()
    await ctx2.gate.release()
    await ctx1.http_client.aclose()
    await ctx2.http_client.aclose()


@pytest.mark.asyncio
async def test_acquire_with_disconnect_aborts_on_client_disconnect() -> None:
    """F-11 regression: a client disconnect during a queue-wait cancels the
    acquire and returns False (no admission), and does not leak a permit to a
    caller that never got one."""
    ctx = _make_provider_context("p1", capacity=1)
    await ctx.gate.acquire(timeout=0.0)  # saturate — acquire will block

    app = _make_app(providers={"p1": ctx}, default_providers=("p1",))

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    acquired = await app._acquire_with_disconnect(ctx, timeout=2.0, receive=receive)
    assert acquired is False
    # No permit leaked: the caller never received one, the gate is unchanged.
    assert ctx.gate.available == 0

    await ctx.gate.release()
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_admit_body_not_buffered_does_not_watch_disconnect() -> None:
    """F-11 safety property: ``_acquire_with_disconnect``'s watcher calls
    ``receive()``, which would steal body events from ``_forward``'s
    ``body_stream``. ``_admit`` therefore invokes it ONLY when the body is
    already buffered. With an unbuffered body, plain ``gate.acquire`` is used
    and ``receive`` is never touched — proven here by a receive that would
    surface any call."""
    from switchboard.control import AdmissionPlan
    ctx = _make_provider_context("p1", capacity=1)
    await ctx.gate.acquire(timeout=0.0)  # saturate the gate

    app = _make_app(providers={"p1": ctx}, default_providers=("p1",))
    app._queue_timeout = 0.1
    plan = AdmissionPlan(
        immediate_candidates=(),
        queue_candidate="p1",
        terminal_fallback="p1",
        reason="queue_only",
    )

    receive_calls = 0

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.disconnect"}

    result = await app._admit(plan, receive=receive, body_buffered=False)
    assert result is None          # timed out — no admission
    assert receive_calls == 0      # watcher never ran → body not stolen

    await ctx.gate.release()
    await ctx.http_client.aclose()


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
    result = await app._admit(plan)
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
    result = await app._admit(plan)
    assert result is not None and result[0] == "p1"

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


# --- Plan 010 Feature B: model extraction + rewrite ---


def test_extract_model_from_json() -> None:
    body = b'{"model": "umans-kimi-k2.7", "messages": []}'
    assert _extract_model(body) == "umans-kimi-k2.7"


def test_extract_model_missing_returns_none() -> None:
    body = b'{"messages": []}'
    assert _extract_model(body) is None


def test_extract_model_non_json_returns_none() -> None:
    assert _extract_model(b"not json") is None
    assert _extract_model(b"") is None


def test_extract_model_non_string_returns_none() -> None:
    body = b'{"model": 123}'
    assert _extract_model(body) is None


def test_rewrite_model_field_changes_model() -> None:
    body = b'{"model": "umans-kimi-k2.7", "messages": [{"role": "user", "content": "hi"}]}'
    rewritten = _rewrite_model_field(body, "kimi-k2.7-code")
    import json
    data = json.loads(rewritten)
    assert data["model"] == "kimi-k2.7-code"
    assert data["messages"][0]["content"] == "hi"


def test_rewrite_model_preserves_other_fields() -> None:
    body = b'{"model": "old", "temperature": 0.7, "stream": true}'
    rewritten = _rewrite_model_field(body, "new")
    import json
    data = json.loads(rewritten)
    assert data["model"] == "new"
    assert data["temperature"] == 0.7
    assert data["stream"] is True


def test_model_map_no_config_is_noop() -> None:
    app = _make_app()
    assert app._model_map_mgr is not None
    assert app._model_map_mgr.get_model_map().routes == {}


@pytest.mark.asyncio
async def test_buffer_request_body_collects_chunks() -> None:
    app = _make_app()

    class MultiChunkReceive:
        def __init__(self) -> None:
            self._chunks = [b'{"model": "', b'test"}']
            self._idx = 0

        async def __call__(self) -> dict[str, Any]:
            if self._idx < len(self._chunks):
                chunk = self._chunks[self._idx]
                self._idx += 1
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": self._idx < len(self._chunks),
                }
            return {"type": "http.request", "body": b"", "more_body": False}

    body, overflow = await app._buffer_request_body(MultiChunkReceive())
    assert overflow is False
    assert body == b'{"model": "test"}'


@pytest.mark.asyncio
async def test_buffer_request_body_detects_overflow() -> None:
    app = _make_app()
    app._max_request_body_bytes = 10

    receive = _MockReceive(body=b'{"model": "very-long-model-name"}')
    body, overflow = await app._buffer_request_body(receive)
    assert overflow is True
    assert body is None


@pytest.mark.asyncio
async def test_buffer_request_body_detects_disconnect() -> None:
    app = _make_app()

    class DisconnectReceive:
        async def __call__(self) -> dict[str, Any]:
            return {"type": "http.disconnect"}

    body, overflow = await app._buffer_request_body(DisconnectReceive())
    assert overflow is False
    assert body is None
