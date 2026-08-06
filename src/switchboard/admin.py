"""Admin route handlers — health, readiness, status, metrics, CRUD (route
table + model map), and dashboard.

Stateless functions that receive the proxy's state as arguments. Shared
utilities (``send_json``, ``send_text``, ``check_admin_auth``) are borrowed
from :mod:`sluice.admin` to avoid duplication. Switchboard-specific handlers
build multi-provider status payloads and manage route table CRUD.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from sluice.admin import (
    build_set_cookie,
    check_admin_auth,
    check_csrf,
    cors_extra_headers,
    read_body,
    send_json,
    send_text,
)
from sluice.session import (
    SESSION_COOKIE,
    LoginThrottle,
    mint_session,
)

from switchboard import __version__

if TYPE_CHECKING:
    from switchboard.estimator import ThresholdEstimator
    from switchboard.model_map import ModelMapManager
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


def _provider_status(
    ctx: ProviderContext,
    overload_tracker: Any | None = None,
    budget_tracker: Any | None = None,
    usage_history_tracker: Any | None = None,
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
) -> dict[str, Any]:
    """Build the full status payload for /status.json."""
    provider_states: dict[str, Any] = {}
    for name, ctx in providers.items():
        provider_states[name] = _provider_status(
            ctx,
            overload_tracker=overload_tracker,
            budget_tracker=budget_tracker,
            usage_history_tracker=usage_history_tracker,
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
            "usage_reroutes_total": routing_metrics.usage_reroutes_total,
            "usage_reroutes_from": dict(routing_metrics.usage_reroutes_from),
        },
        "version": __version__,
        "build": build_sha,
    }

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
) -> None:
    """GET /status.json — per-provider state + route table + routing metrics."""
    payload = _build_status_payload(
        providers, route_table, routing_metrics, build_sha,
        estimator=estimator,
        overload_tracker=overload_tracker,
        budget_tracker=budget_tracker,
        usage_history_tracker=usage_history_tracker,
        model_map_mgr=model_map_mgr,
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
) -> None:
    """POST /admin/routes — add or update a route entry.

    Body: ``{"key": "<raw API key>", "providers": ["umans", "ollama"]}``
    The server hashes the key before storing. The raw key is never persisted.
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

    hashed = hash_route_key(raw_key)
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
    model_map_mgr.set_model(model_name, aliases)

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

    removed = model_map_mgr.remove_model(model_name)
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
    """GET /admin/config — current routing config."""
    body = {
        "failover_threshold_seconds": routing_config.failover_threshold_seconds,
        "failover_margin": routing_config.failover_margin,
        "dwell_interval": routing_config.dwell_interval,
        "headroom_threshold": routing_config.headroom_threshold,
        "token_budget_threshold": routing_config.token_budget_threshold,
    }
    await send_json(
        send, 200, body,
        extra_headers=cors_extra_headers(cors_allow_origin, None),
    )


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
    the named provider's reconcile loop (Plan 012 WI-3).  The reconcile loop
    validates against the provider's hard_cap.  DELETE reverts to boot value.
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
