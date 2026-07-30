"""Tests for /metrics (send_prometheus) and /admin/threshold-events.

Regression coverage: there were previously no tests exercising the
Prometheus surface with the optional trackers attached, which allowed an
AttributeError on ``penalty=None`` to slip through.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any

import httpx
from sluice.control import BreakerConfig, ControllerConfig
from sluice.gate import PermitGate
from sluice.providers import NullTruthSource
from sluice.reconcile import ReconciliationLoop

from switchboard.admin import handle_threshold_events, send_prometheus
from switchboard.estimator import ThresholdEstimator
from switchboard.providers import ProviderContext
from switchboard.proxy import RoutingMetrics
from switchboard.usage_history import PenaltyTokenSummary, UsageHistoryTracker


def _make_ctx(name: str = "umans") -> ProviderContext:
    """Build a minimal ProviderContext with a real reconcile loop."""
    gate = PermitGate(initial_capacity=3)
    truth = NullTruthSource(provider="umans")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        controller_config=ControllerConfig(target=3),
        breaker_config=BreakerConfig(),
    )
    reconcile._first_poll_ok = True
    return ProviderContext(
        name=name,
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=httpx.AsyncClient(),
    )


class _CaptureSend:
    """Collect ASGI messages."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, msg: dict[str, Any]) -> None:
        self.messages.append(msg)

    @property
    def status(self) -> int:
        return self.messages[0]["status"]

    @property
    def body(self) -> str:
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        ).decode()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestPrometheusUsageHistory:
    def test_metrics_with_tracker_no_penalty(self) -> None:
        """Regression: /metrics must not crash when a tracked provider has
        usage-history data but no active penalty (penalty dict absent)."""
        tracker = UsageHistoryTracker()
        tracker.register("umans", base_url="https://api.example.com", api_key="k")
        snap = tracker.snapshot("umans")
        assert snap is not None
        snap.tokens_24h = 5_000_000
        snap.tokens_24h_in = 2_000_000
        snap.tokens_24h_out = 3_000_000

        ctx = _make_ctx()
        send = _CaptureSend()
        _run(
            send_prometheus(
                send, {"umans": ctx}, RoutingMetrics(),
                usage_history_tracker=tracker,
            )
        )
        assert send.status == 200
        assert 'switchboard_tokens_24h{provider="umans"} 5000000' in send.body
        assert "switchboard_penalty_since_tokens" not in send.body
        asyncio.run(ctx.http_client.aclose())
        asyncio.run(tracker.close())

    def test_metrics_with_tracker_and_penalty(self) -> None:
        """Penalty gauges render when a penalty summary is present."""
        tracker = UsageHistoryTracker()
        tracker.register("umans", base_url="https://api.example.com", api_key="k")
        snap = tracker.snapshot("umans")
        assert snap is not None
        snap.tokens_24h = 5_000_000
        snap.penalty = PenaltyTokenSummary(
            penalty_started_at=time.time() - 3600,
            before_total=2_000_000,
            since_total=500_000,
        )

        ctx = _make_ctx()
        send = _CaptureSend()
        _run(
            send_prometheus(
                send, {"umans": ctx}, RoutingMetrics(),
                usage_history_tracker=tracker,
            )
        )
        assert send.status == 200
        assert 'switchboard_penalty_before_tokens{provider="umans"} 2000000' in send.body
        assert 'switchboard_penalty_since_tokens{provider="umans"} 500000' in send.body
        asyncio.run(ctx.http_client.aclose())
        asyncio.run(tracker.close())

    def test_metrics_without_trackers(self) -> None:
        """Baseline: /metrics renders with no optional trackers."""
        ctx = _make_ctx()
        send = _CaptureSend()
        _run(send_prometheus(send, {"umans": ctx}, RoutingMetrics()))
        assert send.status == 200
        assert "switchboard_routing_decisions 0" in send.body
        assert "switchboard_affinity_pins_total 0" in send.body
        assert "switchboard_affinity_failbacks_total 0" in send.body
        asyncio.run(ctx.http_client.aclose())

    def test_metrics_with_estimator(self) -> None:
        """Estimator gauges render when the estimator is attached."""
        db = sqlite3.connect(":memory:")
        est = ThresholdEstimator(provider_name="umans", db=db)
        ctx = _make_ctx()
        send = _CaptureSend()
        _run(
            send_prometheus(
                send, {"umans": ctx}, RoutingMetrics(), estimator=est,
            )
        )
        assert send.status == 200
        assert "switchboard_threshold_edges_total" in send.body
        assert "switchboard_threshold_trigger_events_total" in send.body
        asyncio.run(ctx.http_client.aclose())
        db.close()


class TestThresholdEventsEndpoint:
    def _scope(self, query: bytes = b"", authed: bool = True) -> dict[str, Any]:
        headers: list[tuple[bytes, bytes]] = []
        if authed:
            headers.append((b"authorization", b"Bearer secret-token"))
        return {
            "type": "http",
            "method": "GET",
            "query_string": query,
            "headers": headers,
        }

    def test_requires_auth(self) -> None:
        send = _CaptureSend()
        _run(handle_threshold_events(send, self._scope(authed=False), "secret-token", None))
        assert send.status == 401

    def test_404_without_estimator(self) -> None:
        send = _CaptureSend()
        _run(handle_threshold_events(send, self._scope(), "secret-token", None))
        assert send.status == 404

    def test_200_with_events(self) -> None:
        db = sqlite3.connect(":memory:")
        est = ThresholdEstimator(provider_name="umans", db=db)
        est._save_event(
            _make_event(window_id="w1", requests=120, tokens=1900, sessions=4)
        )
        send = _CaptureSend()
        _run(handle_threshold_events(send, self._scope(), "secret-token", est))
        assert send.status == 200
        body = json.loads(send.body)
        assert body["provider"] == "umans"
        assert body["summary"]["trigger_count"] == 1
        assert len(body["events"]) == 1
        assert body["events"][0]["window_id"] == "w1"
        db.close()

    def test_limit_param_clamped(self) -> None:
        db = sqlite3.connect(":memory:")
        est = ThresholdEstimator(provider_name="umans", db=db)
        for i in range(5):
            est._save_event(_make_event(window_id=f"w{i}"))
        send = _CaptureSend()
        _run(
            handle_threshold_events(
                send, self._scope(query=b"limit=2"), "secret-token", est,
            )
        )
        assert send.status == 200
        body = json.loads(send.body)
        assert len(body["events"]) == 2
        # Garbage limit falls back to default, doesn't crash.
        send2 = _CaptureSend()
        _run(
            handle_threshold_events(
                send2, self._scope(query=b"limit=abc"), "secret-token", est,
            )
        )
        assert send2.status == 200
        db.close()


def _make_event(
    *,
    window_id: str = "w1",
    requests: int = 100,
    tokens: int = 1000,
    sessions: int = 2,
    triggered: bool = True,
) -> Any:
    from switchboard.threshold import ThresholdEvent

    return ThresholdEvent(
        window_id=window_id,
        requests=requests,
        tokens=tokens,
        concurrent_sessions=sessions,
        triggered=triggered,
    )
