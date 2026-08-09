"""Admin route handlers — health, readiness, status, metrics, CRUD (route
table + model map), and dashboard.

Stateless functions that receive the proxy's state as arguments. Shared
utilities (``send_json``, ``send_text``, ``check_admin_auth``) are borrowed
from :mod:`switchboard.utils` to avoid duplication. Switchboard-specific handlers
build multi-provider status payloads and manage route table CRUD.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx

from switchboard import __version__
from switchboard.config_reset import (
    SECTIONS,
    ResetError,
    parse_sections,
    reset_sections,
)
from switchboard.config_store import ConfigStoreManager
from switchboard.control import MUTABLE_ROUTING_FIELDS as _MUTABLE_ROUTING_FIELDS
from switchboard.session import (
    SESSION_COOKIE,
    LoginThrottle,
    mint_session,
)
from switchboard.utils import (
    build_set_cookie,
    check_admin_auth,
    check_csrf,
    cors_extra_headers,
    read_body,
    send_json,
    send_text,
)

if TYPE_CHECKING:
    from switchboard.estimator import ThresholdEstimator
    from switchboard.model_map import ModelMapManager
    from switchboard.provider_manager import ProviderManager
    from switchboard.providers import ProviderContext
    from switchboard.proxy import RoutingMetrics
    from switchboard.route_table import RouteTableManager

log = logging.getLogger("switchboard.admin")

Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json; charset=utf-8",
}

_DASHBOARD_HTML = (_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
_LOGIN_HTML = (_STATIC_DIR / "login.html").read_text(encoding="utf-8")

_SESSION_COOKIE = SESSION_COOKIE
_SESSION_TTL = 2_592_000


async def handle_healthz(send: Send) -> None:
    """GET /healthz — always 200."""
    await send_json(send, 200, {"status": "ok"})


async def handle_readyz(
    send: Send,
    providers: dict[str, ProviderContext],
) -> None:
    """GET /readyz — 200 if all providers ready, 503 otherwise."""
    ready = (
        all(ctx.reconcile.ready for ctx in providers.values())
        if providers
        else False
    )
    if ready:
        await send_json(send, 200, {"status": "ready"})
    else:
        await send_json(send, 503, {"status": "not ready"})


def _direct_usage_health(ctx: ProviderContext) -> dict[str, Any]:
    """Parse/transport failure counts for a direct-usage provider, else {}.

    Kept out of the main status dict for providers that do not use direct
    usage, so the absence of the keys means "not scraping" rather than
    "scraping fine" (Plan 022 WI-3).
    """
    source = ctx.truth_source
    parse_failures = getattr(source, "parse_failures", None)
    if parse_failures is None:
        return {}
    return {
        "direct_usage": {
            "parse_failures": parse_failures,
            "transport_failures": getattr(source, "transport_failures", 0),
        }
    }


def _provider_status(
    ctx: ProviderContext,
    overload_tracker: Any | None = None,
    budget_tracker: Any | None = None,
    usage_history_tracker: Any | None = None,
    speed_sampler: Any | None = None,
) -> dict[str, Any]:
    """Build a status dict for one provider (Plan 012 §3.1 — full parity surface).

    Reads the full reconcile state that sluice already computes, plus
    switchboard-specific signals (overload breaker, token budget).  Display
    only — the data is already on the loop.
    """
    r = ctx.reconcile
    now = time.monotonic()
    reading = r.last_reading.reading if r.last_reading is not None else None

    status: dict[str, Any] = {
        # Gate / admission
        "gate_closed_reason": r.gate_closed_reason(),
        "effective_permits": r.effective_permits_count,
        "in_flight": ctx.gate.held,
        "queue_depth": ctx.gate.queue_depth,
        "available_permits": ctx.gate.available,
        "capacity": ctx.gate.capacity,
        "ready": r.ready,
        "upstream_url": ctx.upstream_url,
        # Breaker
        "breaker": r.breaker_state.value,
        "breaker_half_open_age_seconds": r.breaker_half_open_age_seconds,
        # Band / penalty
        "band": r.band.value,
        "penalty_started_at": r.penalty_started_at,
        # Usage reading
        "concurrent_sessions": r.observed_concurrent_sessions,
        "limit": reading.limit if reading else None,
        "hard_cap": reading.hard_cap if reading else None,
        "priority_low": reading.priority_low if reading else False,
        "priority_reason": reading.priority_reason if reading else None,
        "boxed_until": reading.boxed_until_epoch if reading else None,
        "resets_at": reading.resets_at_epoch if reading else None,
        "service_mode": reading.service_mode if reading else None,
        "service_mode_resets_at": (
            reading.service_mode_resets_at_epoch if reading else None
        ),
        "low_interactivity": r.is_low_interactivity(),
        "tokens_in": reading.tokens_in if reading else None,
        "tokens_out": reading.tokens_out if reading else None,
        "usage_age": round(r.last_age_seconds, 1),
        "stale": not r.last_fetch_ok,
        "phantom_estimate": r.phantom_estimate_value,
        # Weekly-window quota (Plan 020 D6) — the pace strategy's signal.
        "weekly_remaining_fraction": (
            reading.weekly_remaining_fraction if reading else None
        ),
        "weekly_reset_epoch": (
            reading.weekly_reset_epoch if reading else None
        ),
        # Direct usage solicitation health (Plan 022 WI-3). Absent for
        # providers not using it. `parse_failures` rising means the vendor
        # surface changed shape and this provider's weekly signal is gone —
        # routing degrades safely to table order, so nothing else surfaces it.
        **_direct_usage_health(ctx),
        # Request-window budget
        "requests_in_window": (
            reading.requests_in_window if reading else None
        ),
        "requests_limit": reading.requests_limit if reading else None,
        "requests_remaining": (
            reading.requests_remaining if reading else None
        ),
        "requests_hard_cap": reading.requests_hard_cap if reading else None,
        "requests_window_seconds": (
            reading.requests_window_seconds if reading else None
        ),
        "local_requests_in_window": r.local_requests_in_window,
        "request_window_delta": r.request_window_delta,
        "total_requests_forwarded": r.total_requests_forwarded,
        "throughput": r.last_throughput,
        "idle": r.is_idle,
        # Error counters
        "recent_429s": r.recent_429_count,
        "total_429s": r.total_429s,
        "gateway_429s": r.gateway_429s,
        "rate_limit_429s": r.rate_limit_429s,
        "total_503s": r.total_503s,
        "recent_503_count": r.recent_503_count,
        # Queue / timing
        "cooling_down": ctx.gate.cooling_down,
        "avg_wait_seconds": round(r.avg_wait_seconds, 3),
        "p95_wait_seconds": round(r.p95_wait_seconds, 3),
        "avg_hold_seconds": round(r.avg_hold_seconds, 3),
        "retry_after_hint": r.saturation_hint,
        "queue_timeouts": r.queue_timeouts,
        # Config
        "target": r.target,
        "min_floor": r.min_floor,
        "poll_interval": r.poll_interval,
        "poll_interval_idle": r.poll_interval_idle,
        "usage_fresh_ttl": r.usage_fresh_ttl,
        "phantom_window": r.phantom_window,
        "breaker_threshold": r.breaker_threshold,
        "breaker_window_seconds": r.breaker_window_seconds,
        "breaker_cooldown_seconds": r.breaker_cooldown_seconds,
        "controller": r.controller_name,
        "provider_name": r.provider_name,
        "overrides": r.overrides,
    }

    # Switchboard-specific: overload breaker state.
    if overload_tracker is not None:
        status["overload_consecutive"] = overload_tracker.consecutive(ctx.name)
        status["overload_cooling"] = overload_tracker.is_cooling(
            ctx.name, now=now
        )
        status["overload_cooldown_remaining"] = (
            overload_tracker.cooldown_remaining(ctx.name, now=now)
        )

    # Switchboard-specific: token budget utilization (Plan 012 Feature B).
    if budget_tracker is not None:
        util = budget_tracker.utilization(ctx.name, now=now)
        status["token_utilization"] = util
        status["token_budget"] = budget_tracker.budget_summary(ctx.name)

    # Switchboard-specific: usage-history token counts (24h rolling + penalty).
    if usage_history_tracker is not None:
        uh = usage_history_tracker.status_dict(ctx.name)
        if uh is not None:
            status["usage_history"] = uh

    # Switchboard-specific: per-provider speed statistics (Plan 020 Wave 3).
    if speed_sampler is not None:
        summary = speed_sampler.summary(ctx.name)
        if summary is not None:
            status["speed"] = summary

    # History trend summary (Plan 012 WI-2).
    hist = ctx.reconcile.history
    if hist is not None and hist.length > 0:
        recent = hist.entries()[-20:]
        status["history"] = {
            "count": hist.length,
            "recent": [
                {
                    "ts": e.timestamp,
                    "band": e.band,
                    "ep": e.effective_permits,
                    "stl": e.stale,
                    "tp": e.throughput,
                    "r429": e.recent_429s,
                }
                for e in recent
            ],
        }

    return status


def routing_config_payload(routing_config: Any) -> dict[str, Any]:
    """Serialise a ``RoutingConfig`` for every surface that reports one.

    ``/status.json``, ``GET /admin/config`` and the ``PUT /admin/config/routing``
    response each used to hand-list the fields they cared about. Three
    hand-lists is three chances to forget one, and ``quarantine_threshold``
    (Plan 023) was forgotten by two of them: it was settable and persisted but
    invisible everywhere an operator would look to confirm it.

    So the enumeration lives here once, derived from
    ``MUTABLE_ROUTING_FIELDS`` plus the display-only fields, and
    ``test_config_surfaces`` asserts every mutable field appears — a new knob
    that skips a surface fails the test instead of going quiet in production.
    """
    payload: dict[str, Any] = {}
    for name in _MUTABLE_ROUTING_FIELDS:
        value = getattr(routing_config, name)
        payload[name] = value.value if name == "strategy" else value
    # Retained for display; not settable at runtime (see MUTABLE_ROUTING_FIELDS).
    payload["failover_threshold_seconds"] = routing_config.failover_threshold_seconds
    payload["failover_margin"] = routing_config.failover_margin
    payload["pin_conversations"] = routing_config.pin_conversations
    payload["affinity_max_entries"] = routing_config.affinity_max_entries
    return payload


def _build_status_payload(
    providers: dict[str, ProviderContext],
    route_table: RouteTableManager,
    routing_metrics: RoutingMetrics,
    build_sha: str | None = None,
    estimator: ThresholdEstimator | None = None,
    overload_tracker: Any | None = None,
    budget_tracker: Any | None = None,
    usage_history_tracker: Any | None = None,
    model_map_mgr: ModelMapManager | None = None,
    speed_sampler: Any | None = None,
    routing_config: Any | None = None,
    quarantine: Any | None = None,
) -> dict[str, Any]:
    """Build the full status payload for /status.json."""
    provider_states: dict[str, Any] = {}
    for name, ctx in providers.items():
        provider_states[name] = _provider_status(
            ctx,
            overload_tracker=overload_tracker,
            budget_tracker=budget_tracker,
            usage_history_tracker=usage_history_tracker,
            speed_sampler=speed_sampler,
        )

    routes: dict[str, list[str]] = {}
    for entry in route_table.list_entries():
        routes[entry.key] = list(entry.providers)
    routes["default"] = list(route_table.default_providers)

    payload: dict[str, Any] = {
        "providers": provider_states,
        "route_table": routes,
        "routing_metrics": {
            "forwarded_per_provider": dict(routing_metrics.forwarded_per_provider),
            "failovers": routing_metrics.failovers,
            "routing_decisions": routing_metrics.routing_decisions,
            "recent_decisions": list(routing_metrics.recent_decisions),
            "evicted_decisions": routing_metrics.evicted_decisions,
            "affinity_pins_total": routing_metrics.affinity_pins_total,
            "affinity_failbacks_total": routing_metrics.affinity_failbacks_total,
            "affinity_evictions_total": routing_metrics.affinity_evictions_total,
            "usage_reroutes_total": routing_metrics.usage_reroutes_total,
            "usage_reroutes_from": dict(routing_metrics.usage_reroutes_from),
            "usage_giveups_total": routing_metrics.usage_giveups_total,
        },
        "version": __version__,
        "build": build_sha,
    }

    if quarantine is not None:
        payload["quarantine"] = {
            "threshold": quarantine.threshold,
            "entries": [e.to_dict() for e in quarantine.entries()],
            "counters": quarantine.counters(),
        }

    if routing_config is not None:
        payload["routing_config"] = routing_config_payload(routing_config)

    if estimator is not None:
        est = estimator.state().estimate
        payload["estimator"] = {
            "edges": est.edges,
            "requests": {
                "lower": est.requests.lower,
                "upper": est.requests.upper,
                "best_guess": est.requests.best_guess,
                "edges": est.requests.edges,
                "contradicted": est.requests.contradicted,
            },
            "tokens": {
                "lower": est.tokens.lower,
                "upper": est.tokens.upper,
                "best_guess": est.tokens.best_guess,
                "edges": est.tokens.edges,
                "contradicted": est.tokens.contradicted,
            },
            "last_edge_concurrent_sessions": est.last_edge_concurrent_sessions,
            "events": estimator.event_summary(),
        }

    if model_map_mgr is not None:
        payload["model_map"] = {
            model: dict(aliases)
            for model, aliases in model_map_mgr.list_models()
        }

    return payload


async def send_status_json(
    send: Send,
    providers: dict[str, ProviderContext],
    route_table: RouteTableManager,
    routing_metrics: RoutingMetrics,
    build_sha: str | None = None,
    cors_allow_origin: str | None = None,
    estimator: ThresholdEstimator | None = None,
    overload_tracker: Any | None = None,
    budget_tracker: Any | None = None,
    usage_history_tracker: Any | None = None,
    model_map_mgr: ModelMapManager | None = None,
    speed_sampler: Any | None = None,
    routing_config: Any | None = None,
    quarantine: Any | None = None,
) -> None:
    """GET /status.json — per-provider state + route table + routing metrics."""
    payload = _build_status_payload(
        providers, route_table, routing_metrics, build_sha,
        estimator=estimator,
        overload_tracker=overload_tracker,
        budget_tracker=budget_tracker,
        usage_history_tracker=usage_history_tracker,
        model_map_mgr=model_map_mgr,
        speed_sampler=speed_sampler,
        routing_config=routing_config,
        quarantine=quarantine,
    )
    await send_json(
        send, 200, payload,
        extra_headers=[
            *cors_extra_headers(cors_allow_origin, None),
            (b"cache-control", b"no-store"),
        ],
    )


async def send_prometheus(
    send: Send,
    providers: dict[str, ProviderContext],
    routing_metrics: RoutingMetrics,
    cors_allow_origin: str | None = None,
    overload_tracker: Any | None = None,
    budget_tracker: Any | None = None,
    usage_history_tracker: Any | None = None,
    estimator: ThresholdEstimator | None = None,
    speed_sampler: Any | None = None,
) -> None:
    """GET /metrics — Prometheus text exposition format (Plan 012 §3.1)."""
    now_mono = time.monotonic()
    lines: list[str] = []

    def gauge(
        name: str, help_text: str, labels: str,
        value: float | int | None,
    ) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        v = value if value is not None else float("nan")
        lines.append(f"{name}{{{labels}}} {v}")

    def emit_enum(
        metric: str, help_text: str,
        provider: str, current: str,
        *states: str,
    ) -> None:
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} gauge")
        for st in states:
            lines.append(
                f'{metric}{{provider="{provider}",state="{st}"}} '
                f"{1 if current == st else 0}"
            )

    lines.append(
        "# HELP switchboard_routing_decisions Total routing decisions made"
    )
    lines.append("# TYPE switchboard_routing_decisions counter")
    lines.append(f"switchboard_routing_decisions {routing_metrics.routing_decisions}")

    lines.append(
        "# HELP switchboard_failovers Total failovers (non-primary selected)"
    )
    lines.append("# TYPE switchboard_failovers counter")
    lines.append(f"switchboard_failovers {routing_metrics.failovers}")

    lines.append(
        "# HELP switchboard_evicted_decisions "
        "Total routing decisions evicted from the bounded recent_decisions ring"
    )
    lines.append("# TYPE switchboard_evicted_decisions counter")
    lines.append(
        f"switchboard_evicted_decisions {routing_metrics.evicted_decisions}"
    )

    lines.append(
        "# HELP switchboard_forwarded_per_provider "
        "Total requests forwarded per provider"
    )
    lines.append("# TYPE switchboard_forwarded_per_provider counter")
    for name, count in sorted(routing_metrics.forwarded_per_provider.items()):
        lines.append(
            f'switchboard_forwarded_per_provider{{provider="{name}"}} {count}'
        )

    lines.append(
        "# HELP switchboard_usage_reroutes_total "
        "Requests moved off a provider that returned a usage error"
    )
    lines.append("# TYPE switchboard_usage_reroutes_total counter")
    lines.append(
        f"switchboard_usage_reroutes_total {routing_metrics.usage_reroutes_total}"
    )
    for name, count in sorted(routing_metrics.usage_reroutes_from.items()):
        lines.append(
            f'switchboard_usage_reroutes_from_total{{provider="{name}"}} {count}'
        )

    lines.append(
        "# HELP switchboard_usage_giveups_total "
        "Requests that got a usage error with no eligible provider left to "
        "route to (all candidates exhausted)"
    )
    lines.append("# TYPE switchboard_usage_giveups_total counter")
    lines.append(
        "switchboard_usage_giveups_total "
        f"{routing_metrics.usage_giveups_total}"
    )

    lines.append(
        "# HELP switchboard_affinity_pins_total Total affinity pins created"
    )
    lines.append("# TYPE switchboard_affinity_pins_total counter")
    lines.append(
        f"switchboard_affinity_pins_total {routing_metrics.affinity_pins_total}"
    )

    lines.append(
        "# HELP switchboard_affinity_failbacks_total "
        "Total affinity pins released on failback to primary"
    )
    lines.append("# TYPE switchboard_affinity_failbacks_total counter")
    lines.append(
        "switchboard_affinity_failbacks_total "
        f"{routing_metrics.affinity_failbacks_total}"
    )

    lines.append(
        "# HELP switchboard_affinity_evictions_total "
        "Affinity entries evicted from the LRU table (pin loss)"
    )
    lines.append("# TYPE switchboard_affinity_evictions_total counter")
    lines.append(
        f"switchboard_affinity_evictions_total "
        f"{routing_metrics.affinity_evictions_total}"
    )

    for name, ctx in sorted(providers.items()):
        r = ctx.reconcile
        reading = r.last_reading.reading if r.last_reading is not None else None
        labels = f'provider="{name}"'

        # Short aliases to keep the gauge table readable (Plan 012 §3.1).
        gate = ctx.gate
        riw = reading.requests_in_window if reading else None
        rr = reading.requests_remaining if reading else None
        lriw = r.local_requests_in_window
        rwd = r.request_window_delta

        # (metric_name, help_text, value) — data-driven so the metric set is
        # auditable in one place.
        _gauges: list[tuple[str, str, Any]] = [
            ("switchboard_in_flight", "Currently held permits", gate.held),
            ("switchboard_effective_permits", "Effective permit count", r.effective_permits_count),
            ("switchboard_queue_depth", "Current queue depth", gate.queue_depth),
            ("switchboard_total_429s", "Concurrency 429s from upstream", r.total_429s),
            ("switchboard_total_forwarded", "Requests forwarded upstream",
             r.total_requests_forwarded),
            ("switchboard_rate_limit_429s", "Rate-limit 429s", r.rate_limit_429s),
            ("switchboard_gateway_429s", "Gateway/CDN 429s", r.gateway_429s),
            ("switchboard_total_503s", "Upstream 503s (overload)", r.total_503s),
            ("switchboard_recent_429s", "Recent 429s in breaker window",
             r.recent_429_count),
            ("switchboard_phantom_estimate", "Windowed phantom estimate",
             r.phantom_estimate_value),
            ("switchboard_usage_stale", "1 if last fetch failed",
             0 if r.last_fetch_ok else 1),
            ("switchboard_usage_age_seconds", "Seconds since last poll",
             round(r.last_age_seconds, 1)),
            ("switchboard_observed_sessions", "Reported sessions",
             r.observed_concurrent_sessions),
            ("switchboard_cooling_down", "Permits in release cooldown", gate.cooling_down),
            ("switchboard_queue_wait_avg_seconds", "Mean queue wait", round(r.avg_wait_seconds, 3)),
            ("switchboard_queue_wait_p95_seconds", "P95 queue wait", round(r.p95_wait_seconds, 3)),
            ("switchboard_hold_avg_seconds", "Mean hold duration", round(r.avg_hold_seconds, 3)),
            ("switchboard_retry_after_hint_seconds", "Saturation Retry-After", r.saturation_hint),
            ("switchboard_queue_timeouts_total", "Requests that gave up waiting", r.queue_timeouts),
            ("switchboard_throughput", "Requests in last tick", r.last_throughput),
            ("switchboard_idle", "1 when idle", 1 if r.is_idle else 0),
            ("switchboard_requests_in_window", "Provider requests used", riw),
            ("switchboard_requests_remaining", "Provider remaining requests", rr),
            ("switchboard_local_requests_in_window", "Local forwarded in window", lriw),
            ("switchboard_request_window_delta", "Provider minus local", rwd),
        ]
        for metric_name, help_text, value in _gauges:
            gauge(metric_name, help_text, labels, value)

        emit_enum("switchboard_band", "enforcement band", name,
                  r.band.value, "normal", "low", "reject",
                  "boxed", "low_interactivity")
        emit_enum("switchboard_breaker", "breaker state", name,
                  r.breaker_state.value, "closed", "open", "half_open")

        if overload_tracker is not None:
            gauge(
                "switchboard_overload_consecutive",
                "Consecutive overloaded responses",
                labels,
                overload_tracker.consecutive(name),
            )
            gauge(
                "switchboard_overload_cooling",
                "1 if overload breaker is cooling",
                labels,
                1 if overload_tracker.is_cooling(name, now=now_mono) else 0,
            )

        if budget_tracker is not None:
            util = budget_tracker.utilization(name, now=now_mono)
            gauge(
                "switchboard_token_utilization",
                "Token budget utilization (0..1+)",
                labels,
                util,
            )

        if usage_history_tracker is not None:
            uh = usage_history_tracker.status_dict(name)
            if uh is not None:
                gauge(
                    "switchboard_tokens_24h",
                    "Total tokens (in+out) over the last 24 hours",
                    labels,
                    uh.get("tokens_24h"),
                )
                gauge(
                    "switchboard_tokens_24h_in",
                    "Input tokens over the last 24 hours",
                    labels,
                    uh.get("tokens_24h_in"),
                )
                gauge(
                    "switchboard_tokens_24h_out",
                    "Output tokens over the last 24 hours",
                    labels,
                    uh.get("tokens_24h_out"),
                )
                penalty = uh.get("penalty")
                if penalty is not None:
                    gauge(
                        "switchboard_penalty_before_tokens",
                        "Tokens consumed in the 24h before the penalty event",
                        labels,
                        penalty.get("before_total"),
                    )
                    gauge(
                        "switchboard_penalty_since_tokens",
                        "Tokens consumed since the penalty event started",
                        labels,
                        penalty.get("since_total"),
                    )

        if speed_sampler is not None:
            spd = speed_sampler.summary(name)
            if spd is not None:
                ttfb = spd.get("ttfb_ms") or {}
                gauge(
                    "switchboard_speed_ttfb_ms_avg",
                    "Mean time-to-first-byte (ms)",
                    labels,
                    ttfb.get("avg"),
                )
                gauge(
                    "switchboard_speed_ttfb_ms_p95",
                    "p95 time-to-first-byte (ms)",
                    labels,
                    ttfb.get("p95"),
                )
                gauge(
                    "switchboard_speed_duration_ms_avg",
                    "Mean total request duration (ms)",
                    labels,
                    (spd.get("duration_ms") or {}).get("avg"),
                )
                gauge(
                    "switchboard_speed_tokens_per_sec",
                    "Mean completion tokens per second",
                    labels,
                    spd.get("tokens_per_sec"),
                )
                gauge(
                    "switchboard_speed_samples",
                    "Speed samples in the rolling window",
                    labels,
                    spd.get("samples"),
                )

    if estimator is not None:
        est = estimator.state().estimate
        provider = estimator.provider_name
        elabels = f'provider="{provider}"'
        if est.requests.lower is not None:
            gauge(
                "switchboard_threshold_requests_lower",
                "Max requests that did NOT trigger low-interactivity",
                elabels,
                est.requests.lower,
            )
        if est.requests.upper is not None:
            gauge(
                "switchboard_threshold_requests_upper",
                "Min requests that DID trigger low-interactivity",
                elabels,
                est.requests.upper,
            )
        if est.requests.best_guess is not None:
            gauge(
                "switchboard_threshold_requests_best_guess",
                "Midpoint estimate of the low-interactivity request threshold",
                elabels,
                est.requests.best_guess,
            )
        if est.tokens.lower is not None:
            gauge(
                "switchboard_threshold_tokens_lower",
                "Max tokens that did NOT trigger low-interactivity",
                elabels,
                est.tokens.lower,
            )
        if est.tokens.upper is not None:
            gauge(
                "switchboard_threshold_tokens_upper",
                "Min tokens that DID trigger low-interactivity",
                elabels,
                est.tokens.upper,
            )
        if est.tokens.best_guess is not None:
            gauge(
                "switchboard_threshold_tokens_best_guess",
                "Midpoint estimate of the low-interactivity token threshold",
                elabels,
                est.tokens.best_guess,
            )
        gauge(
            "switchboard_threshold_edges_total",
            "Total OFF->ON edges observed by the estimator",
            elabels,
            est.edges,
        )
        if est.last_edge_concurrent_sessions is not None:
            gauge(
                "switchboard_threshold_last_edge_sessions",
                "Concurrent sessions at the last trigger edge",
                elabels,
                est.last_edge_concurrent_sessions,
            )
        summary = estimator.event_summary()
        gauge(
            "switchboard_threshold_trigger_events_total",
            "Total low-interactivity trigger events recorded",
            elabels,
            summary["trigger_count"],
        )
        gauge(
            "switchboard_threshold_non_trigger_events_total",
            "Total non-trigger events (window ended without low-interactivity)",
            elabels,
            summary["non_trigger_count"],
        )

    text = "\n".join(lines) + "\n"
    await send_text(
        send, 200, text,
        content_type="text/plain; version=0.0.4; charset=utf-8",
        extra_headers=[
            *cors_extra_headers(cors_allow_origin, None),
            (b"cache-control", b"no-store"),
        ],
    )


async def handle_route_list(
    send: Send,
    route_table: RouteTableManager,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /admin/routes — list all route entries."""
    entries = []
    for entry in route_table.list_entries():
        entries.append({
            "key": entry.key,
            "providers": list(entry.providers),
        })
    body = {
        "entries": entries,
        "default": list(route_table.default_providers),
    }
    await send_json(
        send, 200, body,
        extra_headers=[
            *cors_extra_headers(cors_allow_origin, None),
            (b"cache-control", b"no-store"),
        ],
    )


async def handle_route_add(
    send: Send,
    receive: Receive,
    route_table: RouteTableManager,
    admin_token: str | None,
    scope: Scope,
    cors_allow_origin: str | None = None,
    providers: dict[str, ProviderContext] | None = None,
    route_key_secret: str | None = None,
) -> None:
    """POST /admin/routes — add or update a route entry.

    Body: ``{"key": "<raw API key>", "providers": ["umans", "ollama"]}``
    The server hashes the key before storing. The raw key is never persisted.
    When ``route_key_secret`` is set the hash is HMAC-SHA-256 (Plan 008 §3),
    so the stored digest cannot be matched to a guessed key without the
    secret; the proxy's dual-read lookup matches it back on the next request.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    ct = next(
        (
            v.decode("latin-1")
            for k, v in scope.get("headers", [])
            if k == b"content-type"
        ),
        "",
    )
    if not ct.lower().startswith("application/json"):
        await send_json(
            send, 415,
            {"error": "Content-Type must be application/json"},
            extra_headers=cors,
        )
        return

    try:
        body = await read_body(receive)
    except ValueError:
        await send_json(
            send, 413, {"error": "request body too large"},
            extra_headers=cors,
        )
        return
    except ConnectionError:
        return

    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        await send_json(
            send, 400, {"error": "invalid JSON body"},
            extra_headers=cors,
        )
        return

    if not isinstance(data, dict):
        await send_json(
            send, 400, {"error": "body must be a JSON object"},
            extra_headers=cors,
        )
        return

    raw_key = data.get("key")
    providers_raw = data.get("providers")

    if not isinstance(raw_key, str) or not raw_key:
        await send_json(
            send, 400, {"error": "missing required field 'key'"},
            extra_headers=cors,
        )
        return
    if not isinstance(providers_raw, list) or not providers_raw:
        await send_json(
            send, 400, {"error": "missing required field 'providers'"},
            extra_headers=cors,
        )
        return
    if not all(isinstance(p, str) for p in providers_raw):
        await send_json(
            send, 400, {"error": "providers must be a list of strings"},
            extra_headers=cors,
        )
        return
    if providers is not None:
        unknown = [p for p in providers_raw if p not in providers]
        if unknown:
            await send_json(
                send, 400,
                {"error": f"unknown provider(s): {', '.join(unknown)}"},
                extra_headers=cors,
            )
            return

    from switchboard.control import hash_route_key

    hashed = hash_route_key(raw_key, route_key_secret)
    route_table.add_entry(hashed, providers_raw)

    log.info("route added: %s -> %s", hashed[:16] + "...", providers_raw)

    await send_json(
        send, 200,
        {"key": hashed, "providers": list(providers_raw)},
        extra_headers=cors,
    )


async def handle_route_delete(
    send: Send,
    route_table: RouteTableManager,
    admin_token: str | None,
    scope: Scope,
    hashed_key: str,
    cors_allow_origin: str | None = None,
) -> None:
    """DELETE /admin/routes/<key> — remove a route entry."""
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    removed = route_table.remove_entry(hashed_key)
    if not removed:
        await send_json(send, 404, {"error": "route not found"}, extra_headers=cors)
        return

    log.info("route removed: %s", hashed_key[:16] + "...")
    await send_json(send, 200, {"removed": True}, extra_headers=cors)


async def handle_route_default_set(
    send: Send,
    receive: Receive,
    route_table: RouteTableManager,
    admin_token: str | None,
    scope: Scope,
    cors_allow_origin: str | None = None,
    providers: dict[str, ProviderContext] | None = None,
) -> None:
    """PUT /admin/routes/default — replace the default route.

    Body: ``{"providers": ["umans", "ollama"]}``, ordered by preference.

    This is what makes GUI provider management useful (Plan 020 WI-8): the
    model map only *filters* a route's candidate list, it never adds to it,
    so a provider that no route names is unreachable no matter how it was
    created. Before this endpoint the default route was boot-only, which made
    "add provider" in the GUI a dead end.

    The write persists, so it outranks the TOML default on the next restart
    (D1: the store wins wholesale).
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    ct = next(
        (
            v.decode("latin-1")
            for k, v in scope.get("headers", [])
            if k == b"content-type"
        ),
        "",
    )
    if not ct.lower().startswith("application/json"):
        await send_json(
            send, 415,
            {"error": "Content-Type must be application/json"},
            extra_headers=cors,
        )
        return

    try:
        body = await read_body(receive)
    except ValueError:
        await send_json(
            send, 413, {"error": "request body too large"},
            extra_headers=cors,
        )
        return
    except ConnectionError:
        return

    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        await send_json(
            send, 400, {"error": "invalid JSON body"},
            extra_headers=cors,
        )
        return

    if not isinstance(data, dict):
        await send_json(
            send, 400, {"error": "body must be a JSON object"},
            extra_headers=cors,
        )
        return

    providers_raw = data.get("providers")
    if not isinstance(providers_raw, list) or not providers_raw:
        # An empty default is not a valid state: unkeyed traffic would have no
        # candidates and every request would 503. Removing the default is not
        # an operation this API offers.
        await send_json(
            send, 400,
            {"error": "missing required field 'providers' (must be non-empty)"},
            extra_headers=cors,
        )
        return
    if not all(isinstance(p, str) for p in providers_raw):
        await send_json(
            send, 400, {"error": "providers must be a list of strings"},
            extra_headers=cors,
        )
        return

    seen: set[str] = set()
    duplicates: set[str] = set()
    for p in providers_raw:
        if p in seen:
            duplicates.add(p)
        seen.add(p)
    if duplicates:
        # Order is preference order; a repeated name has no meaning and most
        # likely means the operator edited the wrong row.
        await send_json(
            send, 400,
            {"error": f"duplicate provider(s): {', '.join(sorted(duplicates))}"},
            extra_headers=cors,
        )
        return

    if providers is not None:
        unknown = [p for p in providers_raw if p not in providers]
        if unknown:
            await send_json(
                send, 400,
                {"error": f"unknown provider(s): {', '.join(unknown)}"},
                extra_headers=cors,
            )
            return

    route_table.set_default_providers(tuple(providers_raw), persist=True)

    log.info("default route set: %s", " -> ".join(providers_raw))

    await send_json(
        send, 200,
        {"default": list(providers_raw), "persisted": route_table.db is not None},
        extra_headers=cors,
    )


async def handle_model_map_list(
    send: Send,
    model_map_mgr: ModelMapManager,
    cors_allow_origin: str | None = None,
    providers: dict[str, ProviderContext] | None = None,
) -> None:
    """GET /admin/model-map — list each model with its per-provider aliases.

    The question an operator actually asks is "which providers can serve this
    model", so each entry carries ``servable_providers`` (the alias keys) and
    the response includes ``configured_providers`` when known, letting the
    dashboard mark the providers that lack an alias for a model.
    """
    models: list[dict[str, Any]] = []
    for model, aliases in model_map_mgr.list_models():
        models.append({
            "model": model,
            "aliases": aliases,
            "servable_providers": sorted(aliases.keys()),
        })
    body: dict[str, Any] = {"models": models}
    if providers is not None:
        body["configured_providers"] = sorted(providers.keys())
    await send_json(
        send, 200, body,
        extra_headers=[
            *cors_extra_headers(cors_allow_origin, None),
            (b"cache-control", b"no-store"),
        ],
    )


async def handle_model_map_set(
    send: Send,
    receive: Receive,
    model_map_mgr: ModelMapManager,
    admin_token: str | None,
    scope: Scope,
    cors_allow_origin: str | None = None,
    providers: dict[str, ProviderContext] | None = None,
) -> None:
    """POST /admin/model-map — add or update a model's per-provider aliases.

    Body: ``{"model": "<name>", "aliases": {"umans": "umans-alias", ...}}``.

    Validates that every alias key names a configured provider — a typo here
    silently makes that provider ineligible for the model (the failure mode
    WI-017/012 exists to remove), so it is refused at write time with a message
    naming the offending provider.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    ct = next(
        (
            v.decode("latin-1")
            for k, v in scope.get("headers", [])
            if k == b"content-type"
        ),
        "",
    )
    if not ct.lower().startswith("application/json"):
        await send_json(
            send, 415,
            {"error": "Content-Type must be application/json"},
            extra_headers=cors,
        )
        return

    try:
        body = await read_body(receive)
    except ValueError:
        await send_json(
            send, 413, {"error": "request body too large"},
            extra_headers=cors,
        )
        return
    except ConnectionError:
        return

    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        await send_json(
            send, 400, {"error": "invalid JSON body"},
            extra_headers=cors,
        )
        return

    if not isinstance(data, dict):
        await send_json(
            send, 400, {"error": "body must be a JSON object"},
            extra_headers=cors,
        )
        return

    model_name = data.get("model")
    aliases_raw = data.get("aliases")

    if not isinstance(model_name, str) or not model_name:
        await send_json(
            send, 400, {"error": "missing required field 'model'"},
            extra_headers=cors,
        )
        return
    if not isinstance(aliases_raw, dict) or not aliases_raw:
        await send_json(
            send, 400, {"error": "missing required field 'aliases'"},
            extra_headers=cors,
        )
        return
    if not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in aliases_raw.items()
    ):
        await send_json(
            send, 400,
            {"error": "aliases must be an object of provider → string"},
            extra_headers=cors,
        )
        return
    if providers is not None:
        unknown = [p for p in aliases_raw if p not in providers]
        if unknown:
            await send_json(
                send, 400,
                {"error": f"unknown provider(s): {', '.join(sorted(unknown))}"},
                extra_headers=cors,
            )
            return

    aliases = {str(k): str(v) for k, v in aliases_raw.items()}
    # The store writes DB-first (WI-12b): a failure here means nothing was
    # saved, so answer 500 rather than claim success the store never made.
    try:
        model_map_mgr.set_model(model_name, aliases)
    except sqlite3.Error as exc:
        log.error("model-map set failed for %s: %s", model_name, exc)
        await send_json(
            send, 500, {"error": "model map store write failed"},
            extra_headers=cors,
        )
        return

    log.info(
        "model-map set: %s -> %d alias(es)",
        model_name, len(aliases),
    )

    await send_json(
        send, 200,
        {"model": model_name, "aliases": aliases},
        extra_headers=cors,
    )


async def handle_model_map_delete(
    send: Send,
    model_map_mgr: ModelMapManager,
    admin_token: str | None,
    scope: Scope,
    model_name: str,
    cors_allow_origin: str | None = None,
) -> None:
    """DELETE /admin/model-map/<model> — remove a model entry."""
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    # Mirrors the set handler: the store deletes DB-first, so a failure
    # means the entry is still live and 500 is the honest answer.
    try:
        removed = model_map_mgr.remove_model(model_name)
    except sqlite3.Error as exc:
        log.error("model-map delete failed for %s: %s", model_name, exc)
        await send_json(
            send, 500, {"error": "model map store write failed"},
            extra_headers=cors,
        )
        return
    if not removed:
        await send_json(
            send, 404, {"error": "model not found"}, extra_headers=cors,
        )
        return

    log.info("model-map removed: %s", model_name)
    await send_json(send, 200, {"removed": True}, extra_headers=cors)


async def handle_config_get(
    send: Send,
    routing_config: Any,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /admin/config — current routing config, in full."""
    body = routing_config_payload(routing_config)
    await send_json(
        send, 200, body,
        extra_headers=cors_extra_headers(cors_allow_origin, None),
    )


async def handle_quarantine_list(
    send: Send,
    quarantine: Any,
    admin_token: str | None,
    scope: Scope,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /admin/quarantine — what is out of service, and why (Plan 023)."""
    cors = cors_extra_headers(cors_allow_origin, None)
    if admin_token and not check_admin_auth(scope, admin_token):
        await send_json(send, 401, {"error": "unauthorized"}, extra_headers=cors)
        return
    if quarantine is None:
        await send_json(
            send, 200,
            {"enabled": False, "entries": [], "counters": {}},
            extra_headers=cors,
        )
        return
    await send_json(
        send, 200,
        {
            "enabled": True,
            "threshold": quarantine.threshold,
            "entries": [e.to_dict() for e in quarantine.entries()],
            # Pairs partway to the threshold: an operator watching a provider
            # degrade should see it before it goes out, not after.
            "counters": quarantine.counters(),
        },
        extra_headers=cors,
    )


async def handle_quarantine_release(
    send: Send,
    quarantine: Any,
    admin_token: str | None,
    scope: Scope,
    provider: str,
    model: str,
    cors_allow_origin: str | None = None,
) -> None:
    """DELETE /admin/quarantine/<provider>/<model> — the human's decision.

    Releasing is the only way out by design (Plan 023 §6): the quarantine
    fired because something needs looking at, and a timer would recreate the
    flapping it exists to stop.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return
    if quarantine is None:
        await send_json(
            send, 404, {"error": "quarantine is not enabled"},
            extra_headers=cors,
        )
        return
    if not quarantine.release(provider, model):
        await send_json(
            send, 404,
            {"error": f"{provider}/{model} is not quarantined"},
            extra_headers=cors,
        )
        return
    await send_json(
        send, 200,
        {"released": {"provider": provider, "model": model}},
        extra_headers=cors,
    )


async def handle_routing_config_update(
    send: Send,
    receive: Receive,
    proxy_app: Any,
    admin_token: str | None,
    scope: Scope,
    cors_allow_origin: str | None = None,
) -> None:
    """PUT /admin/config/routing — swap routing config at runtime (Plan 020 WI-14).

    Accepts a JSON body with any of the mutable routing fields and applies them
    as an overlay on the current config. Fields not present in the body are
    preserved unchanged. The change takes effect on the next routing decision.

    Mutable fields: ``strategy``, ``pace_burn_rate_per_day``, ``pace_flap_margin``,
    ``dwell_interval``, ``failback_delay``, ``headroom_threshold``,
    ``headroom_ranking``, ``token_budget_threshold``, ``usage_24h_threshold``,
    ``opportunistic_enabled``, ``opportunistic_min_headroom``,
    ``opportunistic_reset_window``, ``opportunistic_margin``,
    ``quarantine_threshold``.

    NOT mutable: ``affinity_max_entries`` (resizing the live table would evict
    active pins), ``pin_conversations`` (requires a body-buffering restart to
    take effect safely), ``failover_threshold_seconds``/``failover_margin``
    (retained for display only).
    """
    from switchboard.control import (
        RoutingConfig,
        RoutingStrategy,
        coerce_routing_value,
        validate_routing_field,
    )

    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    ct = next(
        (
            v.decode("latin-1")
            for k, v in scope.get("headers", [])
            if k == b"content-type"
        ),
        "",
    )
    if not ct.lower().startswith("application/json"):
        await send_json(
            send, 400, {"error": "content-type must be application/json"},
            extra_headers=cors,
        )
        return

    body: bytes = b""
    try:
        body = await read_body(receive)
    except ValueError:
        await send_json(
            send, 413, {"error": "request body too large"}, extra_headers=cors,
        )
        return
    except ConnectionError:
        return
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        await send_json(send, 400, {"error": "invalid JSON body"}, extra_headers=cors)
        return
    if not isinstance(payload, dict):
        await send_json(
            send, 400, {"error": "body must be a JSON object"}, extra_headers=cors,
        )
        return

    current = proxy_app.routing_config
    # Build the new config from the current one, applying the overlay.
    # We use the dataclass fields to construct kwargs, preserving all
    # untouched fields.
    import dataclasses

    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(current):
        kwargs[f.name] = getattr(current, f.name)

    errors: list[str] = []

    # Every field is validated against control.ROUTING_FIELD_BOUNDS — the same
    # table the TOML validator uses.  The two surfaces previously carried
    # independent copies of these bounds and drifted; `test_config_surfaces`
    # now asserts they agree, and this loop is why they can.
    for field_name in _MUTABLE_ROUTING_FIELDS:
        if field_name not in payload:
            continue
        value = payload[field_name]
        message = validate_routing_field(field_name, value)
        if message is not None:
            errors.append(message)
            continue
        # Typed from the same bounds table that validated it, so an integer
        # knob stays an integer (`quarantine_threshold` used to arrive as 3.0).
        kwargs[field_name] = coerce_routing_value(field_name, value)

    # Reject strategy + headroom_ranking conflict
    final_strategy = kwargs.get("strategy")
    final_hr = kwargs.get("headroom_ranking")
    if (
        final_strategy is not None
        and final_strategy is not RoutingStrategy.ORDERED
        and final_hr is True
    ):
        errors.append(
            f"strategy={final_strategy.value} and headroom_ranking=true "
            "are mutually exclusive — use strategy alone"
        )

    # Reject immutable / display-only fields and unknown keys
    for key in payload:
        if key not in _MUTABLE_ROUTING_FIELDS:
            errors.append(
                f"{key} is not mutable at runtime — restart to change"
            )

    if errors:
        await send_json(
            send, 400, {"error": "; ".join(errors)}, extra_headers=cors,
        )
        return

    new_config = RoutingConfig(**kwargs)
    proxy_app.update_routing_config(new_config)

    # Persist the overlay so the change survives a restart, the same rule the
    # default route follows (Plan 020 WI-8a / D1: store wins over TOML). Only
    # the fields this request set are merged in — a knob the operator never
    # touched keeps following TOML instead of being frozen at today's default.
    # Without a store the swap is live but not durable, and `persisted` in the
    # response says so rather than letting the GUI imply otherwise.
    persisted = False
    config_store = getattr(proxy_app, "config_store", None)
    if config_store is not None:
        overlay = dict(config_store.get_routing_overlay())
        for key, value in payload.items():
            overlay[key] = value
        try:
            config_store.set_routing_overlay(overlay)
            persisted = config_store.db is not None
        except Exception:
            log.warning(
                "could not persist the routing overlay; the change is live "
                "but will not survive a restart",
                exc_info=True,
            )

    body_out = {"persisted": persisted, **routing_config_payload(new_config)}
    await send_json(send, 200, body_out, extra_headers=cors)


async def handle_provider_override(
    send: Send,
    receive: Receive,
    providers: dict[str, ProviderContext],
    admin_token: str | None,
    scope: Scope,
    prov_name: str,
    method: str,
    cors_allow_origin: str | None = None,
) -> None:
    """POST/DELETE /admin/providers/<name>/override — runtime target override.

    POST body: ``{"target": <int>}`` — applies a runtime target override to
    the named provider's reconcile loop (Plan 012 WI-3).  For umans-type
    providers the loop validates against the reading's real limit/hard_cap;
    other provider classes accept any target >= 1 (their readings carry
    placeholder caps the runtime does not enforce).  DELETE reverts to the
    boot value.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    ctx = providers.get(prov_name)
    if ctx is None:
        await send_json(send, 404, {"error": "unknown provider"}, extra_headers=cors)
        return

    if method == "DELETE":
        try:
            ctx.reconcile.clear_override("target")
        except ValueError as exc:
            await send_json(send, 400, {"error": str(exc)}, extra_headers=cors)
            return
        await send_json(send, 200, {"reverted": True}, extra_headers=cors)
        return

    if method != "POST":
        await send_text(send, 405, "Method not allowed")
        return

    ct = next(
        (
            v.decode("latin-1")
            for k, v in scope.get("headers", [])
            if k == b"content-type"
        ),
        "",
    )
    if not ct.lower().startswith("application/json"):
        await send_json(
            send, 415,
            {"error": "Content-Type must be application/json"},
            extra_headers=cors,
        )
        return

    try:
        body = await read_body(receive)
    except ValueError:
        await send_json(send, 413, {"error": "request body too large"}, extra_headers=cors)
        return
    except ConnectionError:
        return

    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        await send_json(send, 400, {"error": "invalid JSON body"}, extra_headers=cors)
        return

    if not isinstance(data, dict):
        await send_json(send, 400, {"error": "body must be a JSON object"}, extra_headers=cors)
        return

    target_val = data.get("target")
    if not isinstance(target_val, int) or isinstance(target_val, bool):
        await send_json(
            send, 400,
            {"error": "missing or invalid 'target' (must be integer)"},
            extra_headers=cors,
        )
        return

    try:
        warning = ctx.reconcile.apply_override("target", target_val)
    except ValueError as exc:
        await send_json(send, 400, {"error": str(exc)}, extra_headers=cors)
        return

    log.info(
        "override applied: %s target=%d", prov_name, target_val
    )
    response: dict[str, Any] = {"applied": True, "target": target_val}
    if warning:
        response["warning"] = warning
    await send_json(send, 200, response, extra_headers=cors)


# ---------------------------------------------------------------------------
# Provider CRUD + reachability test + effective config (Plan 020 WI-3/WI-4)
# ---------------------------------------------------------------------------

#: Body fields accepted by the provider create/update endpoints — exactly the
#: config store's upsert fields (``name`` travels separately). Unknown body
#: keys are ignored rather than rejected, matching TOML's tolerance.
_PROVIDER_BODY_FIELDS = (
    "account",
    "upstream",
    "provider_type",
    "target",
    "key_mode",
    "api_key_env",
    "api_key_stored",
    "auth_header",
    "auth_prefix",
    "dashboard_url",
    "dashboard_token_env",
    "usage_key_env",
    "enabled",
)

#: Provider names that collide with admin sub-paths and so must be refused at
#: creation: a provider named e.g. "registry" would shadow
#: ``GET /admin/providers/registry`` via the generic ``<name>`` branch.
_RESERVED_PROVIDER_NAMES = frozenset({"registry", "discover"})


def _provider_fields_from_body(data: dict[str, Any]) -> dict[str, object]:
    """Whitelist the store's upsert fields out of a request body."""
    return {k: data[k] for k in _PROVIDER_BODY_FIELDS if k in data}


async def _discard_contexts(contexts: dict[str, ProviderContext]) -> None:
    """Close throwaway contexts from a dry build (never started)."""
    for ctx in contexts.values():
        with contextlib.suppress(Exception):
            await ctx.truth_source.close()
        with contextlib.suppress(Exception):
            await ctx.http_client.aclose()


async def _dry_build_error(
    name: str, section: dict[str, object]
) -> str | None:
    """Build (and discard) a context from a TOML-shaped section.

    Returns the construction path's error message, or None when the section
    builds cleanly. This is the create/update guard: a section the
    construction path would refuse — e.g. an unset ``api_key_env`` — must
    answer 400 *now* instead of poisoning the store and failing the next
    boot.
    """
    from switchboard.providers import build_provider_contexts_from_config

    try:
        built = build_provider_contexts_from_config(
            {"provider": {name: section}}
        )
    except ValueError as exc:
        return str(exc)
    await _discard_contexts(built)
    return None


#: TOML [provider.*] keys safe to serialize on read surfaces. Everything
#: else is dropped — echo-all-minus-api_key would forward operator typos
#: of credential-shaped keys (e.g. `usage_key`) straight to the browser.
_TOML_SECTION_SAFE_KEYS = (
    "upstream",
    "type",
    "target",
    "api_key_env",
    "auth_header",
    "auth_prefix",
    "dashboard_url",
    "dashboard_token_env",
    "usage_key_env",
    "poll_interval_idle",
    "dashboard_poll_interval",
    "dashboard_stale_ttl",
)


def _masked_toml_section(section: dict[str, Any]) -> dict[str, Any]:
    """Whitelist-mask a TOML provider section for a read surface.

    Adds the derived ``key_mode``/``api_key_set``/``api_key_hint`` trio so
    the GUI can pre-fill an edit form for a TOML-only provider — without it
    a defaulted key_mode on first PUT silently drops the credential (wave
    0+1 review, finding 3).
    """
    out: dict[str, Any] = {
        k: section[k] for k in _TOML_SECTION_SAFE_KEYS if k in section
    }
    api_key = section.get("api_key")
    # Precedence must match the construction path (providers.py): a set
    # api_key_env OVERRIDES an inline api_key, so the mask must call that
    # section "env" or the GUI pre-fills the mode the runtime is not using
    # (cycle-2 review, finding 2). _tombstone_fields_from_toml agrees.
    if section.get("api_key_env"):
        out["key_mode"] = "env"
        out["api_key_set"] = False
        out["api_key_hint"] = ""
    elif isinstance(api_key, str) and api_key:
        out["key_mode"] = "stored"
        out["api_key_set"] = True
        out["api_key_hint"] = api_key[-4:]
    else:
        out["key_mode"] = "passthrough"
        out["api_key_set"] = False
        out["api_key_hint"] = ""
    return out


def _restore_fields(
    section: dict[str, object], masked: dict[str, object]
) -> dict[str, object]:
    """Rebuild upsert fields from a construction-path section + masked row.

    PUT-rollback only. ``get`` masks the stored credential, so the pre-write
    capture must come from ``to_provider_section`` (raw); ``account`` and
    ``enabled`` are recovered from the masked dict because the section does
    not carry them. Never serialized — feeds ``upsert`` directly.
    """
    fields: dict[str, object] = {
        "upstream": section["upstream"],
        "provider_type": section["type"],
        "target": section["target"],
        "account": masked["account"],
        "enabled": masked["enabled"],
    }
    # Store-sourced sections carry exactly one credential key (the row's
    # key_mode picks it), so the branch order is unreachable in practice —
    # kept env-first anyway to match the construction path's precedence.
    if "api_key_env" in section:
        fields["key_mode"] = "env"
        fields["api_key_env"] = section["api_key_env"]
    elif "api_key" in section:
        fields["key_mode"] = "stored"
        fields["api_key_stored"] = section["api_key"]
    else:
        fields["key_mode"] = "passthrough"
    for key in (
        "auth_header",
        "auth_prefix",
        "dashboard_url",
        "dashboard_token_env",
        "usage_key_env",
    ):
        if key in section:
            fields[key] = section[key]
    return fields


def _tombstone_fields_from_masked(
    masked: dict[str, object],
) -> dict[str, object]:
    """Upsert fields reproducing a store row, WITHOUT touching its credential.

    Deliberately built from the MASKED dict: ``api_key_stored`` is omitted,
    and the store's write-only key semantics keep the existing credential on
    re-upsert of an existing ``key_mode='stored'`` row — a tombstone never
    needs the raw key.
    """
    fields: dict[str, object] = {}
    for key in (
        "account",
        "upstream",
        "provider_type",
        "target",
        "key_mode",
        "api_key_env",
        "auth_header",
        "auth_prefix",
        "dashboard_url",
        "dashboard_token_env",
        "usage_key_env",
    ):
        value = masked.get(key)
        if value is not None:
            fields[key] = value
    return fields


def _tombstone_fields_from_toml(
    section: dict[str, Any],
) -> dict[str, object]:
    """Synthesize store upsert fields from a boot TOML ``[provider.*]`` table.

    Needed when a TOML-only provider is deleted: D1 tombstones are store
    rows, so the section is copied into row shape (defaults mirror
    :func:`switchboard.providers.build_provider_contexts_from_config`:
    ``type='generic'``, ``target=3``). ``api_key_env`` wins over an inline
    ``api_key``, matching the construction path's precedence.
    """
    fields: dict[str, object] = {
        "upstream": section.get("upstream", ""),
        "provider_type": (
            section["type"] if isinstance(section.get("type"), str)
            else "generic"
        ),
        "target": (
            section["target"]
            if isinstance(section.get("target"), int)
            and not isinstance(section.get("target"), bool)
            else 3
        ),
    }
    api_key_env = section.get("api_key_env")
    api_key = section.get("api_key")
    if isinstance(api_key_env, str) and api_key_env:
        fields["key_mode"] = "env"
        fields["api_key_env"] = api_key_env
    elif isinstance(api_key, str) and api_key:
        fields["key_mode"] = "stored"
        fields["api_key_stored"] = api_key
    else:
        fields["key_mode"] = "passthrough"
    for key in (
        "auth_header",
        "auth_prefix",
        "dashboard_url",
        "dashboard_token_env",
        "usage_key_env",
    ):
        value = section.get(key)
        if isinstance(value, str):
            fields[key] = value
    return fields


async def handle_providers_list(
    send: Send,
    providers: dict[str, ProviderContext],
    config_store: ConfigStoreManager,
    cors_allow_origin: str | None = None,
    toml_provider_sections: dict[str, dict[str, Any]] | None = None,
) -> None:
    """GET /admin/providers — live providers joined with config-store rows.

    One entry per live provider: identity plus a minimal ``live`` sub-dict
    from the running context, merged with the store's MASKED row when one
    exists (``source`` says which config owns the provider). Disabled store
    rows (``enabled=0`` — D1 tombstones, hence not live) are included with
    ``live: false`` so the GUI can show them. Read-only and masked
    throughout (D2).
    """
    entries: list[dict[str, Any]] = []
    for name, ctx in providers.items():
        entry: dict[str, Any] = {"name": name, "enabled": True}
        masked = config_store.get(name)
        if masked is not None:
            entry.update(masked)
            entry["source"] = "store"
        else:
            entry["upstream"] = ctx.upstream_url
            entry["source"] = "toml"
            # Same masked detail the effective view gives, so the GUI can
            # pre-fill an edit form instead of guessing at key_mode
            # (finding 3: a guessed key_mode drops the credential on PUT).
            section = (toml_provider_sections or {}).get(name)
            if section is not None:
                entry.update(_masked_toml_section(section))
        entry["live"] = {
            "ready": ctx.reconcile.ready,
            "gate_closed_reason": ctx.reconcile.gate_closed_reason(),
            "target": ctx.reconcile.target,
            "upstream": ctx.upstream_url,
            "in_flight": ctx.gate.held,
        }
        entries.append(entry)
    for masked in config_store.list_providers():
        if masked["name"] in providers or masked["enabled"]:
            continue
        tombstone: dict[str, Any] = dict(masked)
        tombstone["source"] = "store"
        tombstone["live"] = False
        entries.append(tombstone)
    await send_json(
        send, 200, {"providers": entries},
        extra_headers=[
            *cors_extra_headers(cors_allow_origin, None),
            (b"cache-control", b"no-store"),
        ],
    )


async def handle_provider_create(
    send: Send,
    receive: Receive,
    provider_manager: ProviderManager,
    config_store: ConfigStoreManager,
    admin_token: str | None,
    scope: Scope,
    cors_allow_origin: str | None = None,
) -> None:
    """POST /admin/providers — create a provider (store row + live context).

    Body: the config store's upsert fields plus ``name``. The order is
    load-bearing: (a) 409 if the name is already live or stored — a create
    must never silently become an update; (b) DRY-BUILD first, so a section
    the construction path would refuse (e.g. an unset ``api_key_env``)
    answers 400 without poisoning the store; (c) only then persist; (d)
    build the live context from the stored section and register it.

    Runtime-added providers get no history ring: the boot path threads
    ``history_store_path`` into construction and this handler does not (a
    restart picks the ring up; live wiring is a Wave 2 item).
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    ct = next(
        (
            v.decode("latin-1")
            for k, v in scope.get("headers", [])
            if k == b"content-type"
        ),
        "",
    )
    if not ct.lower().startswith("application/json"):
        await send_json(
            send, 415,
            {"error": "Content-Type must be application/json"},
            extra_headers=cors,
        )
        return

    try:
        body = await read_body(receive)
    except ValueError:
        await send_json(
            send, 413, {"error": "request body too large"},
            extra_headers=cors,
        )
        return
    except ConnectionError:
        return

    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        await send_json(
            send, 400, {"error": "invalid JSON body"},
            extra_headers=cors,
        )
        return

    if not isinstance(data, dict):
        await send_json(
            send, 400, {"error": "body must be a JSON object"},
            extra_headers=cors,
        )
        return

    name = data.get("name")
    if not isinstance(name, str) or not name:
        await send_json(
            send, 400, {"error": "missing required field 'name'"},
            extra_headers=cors,
        )
        return

    if name in _RESERVED_PROVIDER_NAMES:
        await send_json(
            send, 400,
            {"error": f"'{name}' is a reserved name"},
            extra_headers=cors,
        )
        return

    if (
        name in provider_manager.providers
        or config_store.get(name) is not None
    ):
        await send_json(
            send, 409,
            {"error": f"provider '{name}' already exists"},
            extra_headers=cors,
        )
        return

    fields = _provider_fields_from_body(data)

    # Dry-build against a THROWAWAY store: validates the fields with the
    # store's own rules AND proves the construction path accepts the
    # resulting section, all before anything persists.
    scratch = ConfigStoreManager()
    try:
        scratch.upsert(name, fields)
    except ValueError as exc:
        await send_json(send, 400, {"error": str(exc)}, extra_headers=cors)
        return
    err = await _dry_build_error(name, scratch.to_provider_section(name))
    if err is not None:
        await send_json(send, 400, {"error": err}, extra_headers=cors)
        return

    try:
        config_store.upsert(name, fields)
    except ValueError as exc:
        # Same validation the scratch store just ran; defensive only.
        await send_json(send, 400, {"error": str(exc)}, extra_headers=cors)
        return
    except sqlite3.Error as exc:
        log.error("provider create failed for %s: %s", name, exc)
        await send_json(
            send, 500, {"error": "config store write failed"},
            extra_headers=cors,
        )
        return

    masked = config_store.get(name)
    if masked is not None and masked["enabled"]:
        from switchboard.providers import build_provider_contexts_from_config

        try:
            built = build_provider_contexts_from_config(
                {"provider": {name: config_store.to_provider_section(name)}}
            )
        except ValueError as exc:
            # The dry build passed moments ago; only an environment change
            # in between lands here. Undo the create — a row that cannot
            # build must not poison the next boot.
            config_store.remove(name)
            await send_json(
                send, 400, {"error": str(exc)}, extra_headers=cors,
            )
            return
        except Exception:
            # Not a validation shape (sqlite3.Error/OSError from history
            # wiring, ...): still undo the create — the row would fail the
            # next boot — and answer an honest 500.
            log.exception("provider create failed post-persist for %s", name)
            config_store.remove(name)
            await send_json(
                send, 500,
                {"error": "provider create failed; row removed"},
                extra_headers=cors,
            )
            return
        try:
            await provider_manager.add(name, built[name])
        except ValueError:
            await _discard_contexts(built)
            config_store.remove(name)
            await send_json(
                send, 409,
                {"error": f"provider '{name}' already exists"},
                extra_headers=cors,
            )
            return
        except Exception:
            log.exception("provider registration failed for %s", name)
            await _discard_contexts(built)
            config_store.remove(name)
            await send_json(
                send, 500,
                {"error": "provider registration failed; row removed"},
                extra_headers=cors,
            )
            return

    log.info("provider created: %s", name)
    await send_json(send, 200, masked or {}, extra_headers=cors)


async def handle_provider_update(
    send: Send,
    receive: Receive,
    provider_manager: ProviderManager,
    config_store: ConfigStoreManager,
    admin_token: str | None,
    scope: Scope,
    prov_name: str,
    cors_allow_origin: str | None = None,
) -> None:
    """PUT /admin/providers/<name> — update a provider (store + live swap).

    Write-only key semantics force a different order than create: with
    ``key_mode='stored'`` an absent key on edit means "keep", and only the
    REAL store knows the kept credential. So: upsert into the real store
    first, build from the store's own section, and on build failure ROLL
    BACK by re-upserting the previous row — captured via
    ``to_provider_section`` *before* the write, because ``get`` is masked
    and cannot restore a credential. The build doubles as the dry-run: the
    live swap happens only after it succeeds.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    ct = next(
        (
            v.decode("latin-1")
            for k, v in scope.get("headers", [])
            if k == b"content-type"
        ),
        "",
    )
    if not ct.lower().startswith("application/json"):
        await send_json(
            send, 415,
            {"error": "Content-Type must be application/json"},
            extra_headers=cors,
        )
        return

    try:
        body = await read_body(receive)
    except ValueError:
        await send_json(
            send, 413, {"error": "request body too large"},
            extra_headers=cors,
        )
        return
    except ConnectionError:
        return

    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        await send_json(
            send, 400, {"error": "invalid JSON body"},
            extra_headers=cors,
        )
        return

    if not isinstance(data, dict):
        await send_json(
            send, 400, {"error": "body must be a JSON object"},
            extra_headers=cors,
        )
        return

    live = prov_name in provider_manager.providers
    prev_masked = config_store.get(prov_name)
    if not live and prev_masked is None:
        await send_json(
            send, 404, {"error": "unknown provider"}, extra_headers=cors,
        )
        return
    prev_section = (
        config_store.to_provider_section(prov_name)
        if prev_masked is not None
        else None
    )

    fields = _provider_fields_from_body(data)
    try:
        config_store.upsert(prov_name, fields)
    except ValueError as exc:
        # Validation runs before the DB write — store unchanged, no rollback.
        await send_json(send, 400, {"error": str(exc)}, extra_headers=cors)
        return
    except sqlite3.Error as exc:
        log.error("provider update failed for %s: %s", prov_name, exc)
        await send_json(
            send, 500, {"error": "config store write failed"},
            extra_headers=cors,
        )
        return

    from switchboard.providers import build_provider_contexts_from_config

    section = config_store.to_provider_section(prov_name)
    try:
        built = build_provider_contexts_from_config(
            {"provider": {prov_name: section}}
        )
    except ValueError as exc:
        # Roll back: restore the captured previous row, or remove the row
        # this update just created for a TOML-only provider.
        rollback_failed = False
        try:
            if prev_section is not None and prev_masked is not None:
                config_store.upsert(
                    prov_name, _restore_fields(prev_section, prev_masked)
                )
            else:
                config_store.remove(prov_name)
        except (ValueError, sqlite3.Error):
            rollback_failed = True
            log.error(
                "provider update rollback failed for %s", prov_name,
                exc_info=True,
            )
        err_body: dict[str, Any] = {"error": str(exc)}
        if rollback_failed:
            # The store now holds the row that failed to build; the next
            # boot will warn/skip it, but the operator must know it is
            # dirty rather than discover it later.
            err_body["rollback_failed"] = True
        await send_json(send, 400, err_body, extra_headers=cors)
        return

    masked = config_store.get(prov_name)
    if masked is not None and masked["enabled"]:
        await provider_manager.replace(prov_name, built[prov_name])
    else:
        # The update disabled the row (D1 tombstone): the provider must
        # leave the live map, not be replaced in it.
        await _discard_contexts(built)
        await provider_manager.remove(prov_name)

    log.info("provider updated: %s", prov_name)
    await send_json(send, 200, masked or {}, extra_headers=cors)


async def handle_provider_delete(
    send: Send,
    provider_manager: ProviderManager,
    config_store: ConfigStoreManager,
    admin_token: str | None,
    scope: Scope,
    prov_name: str,
    toml_provider_names: frozenset[str],
    toml_provider_sections: dict[str, dict[str, Any]],
    cors_allow_origin: str | None = None,
) -> None:
    """DELETE /admin/providers/<name> — remove, tombstoning TOML providers.

    D1 precedence makes plain removal insufficient for a TOML-declared
    provider: the next boot would re-load it from the file. Those get a
    tombstone instead — the store row (or a row synthesized from the boot
    TOML section) upserted with ``enabled=0``, which ``effective_providers``
    treats as "remove from the effective set". Store-only providers are
    genuinely deleted. Either way the live context is deregistered and
    drained under the WI-2 rules.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    live = prov_name in provider_manager.providers
    masked = config_store.get(prov_name)
    if not live and masked is None:
        await send_json(
            send, 404, {"error": "unknown provider"}, extra_headers=cors,
        )
        return

    tombstoned = False
    if prov_name in toml_provider_names:
        if masked is not None:
            fields = _tombstone_fields_from_masked(masked)
        else:
            fields = _tombstone_fields_from_toml(
                toml_provider_sections.get(prov_name, {})
            )
        fields["enabled"] = 0
        try:
            config_store.upsert(prov_name, fields)
        except ValueError as exc:
            # Not the client's fault: the boot TOML section does not satisfy
            # the store's row rules (e.g. target=0). Surface it honestly.
            log.error(
                "provider tombstone failed for %s: %s", prov_name, exc
            )
            await send_json(
                send, 500,
                {"error": f"could not tombstone provider: {exc}"},
                extra_headers=cors,
            )
            return
        except sqlite3.Error as exc:
            log.error(
                "provider tombstone failed for %s: %s", prov_name, exc
            )
            await send_json(
                send, 500, {"error": "config store write failed"},
                extra_headers=cors,
            )
            return
        tombstoned = True
    elif masked is not None:
        try:
            config_store.remove(prov_name)
        except sqlite3.Error as exc:
            log.error("provider delete failed for %s: %s", prov_name, exc)
            await send_json(
                send, 500, {"error": "config store write failed"},
                extra_headers=cors,
            )
            return

    await provider_manager.remove(prov_name)
    log.info(
        "provider removed: %s (tombstoned=%s)", prov_name, tombstoned,
    )
    await send_json(
        send, 200, {"removed": True, "tombstoned": tombstoned},
        extra_headers=cors,
    )


async def handle_provider_test(
    send: Send,
    providers: dict[str, ProviderContext],
    admin_token: str | None,
    scope: Scope,
    prov_name: str,
    cors_allow_origin: str | None = None,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> None:
    """POST /admin/providers/<name>/test — upstream reachability probe.

    Issues ``GET {upstream}/models`` with the provider's own credential,
    resolved exactly as the forwarding path presents it (the live context's
    ``api_key``/``auth_header``/``auth_prefix``), and reports status +
    latency. The credential is applied to the OUTBOUND request only and
    appears nowhere in the response — no header echo, no key material.
    Auth-gated like the mutating endpoints because each call spends a
    request against a real upstream. ``client_factory`` exists for tests
    (MockTransport); the default is a short-lived client with a 5 s timeout.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    ctx = providers.get(prov_name)
    if ctx is None:
        await send_json(
            send, 404, {"error": "unknown provider"}, extra_headers=cors,
        )
        return

    url = ctx.upstream_url.rstrip("/") + "/models"
    headers: dict[str, str] = {}
    if ctx.api_key:
        headers[ctx.auth_header] = f"{ctx.auth_prefix}{ctx.api_key}"
    factory = client_factory or (
        lambda: httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    )

    status_code: int | None = None
    detail = ""
    start = time.monotonic()
    try:
        async with factory() as client:
            response = await client.get(url, headers=headers)
            status_code = response.status_code
    except httpx.TimeoutException:
        detail = "timeout"
    except httpx.HTTPError as exc:
        detail = type(exc).__name__
    latency_ms = round((time.monotonic() - start) * 1000.0, 1)

    ok = status_code is not None and 200 <= status_code < 300
    log.info(
        "provider test: %s -> status=%s ok=%s", prov_name, status_code, ok,
    )
    await send_json(
        send, 200,
        {
            "ok": ok,
            "status": status_code,
            "latency_ms": latency_ms,
            "detail": detail,
        },
        extra_headers=cors,
    )


# ── Plan 021 Wave 2: registry, path preview, discovery probe ────────────────


async def handle_provider_registry(
    send: Send,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /admin/providers/registry — curated provider registry (Plan 021 WI-3).

    Feeds the GUI provider-picker.  Each entry carries the vendor's documented
    base URL (paste-ready), the expected auth header, whether it needs a
    usage key, and the probe endpoint for the discovery probe.  No secrets;
    auth-gated at the dispatch layer for topology consistency.
    """
    from switchboard.truth import registry_entries

    cors = cors_extra_headers(cors_allow_origin, None)
    entries = [
        {
            "name": p.name,
            "default_base_url": p.default_base_url,
            "auth_header": p.auth_header,
            "needs_usage_key": p.needs_usage_key,
            "probe_endpoint": p.probe_endpoint,
        }
        for p in registry_entries()
    ]
    await send_json(send, 200, {"providers": entries}, extra_headers=cors)


async def handle_preview_path(
    send: Send,
    scope: Scope,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /admin/preview-path?base=...&path=/v1/chat/completions (Plan 021 WI-5).

    Live preview of :func:`compose_upstream_path` — pure computation, no
    secrets, no upstream.  The GUI calls this per keystroke so the operator
    sees the exact URL switchboard will egress before they save.
    """
    from switchboard.control import compose_upstream_path

    cors = cors_extra_headers(cors_allow_origin, None)
    raw_qs = scope.get("query_string") or b""
    qs = parse_qs(raw_qs.decode("ascii", "replace"))
    base = (qs.get("base") or [""])[0]
    client_path = (qs.get("path") or ["/v1/chat/completions"])[0]
    composed = compose_upstream_path(base, client_path) if base else ""
    await send_json(send, 200, {"composed": composed}, extra_headers=cors)


# A path segment that names an API version: v1, v2, v1beta, v1alpha2. Must
# stay byte-identical to switchboard.control._VERSION_SEGMENT so discovery's
# strip-trailing-version candidate matches the composition rule exactly.
_DISCOVER_VERSION = re.compile(r"^v\d+(?:[a-z]+\d*)?$")


def _discover_candidates(base: str, probe: str) -> list[str]:
    """The ordered, de-duplicated set of upstream URLs to try for a base.

    Plan 021 D5 — three plausible compositions, in order:

    1. ``{base}{probe}`` — the base already carries the version (the common
       case, e.g. ``https://ollama.com/v1`` + ``/models``).
    2. ``{base}/v1{probe}`` — the base is a bare host relying on the client
       to supply the version.
    3. strip a trailing ``/vN`` from the base, then ``{probe}`` — the base has
       a version the endpoint would otherwise duplicate.
    """
    b = base.rstrip("/")
    p = probe if probe.startswith("/") else "/" + probe
    candidates: list[str] = []

    def _add(url: str) -> None:
        if url not in candidates:
            candidates.append(url)

    _add(b + p)
    _add(b + "/v1" + p)
    tail = b.rsplit("/", 1)[-1]
    if _DISCOVER_VERSION.match(tail):
        stripped = b.rsplit("/", 1)[0]
        if stripped:
            _add(stripped + p)
    return candidates


async def handle_provider_discover(
    send: Send,
    receive: Receive,
    admin_token: str | None,
    scope: Scope,
    cors_allow_origin: str | None = None,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> None:
    """POST /admin/providers/discover — probe a base URL's compositions (Plan 021 WI-4).

    The operator pastes a vendor base URL BEFORE saving and asks switchboard
    which composition answers.  Tries the candidates from
    :func:`_discover_candidates` in order and reports status + latency for
    each, so the GUI can show what was tried and offer to save the winner.

    Uses the provided credential (optional) exactly as the forwarding path
    would present it, and reports status/latency only — never echoes the key
    (no header echo, no key material).  Auth + CSRF gated because each call
    spends a request against a real upstream.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    ct = next(
        (
            v.decode("latin-1")
            for k, v in scope.get("headers", [])
            if k == b"content-type"
        ),
        "",
    )
    if not ct.lower().startswith("application/json"):
        await send_json(
            send, 415,
            {"error": "Content-Type must be application/json"},
            extra_headers=cors,
        )
        return

    try:
        raw = await read_body(receive)
    except ValueError:
        await send_json(
            send, 413, {"error": "request body too large"},
            extra_headers=cors,
        )
        return
    except ConnectionError:
        # Client disconnected mid-upload: nothing to send back.
        return

    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        await send_json(send, 400, {"error": "invalid JSON body"}, extra_headers=cors)
        return
    if not isinstance(data, dict):
        await send_json(send, 400, {"error": "body must be a JSON object"}, extra_headers=cors)
        return

    base = str(data.get("base_url", "")).strip()
    probe = str(data.get("probe_endpoint", "/models")).strip() or "/models"
    api_key = str(data.get("api_key", "") or "")
    auth_header = str(data.get("auth_header", "authorization")) or "authorization"
    auth_prefix = str(data.get("auth_prefix", "Bearer "))
    if not base:
        await send_json(send, 400, {"error": "base_url required"}, extra_headers=cors)
        return

    urls = _discover_candidates(base, probe)
    headers: dict[str, str] = (
        {auth_header: f"{auth_prefix}{api_key}"} if api_key else {}
    )
    factory = client_factory or (
        lambda: httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    )

    results: list[dict[str, Any]] = []
    for url in urls:
        status_code: int | None = None
        detail = ""
        start = time.monotonic()
        try:
            async with factory() as client:
                response = await client.get(url, headers=headers)
                status_code = response.status_code
        except httpx.TimeoutException:
            detail = "timeout"
        except httpx.HTTPError as exc:
            detail = type(exc).__name__
        latency_ms = round((time.monotonic() - start) * 1000.0, 1)
        ok = status_code is not None and 200 <= status_code < 300
        results.append(
            {
                "url": url,
                "status": status_code,
                "latency_ms": latency_ms,
                "ok": ok,
                "detail": detail,
            }
        )

    log.info(
        "provider discover: %s -> %d candidates, ok=%s",
        base,
        len(results),
        [r["url"] for r in results if r["ok"]],
    )
    await send_json(
        send, 200, {"base_url": base, "candidates": results}, extra_headers=cors,
    )


async def handle_config_effective(
    send: Send,
    config_store: ConfigStoreManager,
    toml_provider_names: frozenset[str],
    toml_provider_sections: dict[str, dict[str, Any]],
    cors_allow_origin: str | None = None,
    env_field_sources: dict[str, dict[str, str]] | None = None,
    unmatched_env: list[str] | None = None,
) -> None:
    """GET /admin/config/effective — the merged TOML+store view, MASKED.

    Deliberately does NOT call ``effective_providers()`` — that is the
    construction path and carries raw credentials. The view is assembled
    from read surfaces instead: store rows via ``list_providers()`` (already
    masked, tombstones included with ``enabled: false``), and TOML-only
    providers from the boot sections with any inline ``api_key`` replaced by
    ``api_key_set`` + a last-4 hint. Per D1, a store row shadows its TOML
    section wholesale, so store-named providers show the store's row.
    """
    env_field_sources = env_field_sources or {}
    entries: list[dict[str, Any]] = []
    store_names: set[str] = set()
    for masked in config_store.list_providers():
        row: dict[str, Any] = dict(masked)
        row["source"] = "store"
        entries.append(row)
        store_names.add(str(masked["name"]))
    for name in toml_provider_names:
        if name in store_names:
            continue
        section = toml_provider_sections.get(name, {})
        entry: dict[str, Any] = {"name": name, "source": "toml", "enabled": True}
        entry.update(_masked_toml_section(section))
        entries.append(entry)

    # Per-field provenance (Plan 021 D6). The row-level `source` says which
    # tier owns the provider; this says which tier owns each FIELD, because
    # env merges per field rather than replacing the row. It is what lets the
    # GUI lock an input instead of accepting an edit it cannot honour, and
    # what lets an operator answer "why is it this value" without reading the
    # Deployment.
    for entry in entries:
        owned = env_field_sources.get(str(entry["name"]))
        if owned:
            entry["field_sources"] = dict(owned)
            for field in owned:
                if field in entry:
                    entry["env_locked"] = sorted(
                        set(entry.get("env_locked", [])) | {field}
                    )

    entries.sort(key=lambda e: str(e["name"]))
    body: dict[str, Any] = {"providers": entries}
    if unmatched_env:
        # Inert overrides are reported rather than only logged: an operator who
        # typoed a provider name otherwise believes the deployment controls a
        # field it does not, and a log line scrolls away.
        body["unmatched_env_overrides"] = list(unmatched_env)
    await send_json(
        send, 200, body,
        extra_headers=[
            *cors_extra_headers(cors_allow_origin, None),
            (b"cache-control", b"no-store"),
        ],
    )


async def handle_config_reset(
    send: Send,
    receive: Receive,
    route_table: RouteTableManager,
    admin_token: str | None,
    scope: Scope,
    cors_allow_origin: str | None = None,
) -> None:
    """POST /admin/config/reset — clear store rows so declared config wins.

    Body: ``{"sections": ["model-map", "providers"]}`` or
    ``{"sections": ["all"]}``.

    Plan 021 D7. The store outranks the mounted TOML, which is what makes GUI
    edits survive a restart — and also what makes a bad one unfixable by
    editing the configmap and rolling the pod. This is the way back: delete
    the rows, and the declared configuration becomes authoritative on the next
    load.

    Deliberately NOT a wildcard by default, and deliberately not idempotent-
    by-silence: the response names every row deleted, because an operator
    reaching for this is already having a bad day and "it said OK" is not
    enough to know what was discarded.

    The reset takes effect for the sections whose managers reload from the
    store; a restart is the honest way to guarantee the whole process reflects
    it, and the response says so.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not admin_token:
        await send_json(
            send, 405,
            {"error": "mutations disabled — set --admin-token to enable"},
            extra_headers=cors,
        )
        return
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 403, {"error": "unauthorized"}, extra_headers=cors)
        return
    if not check_csrf(scope, admin_token):
        await send_json(
            send, 403, {"error": "cross-site request blocked"},
            extra_headers=cors,
        )
        return

    ct = next(
        (
            v.decode("latin-1")
            for k, v in scope.get("headers", [])
            if k == b"content-type"
        ),
        "",
    )
    if not ct.lower().startswith("application/json"):
        await send_json(
            send, 415,
            {"error": "Content-Type must be application/json"},
            extra_headers=cors,
        )
        return

    try:
        body = await read_body(receive)
    except ValueError:
        await send_json(
            send, 413, {"error": "request body too large"}, extra_headers=cors
        )
        return
    except ConnectionError:
        return

    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        await send_json(
            send, 400, {"error": "invalid JSON body"}, extra_headers=cors
        )
        return

    if not isinstance(data, dict):
        await send_json(
            send, 400, {"error": "body must be a JSON object"}, extra_headers=cors
        )
        return

    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        await send_json(
            send, 400,
            {
                "error": "missing required field 'sections' (non-empty list)",
                "valid_sections": sorted(SECTIONS),
            },
            extra_headers=cors,
        )
        return
    if not all(isinstance(s, str) for s in raw_sections):
        await send_json(
            send, 400, {"error": "sections must be a list of strings"},
            extra_headers=cors,
        )
        return

    try:
        sections = parse_sections(",".join(raw_sections))
    except ResetError as exc:
        await send_json(
            send, 400,
            {"error": str(exc), "valid_sections": sorted(SECTIONS)},
            extra_headers=cors,
        )
        return

    deleted = reset_sections(route_table.db, sections)

    log.warning(
        "config reset via admin API: %s",
        ", ".join(f"{s}={len(rows)}" for s, rows in sorted(deleted.items())),
    )

    await send_json(
        send, 200,
        {
            "reset": sections,
            "deleted": deleted,
            "persisted": route_table.db is not None,
            "note": (
                "restart to guarantee every in-memory manager reflects this"
            ),
        },
        extra_headers=cors,
    )


async def handle_usage_history(
    send: Send,
    scope: Scope,
    admin_token: str | None,
    providers: dict[str, ProviderContext],
    cors_allow_origin: str | None = None,
) -> None:
    """GET /admin/usage-history?provider=<name>&from=...&to=...&granularity=hour

    Proxies umans ``/v1/usage/history`` for the dashboard.  Admin-only —
    the usage API key never reaches the browser.  Fails safe: 502 on any
    upstream error.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 401, {"error": "unauthorized"}, extra_headers=cors)
        return

    from urllib.parse import parse_qs as _parse_qs

    qs = scope.get("query_string", b"").decode("latin-1")
    parsed = _parse_qs(qs)
    params: dict[str, str] = {}
    for key in ("from", "to", "granularity", "scope"):
        val = parsed.get(key)
        if val:
            params[key] = val[0]

    provider_name = parsed.get("provider", [""])[0] if parsed else ""
    if not provider_name:
        await send_json(
            send, 400,
            {"error": "missing required param 'provider'"},
            extra_headers=cors,
        )
        return

    ctx = providers.get(provider_name)
    if ctx is None or not ctx.usage_base_url or not ctx.usage_api_key:
        await send_json(
            send, 404,
            {"error": f"provider '{provider_name}' has no usage-history endpoint"},
            extra_headers=cors,
        )
        return

    if "from" not in params or "to" not in params:
        await send_json(
            send, 400,
            {"error": "missing required params 'from' and 'to'"},
            extra_headers=cors,
        )
        return

    if "granularity" not in params:
        params["granularity"] = "hour"

    url = ctx.usage_base_url.rstrip("/") + "/v1/usage/history"
    if ctx.usage_auth_header.lower() == "x-api-key":
        headers = {"x-api-key": ctx.usage_api_key, "Accept": "application/json"}
    else:
        headers = {
            "Authorization": f"Bearer {ctx.usage_api_key}",
            "Accept": "application/json",
        }

    try:
        import httpx as _httpx

        async with _httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
    except _httpx.HTTPStatusError as exc:
        log.warning("usage history fetch failed: %s", exc)
        await send_json(
            send, 502,
            {"error": f"upstream returned {exc.response.status_code}"},
            extra_headers=cors,
        )
        return
    except Exception as exc:
        log.warning("usage history fetch error: %s: %s", type(exc).__name__, exc)
        await send_json(
            send, 502,
            {"error": f"upstream fetch failed: {type(exc).__name__}"},
            extra_headers=cors,
        )
        return

    await send_json(
        send, 200, data,
        extra_headers=[*cors, (b"cache-control", b"no-store")],
    )


async def handle_threshold_events(
    send: Send,
    scope: Scope,
    admin_token: str | None,
    estimator: ThresholdEstimator | None,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /admin/threshold-events?limit=<N>

    Returns the 30-day rolling history of low-interactivity threshold
    events — both triggers (low priority engaged) and non-triggers
    (window ended without engaging).  Admin-only.
    """
    cors = cors_extra_headers(cors_allow_origin, None)
    if not check_admin_auth(scope, admin_token):
        await send_json(send, 401, {"error": "unauthorized"}, extra_headers=cors)
        return

    if estimator is None:
        await send_json(
            send, 404,
            {"error": "threshold estimator not configured"},
            extra_headers=cors,
        )
        return

    from urllib.parse import parse_qs as _parse_qs

    qs = scope.get("query_string", b"").decode("latin-1")
    parsed = _parse_qs(qs) if qs else {}
    limit = 50
    if parsed and "limit" in parsed:
        with contextlib.suppress(ValueError, IndexError):
            limit = max(1, min(500, int(parsed["limit"][0])))

    events = estimator.recent_events(limit=limit)
    summary = estimator.event_summary()
    est = estimator.state().estimate

    await send_json(
        send, 200,
        {
            "provider": estimator.provider_name,
            "estimate": {
                "edges": est.edges,
                "requests": {
                    "lower": est.requests.lower,
                    "upper": est.requests.upper,
                    "best_guess": est.requests.best_guess,
                    "contradicted": est.requests.contradicted,
                },
                "tokens": {
                    "lower": est.tokens.lower,
                    "upper": est.tokens.upper,
                    "best_guess": est.tokens.best_guess,
                    "contradicted": est.tokens.contradicted,
                },
                "last_edge_concurrent_sessions": est.last_edge_concurrent_sessions,
            },
            "summary": summary,
            "events": events,
        },
        extra_headers=[*cors, (b"cache-control", b"no-store")],
    )


async def serve_static(path: str, send: Send) -> None:
    """Serve a file from the switchboard static directory."""
    rel = path[len("/static/"):]
    try:
        file_path = (_STATIC_DIR / rel).resolve()
        file_path.relative_to(_STATIC_DIR)
    except (ValueError, OSError):
        await send_text(send, 404, "Not found")
        return
    if not file_path.is_file():
        await send_text(send, 404, "Not found")
        return
    ext = file_path.suffix.lower()
    content_type = _STATIC_CONTENT_TYPES.get(ext, "application/octet-stream")
    data = file_path.read_bytes()
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", content_type.encode()),
        (b"content-length", str(len(data)).encode()),
        (b"cache-control", b"public, max-age=3600"),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": data, "more_body": False})


async def send_dashboard(
    send: Send,
    cors_allow_origin: str | None = None,
) -> None:
    """Serve the dashboard HTML page."""
    await send_text(
        send, 200, _DASHBOARD_HTML,
        content_type="text/html; charset=utf-8",
        extra_headers=cors_extra_headers(cors_allow_origin, None),
    )


async def send_login_page(
    send: Send,
    cors_allow_origin: str | None = None,
) -> None:
    """Serve the login HTML page."""
    await send_text(
        send, 200, _LOGIN_HTML,
        content_type="text/html; charset=utf-8",
        extra_headers=cors_extra_headers(cors_allow_origin, None),
    )


async def handle_login_get(
    send: Send,
    admin_token: str | None,
    cors_allow_origin: str | None = None,
) -> None:
    """GET /login — serve the login form, or 404 if no token configured."""
    if not admin_token:
        await send_text(send, 404, "Not found")
        return
    await send_login_page(send, cors_allow_origin)


async def handle_login_post(
    send: Send,
    receive: Receive,
    admin_token: str | None,
    scope: Scope,
    throttle: LoginThrottle,
    trusted_proxies: frozenset[
        ipaddress.IPv4Network | ipaddress.IPv6Network
    ] = frozenset(),
) -> None:
    """POST /login — verify token, set session cookie, redirect to /."""
    if not admin_token:
        await send_text(send, 404, "Not found")
        return

    now = time.time()

    if throttle.is_locked(now):
        retry = throttle.retry_after(now)
        log.warning("login throttled — retry_after=%d", retry)
        await send_json(
            send, 429,
            {"error": "too many attempts", "retry_after": retry},
            retry_after=retry,
        )
        return

    try:
        body = await read_body(receive)
    except ValueError:
        await send_text(send, 413, "request body too large")
        return
    except ConnectionError:
        return

    params = parse_qs(body.decode("utf-8", errors="replace"))
    token = params.get("token", [""])[0]

    if not token or not hmac.compare_digest(
        token.encode("utf-8"), admin_token.encode("utf-8")
    ):
        throttle.record_failure(now)
        log.warning("login failed — remote=%s", _extract_remote(scope))
        await asyncio.sleep(0.2)
        await send_text(
            send, 303, "",
            extra_headers=[(b"location", b"/login?error=1")],
        )
        return

    throttle.record_success(now)
    cookie_value = mint_session(admin_token, now, _SESSION_TTL)
    set_cookie = build_set_cookie(
        cookie_value, _SESSION_TTL, scope, trusted_proxies
    )
    await send_text(
        send, 303, "",
        extra_headers=[(b"location", b"/"), (b"set-cookie", set_cookie)],
    )


async def handle_logout(
    send: Send,
    admin_token: str | None,
    scope: Scope,
    trusted_proxies: frozenset[
        ipaddress.IPv4Network | ipaddress.IPv6Network
    ] = frozenset(),
) -> None:
    """POST /logout — clear session cookie and redirect to /login."""
    if not admin_token:
        await send_text(send, 303, "", extra_headers=[(b"location", b"/")])
        return
    if not check_csrf(scope, admin_token):
        await send_text(send, 403, "cross-site request blocked")
        return
    set_cookie = build_set_cookie("", 0, scope, trusted_proxies)
    await send_text(
        send, 303, "",
        extra_headers=[(b"location", b"/login"), (b"set-cookie", set_cookie)],
    )


def _extract_remote(scope: Scope) -> str:
    """Extract the client IP from the ASGI scope."""
    client = scope.get("client")
    return client[0] if client else "unknown"
