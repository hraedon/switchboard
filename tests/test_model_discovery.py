"""Tests for the shared model-listing probe and discovery runner (Plan 027).

The load-bearing property is PARITY: the probe must hit the byte-identical
URL and present the byte-identical credential the live forwarding path uses
for a client ``GET /v1/models`` — so "the probe answers 200" means "traffic
can flow", not merely "the upstream is alive".
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from switchboard.capability_store import CapabilityStore
from switchboard.control import credential_value
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.model_capabilities import TriState
from switchboard.model_discovery import (
    MODELS_RESPONSE_MAX_BYTES,
    ListingResult,
    ModelDiscovery,
    config_fingerprint,
    models_probe_headers,
    models_probe_url,
    probe_model_listing,
    probe_status_only,
)
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.reconcile import ReconciliationLoop
from switchboard.truth import NullTruthSource


def _ctx(
    name: str = "p1",
    upstream_url: str = "https://up.example.com",
    api_key: str = "",
    auth_header: str = "authorization",
    auth_prefix: str = "Bearer ",
) -> ProviderContext:
    gate = PermitGate(initial_capacity=1)
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
        api_key=api_key,
        auth_header=auth_header,
        auth_prefix=auth_prefix,
    )


def _scope(path: str, query: bytes = b"") -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query,
        "headers": [],
    }


def _client_credential_headers() -> list[tuple[str, str]]:
    """A client's incoming credential headers (mixed styles)."""
    return [
        ("authorization", "Bearer client-key-123"),
        ("x-api-key", "client-key-123"),
        ("content-type", "application/json"),
    ]


# ── URL parity with the live forwarding path ─────────────────────────────────


@pytest.mark.parametrize(
    "base_url",
    [
        "https://up.example.com",
        "https://up.example.com/v1",
        "https://up.example.com/v1/",
        "https://up.example.com/zen/go/v1",
        "https://up.example.com:8443",
    ],
)
def test_probe_url_matches_egress_url(base_url: str) -> None:
    ctx = _ctx(upstream_url=base_url)
    app = object.__new__(ProxyApp)
    egress = ProxyApp._build_url(app, ctx, _scope("/v1/models"))
    assert models_probe_url(ctx) == egress


def test_probe_url_matches_egress_path_with_query() -> None:
    # ASGI query strings carry no "?". The probe composes the canonical
    # listing path (it runs on a timer, never per client request, so it has
    # no client query to carry); parity holds on the path.
    ctx = _ctx(upstream_url="https://up.example.com/v1")
    app = object.__new__(ProxyApp)
    egress = ProxyApp._build_url(app, ctx, _scope("/v1/models", b"x=1"))
    assert egress == "https://up.example.com/v1/models?x=1"
    assert models_probe_url(ctx) == egress.partition("?")[0]


# ── Credential parity with the live forwarding path ──────────────────────────


@pytest.mark.parametrize(
    "api_key, auth_header, auth_prefix, expected_header, expected_value",
    [
        ("k1", "authorization", "Bearer ", "authorization", "Bearer k1"),
        # Stored prefix without trailing space is repaired (single choke
        # point: control.credential_value, shared by egress and probe).
        ("k2", "authorization", "Bearer", "authorization", "Bearer k2"),
        # Non-Bearer scheme: no space inserted.
        ("k3", "x-api-key", "Token", "x-api-key", "Token k3"),
        # Raw key, no scheme.
        ("k4", "x-api-key", "", "x-api-key", "k4"),
    ],
)
def test_probe_headers_match_egress_credential(
    api_key: str,
    auth_header: str,
    auth_prefix: str,
    expected_header: str,
    expected_value: str,
) -> None:
    ctx = _ctx(
        api_key=api_key, auth_header=auth_header, auth_prefix=auth_prefix
    )
    assert models_probe_headers(ctx) == {
        expected_header: expected_value
    }
    assert expected_value == credential_value(auth_prefix, api_key)
    egress = ProxyApp._apply_provider_credential(ctx, _client_credential_headers())
    # The egress presents exactly the provider's credential: every client
    # credential style (mixed headers) is stripped, so no client key
    # material is forwarded to another vendor.
    cred = [
        (k, v)
        for k, v in egress
        if k.lower() in {"authorization", "x-api-key", "api-key", "x-goog-api-key"}
    ]
    assert cred == [(expected_header, expected_value)]
    assert all(v != "client-key-123" for _, v in egress)


def test_probe_headers_absent_without_key() -> None:
    ctx = _ctx(api_key="")
    assert models_probe_headers(ctx) == {}


def test_fingerprint_changes_with_config() -> None:
    a = _ctx(upstream_url="https://a.example.com", api_key="k")
    b = _ctx(upstream_url="https://a.example.com", api_key="other")
    assert config_fingerprint(a) == config_fingerprint(a)
    assert config_fingerprint(a) != config_fingerprint(b)


# ── Bounded reads and error reporting ────────────────────────────────────────


async def test_probe_parses_listing_and_fills_observations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "m1",
                        "max_model_len": 1000,
                        "supported_parameters": ["tools"],
                    },
                    "bare-string-model",
                ]
            },
        )

    ctx = _ctx()
    result = await probe_model_listing(
        ctx,
        observed_at=1000.0,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    assert result.ok is True
    assert result.status == 200
    assert result.fingerprint == config_fingerprint(ctx)
    aliases = [m.alias for m in result.models]
    assert aliases == ["m1", "bare-string-model"]
    m1 = result.models[0]
    assert m1.provider == "p1"
    assert m1.context_limit == 1000
    assert m1.tool_calling is TriState.TRUE
    assert m1.observed_at == 1000.0
    assert m1.config_fingerprint == config_fingerprint(ctx)


async def test_probe_over_size_body_is_abandoned_not_buffered() -> None:
    huge = json.dumps({"data": [{"id": "m" * (MODELS_RESPONSE_MAX_BYTES)}]})
    assert len(huge) > MODELS_RESPONSE_MAX_BYTES

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=huge.encode())

    result = await probe_model_listing(
        _ctx(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    assert result.ok is False
    assert result.status == 200
    assert result.detail == "response too large"
    assert result.models == ()
    assert result.fingerprint == ""


async def test_probe_error_detail_bounded_and_extracted() -> None:
    payload = json.dumps(
        {"message": "quota exceeded", "padding": "x" * (16 * 1024)}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=payload.encode())

    result = await probe_model_listing(
        _ctx(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    assert result.ok is False
    assert result.status == 429
    assert "quota exceeded" in result.detail


async def test_probe_timeout_reports_status_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    status, latency, detail = await probe_status_only(
        _ctx(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    assert status is None
    assert detail == "timeout"
    assert latency >= 0.0


async def test_probe_non_json_response_reports_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    result = await probe_model_listing(
        _ctx(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    assert result.ok is False
    assert result.detail == "non-JSON response"


async def test_probe_status_only_returns_status_and_latency() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/v1/models")
        return httpx.Response(200, json={"data": []})

    status, latency, detail = await probe_status_only(
        _ctx(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    assert status == 200
    assert detail == ""
    assert latency >= 0.0


# ── The discovery runner ─────────────────────────────────────────────────────


def _listing_payload() -> dict:
    return {
        "data": [
            {"id": "m1", "max_model_len": 100},
            {"id": "m2"},
        ]
    }


async def test_refresh_persists_successful_listings() -> None:
    from switchboard.model_capabilities import parse_model_listing

    store = CapabilityStore()

    def make_probe(payload: dict):
        async def probe(ctx, **_kw) -> ListingResult:
            models = tuple(
                parse_model_listing(
                    payload,
                    provider=ctx.name,
                    observed_at=123.0,
                    config_fingerprint=config_fingerprint(ctx),
                )
            )
            return ListingResult(
                provider=ctx.name,
                ok=True,
                status=200,
                models=models,
                fingerprint=config_fingerprint(ctx),
            )

        return probe

    import switchboard.model_discovery as md

    real_probe = md.probe_model_listing
    try:
        discovery = ModelDiscovery(store=store, interval_s=0.0)
        md.probe_model_listing = make_probe(_listing_payload())  # type: ignore[assignment]
        results = await discovery.refresh(
            {
                "a": _ctx("a", upstream_url="https://a.example.com"),
                "b": _ctx("b", upstream_url="https://b.example.com"),
            },
            force=True,
        )
    finally:
        md.probe_model_listing = real_probe  # type: ignore[assignment]

    assert len(results) == 2
    assert all(r.ok for r in results)
    obs_a = store.for_alias("a", "m1")
    assert obs_a is not None
    assert obs_a.provider == "a"
    assert obs_a.context_limit == 100
    assert obs_a.observed_at == 123.0
    assert store.for_alias("b", "m2") is not None


def test_interval_zero_disables_periodic_run() -> None:
    store = CapabilityStore()
    discovery = ModelDiscovery(store=store, interval_s=0.0)
    assert discovery.interval == 0.0
    assert discovery.is_due(1_000_000.0) is False


def test_interval_throttles_successive_runs() -> None:
    store = CapabilityStore()
    discovery = ModelDiscovery(store=store, interval_s=60.0)
    t0 = 1_000_000.0
    assert discovery.is_due(t0) is True
    discovery._last_run = t0
    assert discovery.is_due(t0 + 30) is False
    assert discovery.is_due(t0 + 60) is True


async def test_refresh_not_due_returns_empty_without_probing() -> None:
    calls: list[str] = []

    async def fake_probe(ctx, **_kw) -> ListingResult:
        calls.append(ctx.name)
        return ListingResult(provider=ctx.name, ok=False)

    import switchboard.model_discovery as md

    real_probe = md.probe_model_listing
    md.probe_model_listing = fake_probe  # type: ignore[assignment]
    try:
        store = CapabilityStore()
        discovery = ModelDiscovery(store=store, interval_s=60.0)
        discovery._last_run = time.time()
        result = await discovery.refresh({"a": _ctx("a")})
        assert result == ()
        assert calls == []
        # force bypasses the interval.
        result = await discovery.refresh({"a": _ctx("a")}, force=True)
        assert len(result) == 1
        assert calls == ["a"]
    finally:
        md.probe_model_listing = real_probe  # type: ignore[assignment]


async def test_in_flight_guard_blocks_stacked_waves() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_probe(ctx, **_kw) -> ListingResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ListingResult(provider=ctx.name, ok=False)

    store = CapabilityStore()
    discovery = ModelDiscovery(store=store, interval_s=0.0)

    import switchboard.model_discovery as md

    real_probe = md.probe_model_listing
    md.probe_model_listing = slow_probe  # type: ignore[assignment]
    try:
        # force: the interval is 0 (periodic disabled), and even forced
        # refreshes respect the in-flight guard — that is the point.
        first = asyncio.create_task(
            discovery.refresh({"a": _ctx("a")}, force=True)
        )
        await started.wait()
        second = await discovery.refresh({"a": _ctx("a")}, force=True)
        assert second == ()
        assert calls == 1
        release.set()
        results = await first
        assert len(results) == 1
        # After completion the runner is available again.
        assert not discovery._in_flight
    finally:
        md.probe_model_listing = real_probe  # type: ignore[assignment]


async def test_failed_probe_keeps_previous_observations() -> None:
    store = CapabilityStore()
    from switchboard.model_capabilities import ModelObservation

    store.upsert(
        "a",
        [ModelObservation(provider="a", alias="m1", observed_at=1.0, context_limit=5)],
    )

    async def failing_probe(ctx, **_kw) -> ListingResult:
        return ListingResult(provider=ctx.name, ok=False, detail="timeout")

    import switchboard.model_discovery as md

    real_probe = md.probe_model_listing
    md.probe_model_listing = failing_probe  # type: ignore[assignment]
    try:
        discovery = ModelDiscovery(store=store, interval_s=0.0)
        results = await discovery.refresh({"a": _ctx("a")}, force=True)
        assert results[0].ok is False
        obs = store.for_alias("a", "m1")
        assert obs is not None
        assert obs.context_limit == 5
    finally:
        md.probe_model_listing = real_probe  # type: ignore[assignment]


async def test_successful_refresh_replaces_listing() -> None:
    store = CapabilityStore()
    import switchboard.model_discovery as md

    async def probe_two(ctx, **_kw) -> ListingResult:
        from switchboard.model_capabilities import parse_model_listing

        models = tuple(
            parse_model_listing(
                {"data": [{"id": "m1"}, {"id": "m2"}]},
                provider=ctx.name,
                observed_at=100.0,
                config_fingerprint=config_fingerprint(ctx),
            )
        )
        return ListingResult(
            provider=ctx.name,
            ok=True,
            status=200,
            models=models,
            fingerprint=config_fingerprint(ctx),
        )

    async def probe_one(ctx, **_kw) -> ListingResult:
        from switchboard.model_capabilities import parse_model_listing

        models = tuple(
            parse_model_listing(
                {"data": [{"id": "m1"}]},
                provider=ctx.name,
                observed_at=200.0,
                config_fingerprint=config_fingerprint(ctx),
            )
        )
        return ListingResult(
            provider=ctx.name,
            ok=True,
            status=200,
            models=models,
            fingerprint=config_fingerprint(ctx),
        )

    real_probe = md.probe_model_listing
    try:
        discovery = ModelDiscovery(store=store, interval_s=0.0)
        md.probe_model_listing = probe_two  # type: ignore[assignment]
        await discovery.refresh({"a": _ctx("a")}, force=True)
        assert set(store.for_provider("a")) == {"m1", "m2"}
        md.probe_model_listing = probe_one  # type: ignore[assignment]
        await discovery.refresh({"a": _ctx("a")}, force=True)
        # m2 dropped from the listing must stop certifying.
        assert set(store.for_provider("a")) == {"m1"}
    finally:
        md.probe_model_listing = real_probe  # type: ignore[assignment]
