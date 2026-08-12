"""GET /admin/route-plan — the explain surface, through the real ASGI app.

Plan 026 W1.4. The endpoint's value is that it cannot disagree with the
decision, so these tests drive the actual app and assert against what the
routing core would do — including the properties that make it safe to call on a
live proxy: no affinity pin created, no metric moved, no clock advanced.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from switchboard.control import FailureAttribution, RoutingConfig
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.model_map import ModelMapManager
from switchboard.peak import parse_peak_windows
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.quarantine import QuarantineTracker
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource

_TOKEN = "admin-secret"


def _ctx(name: str, capacity: int = 4) -> ProviderContext:
    gate = PermitGate(initial_capacity=capacity)
    truth = NullTruthSource(provider="generic")
    return ProviderContext(
        name=name,
        upstream_url="https://upstream.invalid/v1",
        gate=gate,
        reconcile=ReconciliationLoop(
            truth_source=truth,
            gate=gate,
            max_concurrency=capacity,
            poll_interval=60.0,
            breaker_config=BreakerConfig(),
            provider_type="generic",
        ),
        truth_source=truth,
        http_client=httpx.AsyncClient(),
    )


async def _build(
    *,
    routing_config: RoutingConfig | None = None,
    models: dict[str, dict[str, str]] | None = None,
    preferences: dict[str, list[str]] | None = None,
    quarantine: QuarantineTracker | None = None,
    admin_token: str | None = _TOKEN,
) -> ProxyApp:
    providers = {"alpha": _ctx("alpha"), "beta": _ctx("beta")}
    route_table = RouteTableManager()
    route_table.set_default_providers(("alpha", "beta"))
    model_map = ModelMapManager(valid_providers=frozenset(providers))
    model_map.load_from_config(
        {
            "model": models
            or {
                "shared": {"alpha": "shared", "beta": "shared"},
                "alpha-only": {"alpha": "alpha-only"},
            }
        }
    )
    for model, preference in (preferences or {}).items():
        model_map.set_model(model, dict(model_map.get_model_map().routes[model]),
                            preference)
    for ctx in providers.values():
        # Without a tick every provider is UNKNOWN and holds no tier.
        await ctx.reconcile.tick()
    return ProxyApp(
        providers=providers,
        route_table=route_table,
        routing_config=routing_config or RoutingConfig(),
        model_map_mgr=model_map,
        quarantine=quarantine,
        admin_token=admin_token,
    )


async def _get(
    app: ProxyApp,
    query: str = "",
    *,
    token: str | None = _TOKEN,
    method: str = "GET",
    key_header: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers: list[tuple[bytes, bytes]] = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if key_header is not None:
        headers.append((b"x-route-plan-key", key_header.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": "/admin/route-plan",
        "raw_path": b"/admin/route-plan",
        "query_string": query.encode(),
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    messages: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        messages.append(msg)

    await app(scope, receive, send)
    status = 0
    body = b""
    for msg in messages:
        if msg["type"] == "http.response.start":
            status = msg["status"]
        elif msg["type"] == "http.response.body":
            body += msg.get("body", b"")
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = {"_raw": body.decode("utf-8", "replace")}
    return status, parsed


@pytest.mark.asyncio
async def test_route_plan_explains_the_default_route() -> None:
    app = await _build()
    status, body = await _get(app)
    assert status == 200
    assert body["reason"] == "primary_available"
    assert body["immediate"] == ["alpha", "beta"]
    assert body["terminal_fallback"] == "alpha"
    assert body["queue_candidate"] == "alpha"
    assert body["keyed_route"] is False
    assert body["strategy"] == "ordered"
    assert body["candidates"] == ["alpha", "beta"]
    assert [a["provider"] for a in body["assessments"]] == ["alpha", "beta"]
    assert body["assessments"][0] == {
        "provider": "alpha",
        "tier": "immediate",
        "signals": [],
        "score": None,
        "rank": 0,
        # Plan 026 W3: null = this provider is not named in a preference (here,
        # the model has none). The field is always present so a consumer never
        # distinguishes "unpreferred" from "old build".
        "preference_rank": None,
        "availability": "available",
        "freshness": "fresh",
    }


@pytest.mark.asyncio
async def test_route_plan_requires_admin_auth() -> None:
    app = await _build()
    status, body = await _get(app, token=None)
    assert status == 401
    assert body["error"] == "unauthorized"
    status, _ = await _get(app, token="wrong")
    assert status == 401


@pytest.mark.asyncio
async def test_route_plan_is_readable_when_no_admin_token_is_configured() -> None:
    app = await _build(admin_token=None)
    status, body = await _get(app, token=None)
    assert status == 200
    assert body["reason"] == "primary_available"


@pytest.mark.asyncio
async def test_route_plan_applies_model_map_filtering() -> None:
    app = await _build()
    status, body = await _get(app, "model=alpha-only")
    assert status == 200
    assert body["model"] == "alpha-only"
    assert body["immediate"] == ["alpha"]
    # beta is filtered, so it holds no tier — and the surface says why rather
    # than leaving the operator to notice an absence.
    assert [a["provider"] for a in body["assessments"]] == ["alpha"]
    assert body["excluded"] == [{"provider": "beta", "why": "filtered"}]


@pytest.mark.asyncio
async def test_route_plan_reports_an_unservable_model() -> None:
    app = await _build(models={"other": {"alpha": "other"}})
    status, body = await _get(app, "model=other&key=")
    assert status == 200
    assert body["reason"] == "primary_available"
    # A model nobody serves is unmapped, not unservable: no filtering applies.
    status, body = await _get(app, "model=unmapped")
    assert body["immediate"] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_route_plan_reports_quarantined_pairs() -> None:
    tracker = QuarantineTracker(threshold=1, clock=lambda: 1000.0)
    app = await _build(quarantine=tracker)
    tracker.record(
        "beta", "shared", FailureAttribution.PROVIDER, status=500, detail="boom"
    )
    status, body = await _get(app, "model=shared")
    assert status == 200
    assert body["quarantined"] == ["beta"]
    assert body["immediate"] == ["alpha"]
    assert [a["provider"] for a in body["assessments"]] == ["alpha"]


@pytest.mark.asyncio
async def test_route_plan_follows_a_keyed_route() -> None:
    app = await _build()
    from switchboard.control import hash_route_key

    app._route_table.add_entry(hash_route_key("sk-live"), ["beta", "alpha"])
    status, body = await _get(app, "key=sk-live")
    assert status == 200
    assert body["keyed_route"] is True
    assert body["candidates"] == ["beta", "alpha"]
    assert body["immediate"] == ["beta", "alpha"]
    assert body["terminal_fallback"] == "beta"
    # The raw key is never echoed back, in any field.
    assert "sk-live" not in json.dumps(body)


@pytest.mark.asyncio
async def test_route_plan_follows_a_keyed_route_from_the_header() -> None:
    """The key belongs in a header, because query strings are access-logged.

    uvicorn writes the request line — path *and* query string — to its access
    log, so ``?key=sk-live`` puts a live API key on disk through the one path
    switchboard does not control. The header carries the same value past a
    logger that does not record headers.
    """
    from switchboard.control import hash_route_key

    app = await _build()
    app._route_table.add_entry(hash_route_key("sk-live"), ["beta", "alpha"])
    status, body = await _get(app, key_header="sk-live")
    assert status == 200
    assert body["keyed_route"] is True
    assert body["candidates"] == ["beta", "alpha"]
    assert body["terminal_fallback"] == "beta"
    assert "sk-live" not in json.dumps(body)


@pytest.mark.asyncio
async def test_route_plan_prefers_the_header_over_the_query_param() -> None:
    """A caller who set the header chose the non-logging path deliberately;
    silently preferring the logged value would defeat the point."""
    from switchboard.control import hash_route_key

    app = await _build()
    app._route_table.add_entry(hash_route_key("sk-header"), ["beta", "alpha"])
    app._route_table.add_entry(hash_route_key("sk-query"), ["alpha"])
    status, body = await _get(app, "key=sk-query", key_header="sk-header")
    assert status == 200
    assert body["candidates"] == ["beta", "alpha"]


@pytest.mark.asyncio
async def test_route_plan_empty_header_falls_back_to_the_query_param() -> None:
    """An empty header is not an answer — curl's ``-H 'x-route-plan-key;'``
    sends one, and treating it as "explain the default route" would silently
    answer a different question than the query param asked."""
    from switchboard.control import hash_route_key

    app = await _build()
    app._route_table.add_entry(hash_route_key("sk-query"), ["beta"])
    status, body = await _get(app, "key=sk-query", key_header="")
    assert status == 200
    assert body["keyed_route"] is True
    assert body["candidates"] == ["beta"]


@pytest.mark.asyncio
async def test_route_plan_key_header_is_never_forwarded_upstream() -> None:
    """It carries a raw API key, so it is a switchboard control header: a
    request that happened to include it must not hand one vendor another
    vendor's credential (AGENTS.md cache-transparency rule)."""
    from switchboard.proxy import _CONTROL_HEADERS

    assert "x-route-plan-key" in _CONTROL_HEADERS

    app = await _build()
    filtered = app._filter_request_headers(
        [
            (b"content-type", b"application/json"),
            (b"x-route-plan-key", b"sk-live"),
        ]
    )
    assert [n for n, _ in filtered] == ["content-type"]


@pytest.mark.asyncio
async def test_route_plan_shows_signals_for_a_demoted_candidate() -> None:
    app = await _build(routing_config=RoutingConfig(usage_24h_threshold=0.9))
    # An all-day peak window: beta is expensive right now, whatever the hour.
    app._providers["beta"].peak_windows = parse_peak_windows(
        ["mon-sun 00:00-23:59 Z"]
    )
    status, body = await _get(app)
    assert status == 200
    by_name = {a["provider"]: a for a in body["assessments"]}
    assert by_name["beta"]["tier"] == "queue"
    assert by_name["beta"]["signals"] == ["in_peak"]
    assert by_name["alpha"]["tier"] == "immediate"
    assert body["immediate"] == ["alpha"]


@pytest.mark.asyncio
async def test_route_plan_never_mutates_affinity_metrics_or_clocks() -> None:
    """The whole reason it is safe to call on a live proxy."""
    app = await _build()
    before_metrics = dict(app.metrics.forwarded_per_provider)
    status, _ = await _get(app, "model=shared&key=sk-live")
    assert status == 200
    assert app._affinity == {}
    assert app.metrics.routing_decisions == 0
    assert app.metrics.failovers == 0
    assert app.metrics.forwarded_per_provider == before_metrics
    assert list(app.metrics.recent_decisions) == []
    assert app._provider_healthy_since == {}


@pytest.mark.asyncio
async def test_route_plan_rejects_other_methods() -> None:
    app = await _build()
    status, _ = await _get(app, method="POST")
    assert status == 405


@pytest.mark.asyncio
async def test_route_plan_is_not_cached() -> None:
    app = await _build()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin/route-plan",
        "raw_path": b"/admin/route-plan",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {_TOKEN}".encode())],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    messages: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        messages.append(msg)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert (b"cache-control", b"no-store") in start["headers"]


@pytest.mark.asyncio
async def test_route_plan_explains_an_estate_with_no_providers() -> None:
    route_table = RouteTableManager()
    app = ProxyApp(
        providers={},
        route_table=route_table,
        routing_config=RoutingConfig(),
        admin_token=_TOKEN,
    )
    status, body = await _get(app)
    assert status == 503
    assert body["reason"] == "no_providers"


@pytest.mark.asyncio
async def test_route_plan_shows_the_per_model_preference() -> None:
    """Plan 026 W3: the operator's per-model provider order is visible in the
    explanation, so "why is beta in front" needs no cross-read of the model
    map. Reported as a top-level ``preference`` plus a ``preference_rank`` per
    assessment (null = not named)."""
    app = await _build(preferences={"shared": ["beta"]})
    status, body = await _get(app, "model=shared")
    assert status == 200
    assert body["preference"] == ["beta"]
    # And the decision itself honours it: beta fronts alpha, the table primary.
    assert body["immediate"] == ["beta", "alpha"]
    assert [
        (a["provider"], a["preference_rank"]) for a in body["assessments"]
    ] == [("beta", 0), ("alpha", None)]
    # The primary keeps its other roles — preference reorders, it never demotes.
    assert body["terminal_fallback"] == "alpha"
    assert body["queue_candidate"] == "alpha"


@pytest.mark.asyncio
async def test_route_plan_preference_is_empty_for_an_unpreferred_model() -> None:
    app = await _build(preferences={"shared": ["beta"]})
    status, body = await _get(app, "model=alpha-only")
    assert status == 200
    assert body["preference"] == []
    assert all(a["preference_rank"] is None for a in body["assessments"])
    # Unfiltered (no model) too: a preference is per model.
    status, body = await _get(app)
    assert body["preference"] == []
