"""The decision log carries the signals that produced the decision (W1.5).

``recent_decisions`` used to answer "where did it go" and nothing else, so
"why" meant reconstructing seven provider signals from a state that had already
moved on. Plan 026 attaches the assessments' signal names to the entry.

The key constraint is that it is *additive*: an unremarkable decision must
serialise exactly as it did before, because /status.json consumers read these
entries.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from switchboard.control import RoutingConfig
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.peak import parse_peak_windows
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp, RoutingMetrics
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource


def test_record_decision_without_signals_is_byte_for_byte_the_old_entry() -> None:
    metrics = RoutingMetrics()
    metrics.record_decision("keyhash", "alpha", "alpha")
    assert list(metrics.recent_decisions) == [
        {"route_key_hash": "keyhash...", "selected": "alpha", "primary": "alpha"}
    ]


def test_record_decision_omits_the_key_when_no_candidate_had_a_signal() -> None:
    metrics = RoutingMetrics()
    metrics.record_decision("keyhash", "alpha", "alpha", {})
    metrics.record_decision("keyhash", "alpha", "alpha", {"alpha": ()})
    assert all("signals" not in e for e in metrics.recent_decisions)


def test_record_decision_carries_the_signals_that_fired() -> None:
    metrics = RoutingMetrics()
    metrics.record_decision(
        "keyhash", "beta", "alpha",
        {"alpha": ("in_peak",), "beta": ("busy", "low_headroom")},
    )
    entry = metrics.recent_decisions[-1]
    assert entry["signals"] == {
        "alpha": ["in_peak"],
        "beta": ["busy", "low_headroom"],
    }
    # Still a failover, still the same three keys alongside.
    assert metrics.failovers == 1
    assert entry["selected"] == "beta"
    assert entry["primary"] == "alpha"


def test_recent_decisions_stay_json_serialisable() -> None:
    """/status.json renders these verbatim."""
    metrics = RoutingMetrics()
    metrics.record_decision("k", "beta", "alpha", {"alpha": ("in_peak",)})
    assert json.loads(json.dumps(list(metrics.recent_decisions)))[0]["signals"] == {
        "alpha": ["in_peak"]
    }


# ── through the proxy ─────────────────────────────────────────────────────


class _Stream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self) -> Any:
        yield self._payload

    async def aclose(self) -> None:
        pass


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        stream=_Stream(b'{"choices":[{"message":{"content":"OK"}}]}'),
    )


def _ctx(name: str) -> ProviderContext:
    gate = PermitGate(initial_capacity=4)
    truth = NullTruthSource(provider="generic")
    return ProviderContext(
        name=name,
        upstream_url="https://upstream.invalid/v1",
        gate=gate,
        reconcile=ReconciliationLoop(
            truth_source=truth,
            gate=gate,
            max_concurrency=4,
            poll_interval=60.0,
            breaker_config=BreakerConfig(),
            provider_type="generic",
        ),
        truth_source=truth,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_ok), timeout=httpx.Timeout(5.0)
        ),
        api_key="k",
    )


async def _post(app: ProxyApp) -> int:
    body = json.dumps(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    ).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer client-key"),
        ],
        "query_string": b"",
        "client": ("10.0.0.1", 1234),
    }
    sent: list[dict[str, Any]] = []
    delivered = {"body": False}

    async def receive() -> dict[str, Any]:
        if not delivered["body"]:
            delivered["body"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return int(start["status"])


@pytest.mark.asyncio
async def test_a_peak_demotion_is_visible_in_the_decision_log() -> None:
    """The operational question this answers: "why did traffic move off alpha
    at 14:00?" — now readable from /status.json instead of inferred."""
    providers = {"alpha": _ctx("alpha"), "beta": _ctx("beta")}
    providers["alpha"].peak_windows = parse_peak_windows(
        ["mon-sun 00:00-23:59 Z"]
    )
    route_table = RouteTableManager()
    route_table.set_default_providers(("alpha", "beta"))
    for ctx in providers.values():
        await ctx.reconcile.tick()
    app = ProxyApp(
        providers=providers,
        route_table=route_table,
        routing_config=RoutingConfig(),
    )
    assert await _post(app) == 200
    entry = app.metrics.recent_decisions[-1]
    assert entry["selected"] == "beta"
    assert entry["signals"] == {"alpha": ["in_peak"]}


@pytest.mark.asyncio
async def test_an_unremarkable_decision_logs_no_signals_key() -> None:
    providers = {"alpha": _ctx("alpha"), "beta": _ctx("beta")}
    route_table = RouteTableManager()
    route_table.set_default_providers(("alpha", "beta"))
    for ctx in providers.values():
        await ctx.reconcile.tick()
    app = ProxyApp(
        providers=providers,
        route_table=route_table,
        routing_config=RoutingConfig(),
    )
    assert await _post(app) == 200
    assert "signals" not in app.metrics.recent_decisions[-1]
