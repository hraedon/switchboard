"""Async multi-provider reverse proxy shell — streaming passthrough with routing.

Holds multiple :class:`~switchboard.providers.ProviderContext` instances and
routes each request to the best available upstream based on real-time pressure
signals.  The routing decision is made by the pure
:func:`~switchboard.control.route_decision` function, which returns an
:class:`~switchboard.control.AdmissionPlan`; this module is the thin async
shell that consumes the plan, acquires permits, streams bytes, and records
metrics.

Admission algorithm (Plan 006 §4):

1. For each ``immediate_candidate``, call a non-blocking gate acquire.
2. Forward through the first successful acquisition.
3. If all immediate attempts lose the snapshot race, perform one final
   non-blocking pass over the remaining eligible candidates.
4. If configured, wait only on ``queue_candidate`` for the remaining queue
   budget.
5. After queue timeout, return an honest 503 derived from the best available
   structural signal.

Streaming logic is adapted from sluice's ``_forward()``: true streaming
(request and response bytes forwarded as they arrive, never buffered),
disconnect detection, phantom prevention (cancel upstream on client disconnect),
and hop-by-hop header stripping.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from sluice.admin import (
    check_admin_auth,
    cors_extra_headers,
    is_admin_auth_value,
    send_json,
    send_text,
)
from sluice.reconcile import RETRY_AFTER_SHORT
from sluice.session import (
    SESSION_COOKIE,
    LoginThrottle,
)

from switchboard.admin import (
    handle_config_get,
    handle_healthz,
    handle_login_get,
    handle_login_post,
    handle_logout,
    handle_readyz,
    handle_route_add,
    handle_route_delete,
    handle_route_list,
    send_dashboard,
    send_login_page,
    send_prometheus,
    send_status_json,
    serve_static,
)
from switchboard.control import (
    AdmissionPlan,
    ModelMap,
    RouteAffinity,
    RoutingConfig,
    hash_route_key,
    route_decision,
)
from switchboard.estimator import ThresholdEstimator
from switchboard.overload import OverloadConfig, OverloadTracker
from switchboard.providers import ProviderContext, snapshot_provider_state
from switchboard.route_table import RouteTableManager
from switchboard.token_budget import TokenBudgetTracker
from switchboard.usage_observer import UsageObserver

log = logging.getLogger("switchboard.proxy")

Scope = dict[str, Any]
Send = Any
Receive = Any


_DRAIN_POLL_INTERVAL = 0.1
_QUEUE_TIMEOUT_DEFAULT = 30.0
_DRAIN_TIMEOUT_DEFAULT = 25.0
_RECENT_DECISIONS_MAX = 128
_AFFINITY_MAX = 1024
_OVERLOAD_STATUSES_DEFAULT = frozenset({503, 529})


def _parse_retry_after_seconds(raw: str | None) -> float | None:
    """Parse a Retry-After header into seconds, or None if unparseable."""
    if raw is None:
        return None
    raw = raw.strip()
    try:
        return float(int(raw))
    except (ValueError, TypeError):
        pass
    try:
        dt = parsedate_to_datetime(raw)
        remaining = dt.timestamp() - time.time()
        return max(0.0, remaining)
    except (ValueError, TypeError, OverflowError):
        return None


def _extract_model(body: bytes) -> str | None:
    """Extract the top-level ``model`` field from a JSON request body."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        model = data.get("model")
        if isinstance(model, str):
            return model
    return None


def _rewrite_model_field(body: bytes, new_model: str) -> bytes:
    """Rewrite the ``model`` field in a JSON request body and re-serialize."""
    data = json.loads(body)
    data["model"] = new_model
    return json.dumps(data).encode("utf-8")


async def _cancel_task(task: asyncio.Future[Any]) -> None:
    """Cancel a racing task and await it, swallowing the fallout."""
    if task.done():
        with contextlib.suppress(Exception, asyncio.CancelledError):
            task.result()
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def _parse_connection_headers(headers: list[tuple[bytes, bytes]]) -> set[str]:
    """Extract header names listed in Connection headers (RFC 7230 §6.1)."""
    extra: set[str] = set()
    for k, v in headers:
        if k.lower() == b"connection":
            for name in v.decode("latin-1").split(","):
                name = name.strip().lower()
                if name:
                    extra.add(name)
    return extra


_CONTROL_HEADERS = frozenset(
    {
        "x-switchboard-route-key",
        "x-switchboard-qos",
    }
)

_STRIP_REQUEST = _HOP_BY_HOP | _CONTROL_HEADERS | frozenset({"host"})

_CDN_HEADERS = frozenset(
    {
        "cf-ray",
        "x-amz-cf-id",
        "x-served-by",
        "x-fastly-request-id",
        "x-vercel-id",
        "fly-request-id",
    }
)
_CDN_SERVERS = frozenset({"cloudflare"})


def _classify_429(
    retry_after: str | None, headers: Mapping[str, str]
) -> str:
    """Classify a 429 as 'concurrency', 'rate_limit', or 'gateway'."""
    for cdn_header in _CDN_HEADERS:
        if headers.get(cdn_header) is not None:
            return "gateway"
    server = (headers.get("server") or "").lower()
    for cdn_server in _CDN_SERVERS:
        if cdn_server in server:
            return "gateway"

    if retry_after is None:
        return "concurrency"
    try:
        return "concurrency" if int(retry_after.strip()) <= 0 else "rate_limit"
    except (ValueError, TypeError):
        try:
            parsedate_to_datetime(retry_after.strip())
            return "rate_limit"
        except (ValueError, TypeError):
            return "concurrency"


@dataclass
class RoutingMetrics:
    """Per-provider routing counters surfaced in /status.json and /metrics.

    ``recent_decisions`` is a bounded ring buffer (WI-006.4): it holds at most
    ``_RECENT_DECISIONS_MAX`` entries.  When full, the oldest entry is evicted
    and ``evicted_decisions`` is incremented.  Never creates labels from
    arbitrary client-provided values — ``route_key_hash`` is truncated.
    """

    forwarded_per_provider: dict[str, int] = field(default_factory=dict)
    failovers: int = 0
    routing_decisions: int = 0
    recent_decisions: deque[dict[str, str]] = field(
        default_factory=lambda: deque(maxlen=_RECENT_DECISIONS_MAX)
    )
    evicted_decisions: int = 0

    def record_decision(
        self, route_key: str, selected: str, primary: str
    ) -> None:
        """Record a routing decision and whether it was a failover."""
        self.routing_decisions += 1
        if len(self.recent_decisions) == self.recent_decisions.maxlen:
            self.evicted_decisions += 1
        self.recent_decisions.append({
            "route_key_hash": route_key[:16] + "...",
            "selected": selected,
            "primary": primary,
        })
        if selected != primary:
            self.failovers += 1

    def record_forwarded(self, provider: str) -> None:
        """Record a successful forward to a provider."""
        self.forwarded_per_provider[provider] = (
            self.forwarded_per_provider.get(provider, 0) + 1
        )


def _extract_route_key(scope: Scope) -> str:
    """Extract the raw route key from the Authorization (Bearer) or x-api-key header."""
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for k, v in headers:
        if k == b"authorization":
            value = v.decode("latin-1").strip()
            if value.lower().startswith("bearer "):
                return value[7:].strip()
            continue
        if k == b"x-api-key":
            return v.decode("latin-1").strip()
    return ""


class ProxyApp:
    """ASGI multi-provider reverse proxy with pressure-based routing."""

    def __init__(
        self,
        *,
        providers: dict[str, ProviderContext],
        route_table: RouteTableManager,
        routing_config: RoutingConfig | None = None,
        admin_token: str | None = None,
        queue_timeout: float = _QUEUE_TIMEOUT_DEFAULT,
        drain_timeout: float = _DRAIN_TIMEOUT_DEFAULT,
        trusted_proxies: (
            frozenset[ipaddress.IPv4Network | ipaddress.IPv6Network] | None
        ) = None,
        max_request_body_bytes: int | None = None,
        upstream_idle_timeout: float | None = None,
        cors_allow_origin: str | None = None,
        overload_config: OverloadConfig | None = None,
        overload_statuses: frozenset[int] | None = None,
        model_map: ModelMap | None = None,
        estimator: ThresholdEstimator | None = None,
        budget_tracker: TokenBudgetTracker | None = None,
    ) -> None:
        self._providers = providers
        self._route_table = route_table
        self._routing_config = routing_config or RoutingConfig()
        self._admin_token = admin_token
        self._queue_timeout = queue_timeout
        self._drain_timeout = drain_timeout
        self._trusted_proxies = trusted_proxies or frozenset()
        self._max_request_body_bytes = max_request_body_bytes
        self._upstream_idle_timeout = upstream_idle_timeout
        self._cors_allow_origin = cors_allow_origin
        self._overload_tracker = OverloadTracker(overload_config)
        self._overload_statuses = (
            overload_statuses
            if overload_statuses is not None
            else _OVERLOAD_STATUSES_DEFAULT
        )
        self._model_map = model_map
        self._estimator = estimator
        self._budget_tracker = budget_tracker
        self._build_sha = os.environ.get("SWITCHBOARD_BUILD_SHA") or None
        self._login_throttle = LoginThrottle()
        self._metrics = RoutingMetrics()
        self._draining = False
        self._affinity: OrderedDict[str, RouteAffinity] = OrderedDict()

    @property
    def metrics(self) -> RoutingMetrics:
        """Routing metrics for /status.json and /metrics."""
        return self._metrics

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)
            return
        if scope["type"] != "http":
            return

        path = scope["path"]
        method = scope["method"]

        if path == "/healthz":
            await handle_healthz(send)
            return
        if path == "/readyz":
            await handle_readyz(send, self._providers)
            return

        if (
            method == "OPTIONS"
            and self._cors_allow_origin is not None
            and (
                path in (
                    "/", "/status.json", "/metrics",
                    "/admin/routes", "/admin/config",
                    "/login", "/logout",
                )
                or (
                    path.startswith("/admin/providers/")
                    and path.endswith("/override")
                )
            )
        ):
            await send_text(
                send, 204, "",
                extra_headers=cors_extra_headers(
                    self._cors_allow_origin, None
                ),
            )
            return

        if path.startswith("/static/"):
            await serve_static(path, send)
            return

        if path == "/login":
            if method == "GET":
                await handle_login_get(
                    send, self._admin_token, self._cors_allow_origin
                )
            elif method == "POST":
                await handle_login_post(
                    send, receive, self._admin_token, scope,
                    self._login_throttle, self._trusted_proxies,
                )
            else:
                await send_text(send, 405, "Method not allowed")
            return

        if path == "/logout":
            if method == "POST":
                await handle_logout(
                    send, self._admin_token, scope, self._trusted_proxies,
                )
            else:
                await send_text(send, 405, "Method not allowed")
            return

        if path in ("/", "/status.json", "/metrics"):
            authed = check_admin_auth(scope, self._admin_token)
            if not authed and path != "/":
                await send_json(
                    send, 401, {"error": "unauthorized"},
                    extra_headers=cors_extra_headers(
                        self._cors_allow_origin, None
                    ),
                )
                return
            if path == "/":
                if authed or not self._admin_token:
                    await send_dashboard(send, self._cors_allow_origin)
                else:
                    await send_login_page(send, self._cors_allow_origin)
                return
            if path == "/status.json":
                await send_status_json(
                    send, self._providers, self._route_table,
                    self._metrics, self._build_sha,
                    self._cors_allow_origin,
                    estimator=self._estimator,
                    overload_tracker=self._overload_tracker,
                    budget_tracker=self._budget_tracker,
                )
                return
            if path == "/metrics":
                await send_prometheus(
                    send, self._providers, self._metrics,
                    self._cors_allow_origin,
                    overload_tracker=self._overload_tracker,
                    budget_tracker=self._budget_tracker,
                )
                return

        if path == "/admin/routes":
            if method == "GET":
                authed = check_admin_auth(scope, self._admin_token)
                if not authed and self._admin_token:
                    await send_json(
                        send, 401, {"error": "unauthorized"},
                        extra_headers=cors_extra_headers(
                            self._cors_allow_origin, None
                        ),
                    )
                    return
                await handle_route_list(
                    send, self._route_table, self._cors_allow_origin,
                )
                return
            if method == "POST":
                await handle_route_add(
                    send, receive, self._route_table,
                    self._admin_token, scope, self._cors_allow_origin,
                    self._providers,
                )
                return
            await send_text(send, 405, "Method not allowed")
            return

        if path.startswith("/admin/routes/") and method == "DELETE":
            hashed_key = path[len("/admin/routes/"):]
            await handle_route_delete(
                send, self._route_table, self._admin_token,
                scope, hashed_key, self._cors_allow_origin,
            )
            return

        if path == "/admin/config" and method == "GET":
            authed = check_admin_auth(scope, self._admin_token)
            if not authed and self._admin_token:
                await send_json(
                    send, 401, {"error": "unauthorized"},
                    extra_headers=cors_extra_headers(
                        self._cors_allow_origin, None
                    ),
                )
                return
            await handle_config_get(
                send, self._routing_config, self._cors_allow_origin,
            )
            return

        if (
            path.startswith("/admin/providers/")
            and path.endswith("/override")
        ):
            from switchboard.admin import (
                handle_provider_override,
            )

            parts = path.split("/")
            if len(parts) == 5 and parts[4] == "override":
                prov_name = parts[3]
                await handle_provider_override(
                    send, receive, self._providers,
                    self._admin_token, scope, prov_name,
                    method, self._cors_allow_origin,
                )
                return

        await self._proxy_request(scope, receive, send)

    async def _handle_lifespan(self, receive: Receive, send: Send) -> None:
        """ASGI lifespan: start/stop all reconcile loops, drain, close clients."""
        prune_task: asyncio.Task[None] | None = None
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                for ctx in self._providers.values():
                    await ctx.reconcile.start()
                if self._budget_tracker is not None:
                    prune_task = asyncio.create_task(
                        self._budget_prune_loop()
                    )
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                self._draining = True
                if prune_task is not None:
                    prune_task.cancel()
                    with contextlib.suppress(
                        asyncio.CancelledError, Exception
                    ):
                        await prune_task
                for ctx in self._providers.values():
                    await ctx.reconcile.stop()
                if self._drain_timeout > 0:
                    deadline = (
                        asyncio.get_running_loop().time() + self._drain_timeout
                    )
                    while any(
                        ctx.gate.held > 0
                        for ctx in self._providers.values()
                    ):
                        remaining = (
                            deadline - asyncio.get_running_loop().time()
                        )
                        if remaining <= 0:
                            log.warning(
                                "shutdown: drain timeout — closing with "
                                "%d request(s) in-flight",
                                sum(
                                    ctx.gate.held
                                    for ctx in self._providers.values()
                                ),
                            )
                            break
                        await asyncio.sleep(
                            min(_DRAIN_POLL_INTERVAL, remaining)
                        )
                for ctx in self._providers.values():
                    await ctx.truth_source.close()
                for ctx in self._providers.values():
                    await ctx.http_client.aclose()
                self._route_table.close()
                if self._budget_tracker is not None:
                    self._budget_tracker.prune_all(now=time.monotonic())
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _budget_prune_loop(self) -> None:
        """Periodically prune the token-budget SQLite table (every 5 min)."""
        while True:
            try:
                await asyncio.sleep(300)
                if self._budget_tracker is not None:
                    self._budget_tracker.prune_all(now=time.monotonic())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("budget prune failed", exc_info=True)

    async def _proxy_request(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Route a request to the best available provider and stream it through."""
        if self._draining:
            await send_json(
                send, 503,
                {
                    "error": "draining",
                    "reason": "draining",
                    "retry_after": RETRY_AFTER_SHORT,
                },
                retry_after=RETRY_AFTER_SHORT,
            )
            return

        raw_key = _extract_route_key(scope)
        hashed_key = hash_route_key(raw_key)
        candidates = self._route_table.lookup(hashed_key)

        if not candidates:
            await send_json(
                send, 503,
                {
                    "error": "no providers configured",
                    "reason": "no_providers",
                    "retry_after": RETRY_AFTER_SHORT,
                },
                retry_after=RETRY_AFTER_SHORT,
            )
            return

        buffered_body: bytes | None = None
        request_model: str | None = None
        servable_providers: frozenset[str] | None = None

        if self._model_map is not None and self._model_map.routes:
            buffered_body, overflow = await self._buffer_request_body(receive)
            if overflow:
                await send_json(
                    send, 413, {"error": "request body too large"}
                )
                return
            if buffered_body is None:
                return
            request_model = _extract_model(buffered_body)
            if (
                request_model is not None
                and request_model in self._model_map
            ):
                servable_providers = self._model_map.providers_for(
                    request_model
                )

        now_mono = time.monotonic()

        if self._estimator is not None:
            est_provider = self._estimator.provider_name
            est_ctx = self._providers.get(est_provider)
            if est_ctx is not None:
                self._estimator.maybe_sample(est_ctx)

        # Reconcile token budgets with dashboard readings (Plan 012 §4.7).
        if self._budget_tracker is not None:
            for name in candidates:
                ctx = self._providers.get(name)
                if ctx is None:
                    continue
                cached = ctx.reconcile.last_reading
                if cached is not None and cached.ok:
                    reading = cached.reading
                    if (
                        reading.tokens_in is not None
                        and reading.tokens_out is not None
                    ):
                        self._budget_tracker.reconcile(
                            name,
                            reading.tokens_in + reading.tokens_out,
                            now=now_mono,
                        )

        states: dict[str, Any] = {}
        for name in candidates:
            ctx = self._providers.get(name)
            if ctx is not None:
                states[name] = snapshot_provider_state(
                    name, ctx, now=now_mono,
                    overload_tracker=self._overload_tracker,
                    budget_tracker=self._budget_tracker,
                )

        table = self._route_table.get_route_table()
        affinity = self._affinity.get(hashed_key)
        plan = route_decision(
            states, table, hashed_key, self._routing_config,
            now=now_mono,
            affinity=affinity,
            servable_providers=servable_providers,
        )
        primary = candidates[0]

        acquired_provider: str | None = None
        try:
            acquired_provider = await self._admit(
                plan, receive=receive, body_buffered=buffered_body is not None,
            )
        except Exception:
            log.exception("admission failed")
            await send_json(
                send, 503,
                {
                    "error": "admission failed",
                    "reason": "admission_error",
                    "retry_after": RETRY_AFTER_SHORT,
                },
                retry_after=RETRY_AFTER_SHORT,
            )
            return

        if acquired_provider is None:
            retry_after = RETRY_AFTER_SHORT
            reason = "no_capacity"
            ctx = self._providers.get(plan.terminal_fallback)
            if ctx is not None:
                if ctx.reconcile.is_low_interactivity():
                    reason = "low_interactivity"
                    cached = ctx.reconcile.last_reading
                    if cached is not None:
                        resets_at = (
                            cached.reading.service_mode_resets_at_epoch
                        )
                        if resets_at is not None and resets_at > 0:
                            remaining = int(resets_at - time.time())
                            retry_after = max(1, remaining)
                elif self._overload_tracker.is_cooling(
                    plan.terminal_fallback, now=time.monotonic()
                ):
                    reason = "overloaded"
                    retry_after = self._overload_tracker.cooldown_remaining(
                        plan.terminal_fallback, now=time.monotonic()
                    )
                else:
                    gate_reason = ctx.reconcile.gate_closed_reason()
                    if gate_reason == "boxed":
                        reason = "provider_boxed"
                        retry_after = ctx.reconcile.retry_after_seconds()
                    elif gate_reason == "breaker":
                        reason = "breaker_open"
                        retry_after = ctx.reconcile.retry_after_seconds()
                    elif gate_reason == "saturated":
                        reason = "saturated"
                        retry_after = ctx.reconcile.saturation_retry_after()
            await send_json(
                send, 503,
                {
                    "error": "concurrency limit reached",
                    "reason": reason,
                    "retry_after": retry_after,
                },
                retry_after=retry_after,
            )
            return

        if self._draining:
            ctx = self._providers[acquired_provider]
            await ctx.gate.release()
            await send_json(
                send, 503,
                {
                    "error": "draining",
                    "reason": "draining",
                    "retry_after": RETRY_AFTER_SHORT,
                },
                retry_after=RETRY_AFTER_SHORT,
            )
            return

        self._metrics.record_decision(hashed_key, acquired_provider, primary)

        if acquired_provider != primary:
            select_time = time.monotonic()
            self._affinity[hashed_key] = RouteAffinity(
                provider=acquired_provider,
                selected_at=select_time,
                failover_reason=plan.reason,
            )
            self._affinity.move_to_end(hashed_key)
            if len(self._affinity) > _AFFINITY_MAX:
                self._affinity.popitem(last=False)
        elif affinity is not None and affinity.provider != primary:
            self._affinity.pop(hashed_key, None)

        ctx = self._providers[acquired_provider]
        ctx.reconcile.record_request_forwarded()

        acquire_mono = time.monotonic()
        forward_failed = False
        try:
            await self._forward(
                ctx, scope, receive, send,
                buffered_body=buffered_body,
                request_model=request_model,
            )
            self._metrics.record_forwarded(acquired_provider)
        except Exception:
            forward_failed = True
            log.exception("proxy forward failed")
        finally:
            hold_seconds = time.monotonic() - acquire_mono
            await ctx.gate.release(
                hold_seconds=None if forward_failed else hold_seconds,
            )

        # Increment healthy observations on the affinity entry when a
        # failover provider served successfully (Plan 012 WI-C5).
        if (
            not forward_failed
            and acquired_provider != primary
            and hashed_key in self._affinity
        ):
            old = self._affinity[hashed_key]
            if old.provider == acquired_provider:
                self._affinity[hashed_key] = RouteAffinity(
                    provider=old.provider,
                    selected_at=old.selected_at,
                    failover_reason=old.failover_reason,
                    healthy_observations=old.healthy_observations + 1,
                )
                self._affinity.move_to_end(hashed_key)

    async def _admit(
        self,
        plan: AdmissionPlan,
        *,
        receive: Receive | None = None,
        body_buffered: bool = False,
    ) -> str | None:
        """Plan-driven admission (Plan 006 §4).

        1. Try each immediate candidate with a non-blocking gate acquire.
        2. If all immediate attempts lose the snapshot race, perform one
           final non-blocking retry pass over the same candidates.
        3. If still none, wait on queue_candidate for the remaining queue
           budget (not the full timeout).  If ``receive`` is provided AND
           the request body has already been buffered, race the wait against
           a client-disconnect check (Plan 012 WI-C3) so a disconnect during
           queue wait doesn't burn the full timeout.  When the body is NOT
           buffered, the disconnect watcher would steal body events from
           ``_forward``'s ``body_stream``, so the plain acquire is used.
        4. Return the acquired provider name, or None.
        """
        admit_start = time.monotonic()

        for name in plan.immediate_candidates:
            ctx = self._providers.get(name)
            if ctx is None:
                continue
            acquired = await ctx.gate.acquire(timeout=0.0)
            if acquired:
                return name

        for name in plan.immediate_candidates:
            ctx = self._providers.get(name)
            if ctx is None:
                continue
            reason = ctx.reconcile.gate_closed_reason()
            if reason in ("boxed", "breaker"):
                continue
            if not ctx.reconcile.ready:
                continue
            acquired = await ctx.gate.acquire(timeout=0.0)
            if acquired:
                return name

        if plan.queue_candidate is not None:
            elapsed = time.monotonic() - admit_start
            remaining = max(0.0, self._queue_timeout - elapsed)
            if remaining > 0:
                ctx = self._providers.get(plan.queue_candidate)
                if ctx is not None:
                    acquired = False
                    try:
                        if receive is not None and body_buffered:
                            acquired = await self._acquire_with_disconnect(
                                ctx, remaining, receive,
                            )
                        else:
                            acquired = await ctx.gate.acquire(
                                timeout=remaining
                            )
                    except BaseException:
                        if acquired:
                            await ctx.gate.release()
                        raise
                    if acquired:
                        return plan.queue_candidate

        return None

    async def _acquire_with_disconnect(
        self,
        ctx: ProviderContext,
        timeout: float,
        receive: Receive,
    ) -> bool:
        """Race a gate acquire against a client-disconnect check (Plan 012 WI-C3).

        If the client disconnects while waiting on the queue, aborts
        immediately without burning the full queue timeout.

        **Precondition**: the request body must already be buffered
        (``body_buffered=True``) — the watcher calls ``receive()`` which
        would otherwise steal body events from ``_forward``'s ``body_stream``.
        """
        disconnect_event = asyncio.Event()

        async def watch_disconnect() -> None:
            try:
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        disconnect_event.set()
                        return
                    if (
                        event["type"] == "http.request"
                        and not event.get("more_body", False)
                    ):
                        return
            except Exception:
                pass

        watcher = asyncio.create_task(watch_disconnect())
        acquire_task = asyncio.ensure_future(
            ctx.gate.acquire(timeout=timeout)
        )
        disc_task = asyncio.ensure_future(disconnect_event.wait())
        try:
            await asyncio.wait(
                {acquire_task, disc_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if acquire_task.done() and not acquire_task.cancelled():
                result = acquire_task.result()
                if result:
                    return True
            # Either disconnected or timed out — cancel the acquire.
            await _cancel_task(acquire_task)
            return False
        finally:
            await _cancel_task(disc_task)
            if not watcher.done():
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher

    async def _buffer_request_body(
        self, receive: Receive,
    ) -> tuple[bytes | None, bool]:
        """Buffer the request body from ASGI receive.

        Returns ``(body, overflow)``.  ``body`` is ``None`` on client
        disconnect; ``overflow`` is ``True`` when the body exceeds
        ``max_request_body_bytes``.
        """
        body = bytearray()
        limit = self._max_request_body_bytes
        while True:
            event = await receive()
            etype = event["type"]
            if etype == "http.disconnect":
                return None, False
            if etype == "http.request":
                data = event.get("body", b"")
                if data:
                    if limit is not None and len(body) + len(data) > limit:
                        return None, True
                    body.extend(data)
                if not event.get("more_body", False):
                    return bytes(body), False
            # ignore other event types

    async def _forward(
        self,
        ctx: ProviderContext,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        buffered_body: bytes | None = None,
        request_model: str | None = None,
    ) -> None:
        """Stream a request to the selected provider's upstream.

        Adapted from sluice's ``_forward()``: true streaming, disconnect
        detection, phantom prevention, hop-by-hop header stripping.

        When ``buffered_body`` is provided (model-map configured), the
        request body has already been consumed from ``receive``.  The
        ``model`` field may be rewritten for the fallback path (Plan 010
        Feature B); the primary path stays byte-transparent.
        """
        url = self._build_url(ctx, scope)
        headers = self._filter_request_headers(scope["headers"])
        method = scope["method"]

        disconnect = asyncio.Event()
        body_done = asyncio.Event()
        body_overflow = asyncio.Event()

        async def body_stream() -> AsyncIterator[bytes]:
            """Consume ASGI receive() directly — no intermediate queue."""
            seen = 0
            limit = self._max_request_body_bytes
            while True:
                event = await receive()
                etype = event["type"]
                if etype == "http.disconnect":
                    disconnect.set()
                    body_done.set()
                    return
                if etype == "http.request":
                    data = event.get("body", b"")
                    if data:
                        if limit is not None:
                            seen += len(data)
                            if seen > limit:
                                body_overflow.set()
                                body_done.set()
                                return
                        yield data
                    if not event.get("more_body", False):
                        body_done.set()
                        return

        async def disconnect_watcher() -> None:
            """Listen for client disconnect during the response phase."""
            await body_done.wait()
            if disconnect.is_set():
                return
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    disconnect.set()
                    return

        watcher_task = asyncio.create_task(disconnect_watcher())
        response_started = False

        try:
            if buffered_body is not None:
                content: Any = buffered_body
                if (
                    request_model is not None
                    and self._model_map is not None
                ):
                    alias = self._model_map.alias_for(
                        request_model, ctx.name
                    )
                    if alias is not None and alias != request_model:
                        content = _rewrite_model_field(
                            buffered_body, alias
                        )
                        headers = [
                            (k, v) for k, v in headers
                            if k.lower() != "content-length"
                        ]
                body_done.set()
            else:
                content = body_stream()

            stream_cm = ctx.http_client.stream(
                method, url, headers=headers, content=content
            )

            entry_task = asyncio.ensure_future(stream_cm.__aenter__())
            disconnect_task = asyncio.ensure_future(disconnect.wait())
            await asyncio.wait(
                [entry_task, disconnect_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if disconnect_task.done() and not entry_task.done():
                entry_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await entry_task
                with contextlib.suppress(Exception):
                    await stream_cm.__aexit__(None, None, None)
                return

            if body_overflow.is_set() and not response_started:
                if not entry_task.done():
                    entry_task.cancel()
                    with contextlib.suppress(
                        asyncio.CancelledError, Exception
                    ):
                        await entry_task
                if not disconnect_task.done():
                    disconnect_task.cancel()
                    with contextlib.suppress(
                        asyncio.CancelledError, Exception
                    ):
                        await disconnect_task
                with contextlib.suppress(Exception):
                    await stream_cm.__aexit__(None, None, None)
                await send_json(send, 413, {"error": "request body too large"})
                return

            if not disconnect_task.done():
                disconnect_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await disconnect_task

            response = entry_task.result()

            try:
                if response.status_code == 429:
                    retry_after_raw = response.headers.get("retry-after")
                    classification = _classify_429(
                        retry_after_raw, response.headers
                    )
                    log.warning(
                        "upstream 429: retry_after=%r classification=%s",
                        retry_after_raw,
                        classification,
                    )
                    if classification == "concurrency":
                        ctx.reconcile.record_429()
                    elif classification == "rate_limit":
                        ctx.reconcile.record_rate_limit_429()
                    elif classification == "gateway":
                        ctx.reconcile.record_gateway_429()
                    else:
                        ctx.reconcile.record_429()

                if response.status_code in self._overload_statuses:
                    retry_after_val = _parse_retry_after_seconds(
                        response.headers.get("retry-after")
                    )
                    self._overload_tracker.record_overloaded(
                        ctx.name,
                        now=time.monotonic(),
                        retry_after=retry_after_val,
                    )
                else:
                    self._overload_tracker.record_ok(ctx.name)

                ctx.reconcile.record_response_headers(
                    dict(response.headers), response.status_code
                )

                if disconnect.is_set():
                    return

                try:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": response.status_code,
                            "headers": self._encode_response_headers(
                                response
                            ),
                        }
                    )
                    response_started = True
                except Exception:
                    disconnect.set()
                    return

                idle = self._upstream_idle_timeout
                chunk_iter = response.aiter_raw()
                upstream_idle = False
                disc_wait = asyncio.ensure_future(disconnect.wait())

                # Token-budget observer (Plan 012 Feature B): read-only
                # in-flight usage parsing.  Only instantiated when a budget
                # is configured for this provider and the response is 2xx.
                # Bytes forwarded to the client are never modified.
                observer: UsageObserver | None = None
                non_sse_buf: bytearray | None = None
                if (
                    self._budget_tracker is not None
                    and self._budget_tracker.has_budget(ctx.name)
                    and 200 <= response.status_code < 300
                ):
                    content_type = (
                        response.headers.get("content-type", "")
                        .lower()
                    )
                    is_sse = "text/event-stream" in content_type
                    observer = UsageObserver(is_sse=is_sse)
                    if not is_sse:
                        non_sse_buf = bytearray()

                try:
                    while True:
                        if disconnect.is_set():
                            break
                        read_task = asyncio.ensure_future(
                            chunk_iter.__anext__()
                        )
                        done, _pending = await asyncio.wait(
                            {read_task, disc_wait},
                            timeout=idle,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done:
                            await _cancel_task(read_task)
                            log.warning(
                                "upstream idle timeout (%.1fs) — aborting",
                                idle,
                            )
                            upstream_idle = True
                            break
                        if read_task not in done:
                            await _cancel_task(read_task)
                            break
                        try:
                            chunk = read_task.result()
                        except StopAsyncIteration:
                            break
                        if disconnect.is_set():
                            break
                        # Feed to the read-only observer BEFORE forwarding
                        # — bytes sent to the client are unchanged.
                        if observer is not None:
                            observer.feed_chunk(chunk)
                        if (
                            non_sse_buf is not None
                            and len(non_sse_buf) < 1_048_576
                        ):
                            non_sse_buf.extend(chunk)
                        try:
                            await send(
                                {
                                    "type": "http.response.body",
                                    "body": chunk,
                                    "more_body": True,
                                }
                            )
                        except Exception:
                            disconnect.set()
                            break
                finally:
                    await _cancel_task(disc_wait)

                if not disconnect.is_set():
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"",
                            "more_body": False,
                        }
                    )
                    if (
                        200 <= response.status_code < 400
                        and not upstream_idle
                    ):
                        ctx.reconcile.record_success()

                    # Record observed usage into the budget tracker
                    # (Plan 012 Feature B — read-only, best-effort).
                    if (
                        observer is not None
                        and self._budget_tracker is not None
                        and not disconnect.is_set()
                    ):
                        if non_sse_buf is not None:
                            observer.feed_non_streaming(
                                bytes(non_sse_buf)
                            )
                        usage = observer.usage
                        if usage is not None:
                            self._budget_tracker.record_usage(
                                ctx.name,
                                usage[0],
                                usage[1],
                                now=time.monotonic(),
                            )
            finally:
                with contextlib.suppress(Exception):
                    await stream_cm.__aexit__(None, None, None)

        except httpx.RequestError as exc:
            if not disconnect.is_set() and not response_started:
                log.warning(
                    "upstream error: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                with contextlib.suppress(Exception):
                    await send_json(send, 502, {"error": "upstream error"})
            elif response_started and not disconnect.is_set():
                with contextlib.suppress(Exception):
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"",
                            "more_body": False,
                        }
                    )
        finally:
            if not watcher_task.done():
                watcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher_task

    def _build_url(self, ctx: ProviderContext, scope: Scope) -> str:
        """Build the upstream URL from the provider's base URL + path + query."""
        upstream = ctx.upstream_url.rstrip("/")
        path: str = scope["path"]
        qs: bytes = scope.get("query_string", b"")
        if qs:
            path += "?" + qs.decode("latin-1")
        return upstream + path

    def _filter_request_headers(
        self, scope_headers: list[tuple[bytes, bytes]]
    ) -> list[tuple[str, str]]:
        """Strip hop-by-hop, switchboard-internal, and admin auth headers."""
        connection_hop_by_hop = _parse_connection_headers(scope_headers)
        strip_set = _STRIP_REQUEST | connection_hop_by_hop

        result: list[tuple[str, str]] = []
        for k, v in scope_headers:
            name = k.decode("latin-1").lower()
            if name in strip_set:
                continue
            if name.startswith("x-switchboard-"):
                continue
            if name.startswith("x-sluice-"):
                continue
            if name == "authorization" and is_admin_auth_value(
                v, self._admin_token
            ):
                continue
            if name == "cookie":
                cookie_str = v.decode("latin-1")
                parts = [p.strip() for p in cookie_str.split(";")]
                filtered = [
                    p
                    for p in parts
                    if p and not p.startswith(f"{SESSION_COOKIE}=")
                ]
                if len(filtered) < len([p for p in parts if p]):
                    if filtered:
                        result.append(
                            (k.decode("latin-1"), "; ".join(filtered))
                        )
                    continue
            result.append((k.decode("latin-1"), v.decode("latin-1")))
        return result

    @staticmethod
    def _encode_response_headers(
        response: httpx.Response,
    ) -> list[tuple[bytes, bytes]]:
        """Strip hop-by-hop headers from the upstream response."""
        connection_hop_by_hop = _parse_connection_headers(
            [
                (k.encode("latin-1"), v.encode("latin-1"))
                for k, v in response.headers.items()
            ]
        )
        strip_set = _HOP_BY_HOP | connection_hop_by_hop
        return [
            (k.encode("latin-1"), v.encode("latin-1", errors="replace"))
            for k, v in response.headers.items()
            if k.lower() not in strip_set
            and not (
                k.lower() == "set-cookie"
                and v.strip().lower().startswith(f"{SESSION_COOKIE}=")
            )
        ]

