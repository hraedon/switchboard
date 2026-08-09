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
import math
import os
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from switchboard.admin import (
    handle_config_effective,
    handle_config_get,
    handle_config_reset,
    handle_healthz,
    handle_login_get,
    handle_login_post,
    handle_logout,
    handle_model_map_delete,
    handle_model_map_list,
    handle_model_map_set,
    handle_preview_path,
    handle_provider_create,
    handle_provider_delete,
    handle_provider_discover,
    handle_provider_registry,
    handle_provider_test,
    handle_provider_update,
    handle_providers_list,
    handle_quarantine_list,
    handle_quarantine_release,
    handle_readyz,
    handle_route_add,
    handle_route_default_set,
    handle_route_delete,
    handle_route_list,
    handle_routing_config_update,
    handle_threshold_events,
    handle_usage_history,
    send_dashboard,
    send_login_page,
    send_prometheus,
    send_status_json,
    serve_static,
)
from switchboard.config_store import ConfigStoreManager
from switchboard.control import (
    DEFAULT_REROUTE_STATUSES,
    AdmissionPlan,
    Availability,
    ModelMap,
    RouteAffinity,
    RoutingConfig,
    SignalFreshness,
    classify_failure,
    compose_upstream_path,
    extract_conversation_fingerprint,
    hash_route_key,
    route_decision,
    should_reroute,
)
from switchboard.estimator import ThresholdEstimator
from switchboard.limit import RETRY_AFTER_SHORT
from switchboard.model_map import ModelMapManager
from switchboard.overload import OverloadConfig, OverloadTracker
from switchboard.provider_manager import ProviderManager
from switchboard.providers import ProviderContext, snapshot_provider_state
from switchboard.quarantine import QuarantineTracker
from switchboard.route_table import RouteTableManager
from switchboard.session import (
    SESSION_COOKIE,
    LoginThrottle,
)
from switchboard.speed import SpeedSampler
from switchboard.token_budget import TokenBudgetTracker
from switchboard.usage_history import UsageHistoryTracker
from switchboard.usage_observer import UsageObserver
from switchboard.utils import (
    check_admin_auth,
    cors_extra_headers,
    is_admin_auth_value,
    send_json,
    send_text,
)

log = logging.getLogger("switchboard.proxy")

Scope = dict[str, Any]
Send = Any
Receive = Any


_DRAIN_POLL_INTERVAL = 0.1
_QUEUE_TIMEOUT_DEFAULT = 30.0
_DRAIN_TIMEOUT_DEFAULT = 25.0
_RECENT_DECISIONS_MAX = 128
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


#: Inbound headers that may carry a caller's credential. All are stripped
#: before a provider's own credential is applied, so no client- or
#: other-vendor-issued key can ride along to an upstream that did not issue it.
_CREDENTIAL_HEADERS = frozenset(
    {"authorization", "x-api-key", "api-key", "x-goog-api-key"}
)


@dataclass
class _RerouteProbe:
    """Carries a usage-error verdict out of ``_forward`` without unwinding it.

    ``_forward`` has many early-return paths; threading a return type through
    all of them would be a large, risky edit to the streaming core for a
    feature that only ever fires before the first byte reaches the client.
    The probe is passed in, ``_forward`` stamps it and returns early *without
    sending anything*, and the caller decides whether to retry elsewhere or
    surface the error. ``armed`` is False for every attempt that cannot be
    retried, which makes the whole feature inert by default.
    """

    armed: bool = False
    triggered: bool = False
    status: int | None = None
    retry_after: float | None = None
    #: Response headers and the opening bytes of the body, captured only for
    #: non-2xx. Quarantine attribution (Plan 023) needs both to tell "the
    #: vendor refused our credential" from "an edge blocked the caller".
    headers: dict[str, str] = field(default_factory=dict)
    body_prefix: str = ""


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
    affinity_pins_total: int = 0
    affinity_failbacks_total: int = 0
    affinity_evictions_total: int = 0
    usage_reroutes_total: int = 0
    usage_reroutes_from: dict[str, int] = field(default_factory=dict)
    usage_giveups_total: int = 0

    def record_affinity_pin(self) -> None:
        """Record a new affinity pin (non-primary acquisition)."""
        self.affinity_pins_total += 1

    def record_affinity_failback(self) -> None:
        """Record a return to the primary that popped an affinity pin."""
        self.affinity_failbacks_total += 1

    def record_affinity_eviction(self) -> None:
        """Record an LRU eviction of an affinity entry (pin loss)."""
        self.affinity_evictions_total += 1

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

    def record_usage_reroute(self, from_provider: str, to_provider: str) -> None:
        """Record a request moved off a provider that returned a usage error.

        Counted by ORIGIN: the useful operational question is "who is running
        out", and the destination is already visible in forwarded_per_provider.
        """
        self.usage_reroutes_total += 1
        self.usage_reroutes_from[from_provider] = (
            self.usage_reroutes_from.get(from_provider, 0) + 1
        )

    def record_usage_giveup(self) -> None:
        """Record a give-up: a usage error with no eligible provider left.

        The one condition routing cannot fix — every eligible provider
        answered with a usage error, so the upstream's error is surfaced to
        the client.  Distinct from ``usage_reroutes_total``, which counts the
        reroutes that actually moved a request.
        """
        self.usage_giveups_total += 1

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
        model_map_mgr: ModelMapManager | None = None,
        estimator: ThresholdEstimator | None = None,
        budget_tracker: TokenBudgetTracker | None = None,
        usage_history_tracker: UsageHistoryTracker | None = None,
        speed_sampler: SpeedSampler | None = None,
        quarantine: QuarantineTracker | None = None,
        reroute_statuses: frozenset[int] | None = None,
        reroute_max_attempts: int = 0,
        config_store: ConfigStoreManager | None = None,
        toml_provider_names: frozenset[str] | None = None,
        toml_provider_sections: dict[str, dict[str, Any]] | None = None,
        env_field_sources: dict[str, dict[str, str]] | None = None,
        unmatched_env: list[str] | None = None,
        route_key_secrets: tuple[str, ...] = (),
    ) -> None:
        self._provider_manager = ProviderManager(
            providers, drain_timeout=drain_timeout
        )
        # Provider config store (Plan 020 WI-3/4). A memory-only store when
        # the caller passes none keeps the admin endpoints functional (their
        # writes simply don't survive a restart). The boot TOML sections are
        # kept for the tombstone/effective-config paths ONLY — they may hold
        # an inline api_key, and every serialization path masks them.
        self._config_store = (
            config_store if config_store is not None else ConfigStoreManager()
        )
        self._toml_provider_names = toml_provider_names or frozenset()
        self._toml_provider_sections = toml_provider_sections or {}
        # Per-field env provenance (Plan 021 D6), computed once at boot: env
        # cannot change under a running process, so recomputing per request
        # would only invite drift between what routing uses and what the
        # config surface reports.
        self._env_field_sources = env_field_sources or {}
        self._unmatched_env = unmatched_env or []
        self._route_table = route_table
        self._routing_config = routing_config or RoutingConfig()
        # Route-key HMAC secrets (Plan 008 §3), ordered current-first then any
        # previous secret for the rotation dual-read window. Empty tuple =
        # plain SHA-256 (the pre-HMAC behaviour, full backward compat). The
        # first element is the "current" secret used to hash newly-added keys.
        self._route_key_secrets = tuple(route_key_secrets)
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
        self._model_map_mgr = model_map_mgr or ModelMapManager()
        # Usage-error reroute. OFF by default: enabling it requires buffering
        # the request body so a retry can replay it, which changes request
        # streaming semantics and costs memory. An operator opts in per
        # deployment; unconfigured switchboard behaves exactly as before.
        self._reroute_statuses = (
            reroute_statuses
            if reroute_statuses is not None
            else DEFAULT_REROUTE_STATUSES
        )
        self._reroute_max_attempts = max(0, int(reroute_max_attempts))
        self._estimator = estimator
        self._budget_tracker = budget_tracker
        self._speed_sampler = speed_sampler
        #: Plan 023. None disables the feature entirely.
        self._quarantine = quarantine
        self._usage_history_tracker = usage_history_tracker
        self._build_sha = os.environ.get("SWITCHBOARD_BUILD_SHA") or None
        self._login_throttle = LoginThrottle()
        self._metrics = RoutingMetrics()
        self._draining = False
        self._affinity: OrderedDict[str, RouteAffinity] = OrderedDict()
        self._provider_healthy_since: dict[str, float] = {}

    @property
    def _providers(self) -> dict[str, ProviderContext]:
        """Snapshot of the live provider map (owned by the manager).

        Copy-on-swap semantics: this reference is replaced, never mutated,
        so any admission/status path that grabs it works on a consistent
        view even while providers are added or removed mid-request.
        """
        return self._provider_manager.providers

    def _match_route(self, raw_key: str) -> tuple[tuple[str, ...], str]:
        """Resolve ``(providers, matched_hashed_key)`` for a raw API key under
        HMAC rotation.

        Tries each active route-key secret in order (current, then any
        previous secret for the dual-read window) against the keyed entries;
        the first keyed hit wins and its digest is returned. With no keyed
        match under any secret the request falls through to the default route
        (Plan 008 §3) and the primary-secret digest is returned. With no
        secrets configured this is plain SHA-256 lookup — byte-for-byte the
        pre-HMAC behaviour, so an unconfigured deployment is unchanged.

        The matched digest (not always the current-secret one) is what
        ``route_decision`` re-resolves the entry by, so it must be the digest
        that actually hit — otherwise a legacy entry matched under the
        previous secret would be re-resolved under the current secret, miss,
        and silently fall back to the default route mid-rotation.
        """
        for secret in self._route_key_secrets:
            h = hash_route_key(raw_key, secret)
            keyed = self._route_table.get_entry(h)
            if keyed is not None:
                return keyed, h
        primary = self._route_key_secrets[0] if self._route_key_secrets else None
        h = hash_route_key(raw_key, primary)
        return self._route_table.lookup(h), h

    @property
    def provider_manager(self) -> ProviderManager:
        """Runtime provider lifecycle (Plan 020 WI-2); admin handlers use this."""
        return self._provider_manager

    @property
    def metrics(self) -> RoutingMetrics:
        """Routing metrics for /status.json and /metrics."""
        return self._metrics

    @property
    def routing_config(self) -> RoutingConfig:
        """The current routing config (Plan 020 WI-14 runtime swap)."""
        return self._routing_config

    @property
    def config_store(self) -> ConfigStoreManager:
        """The config store, where a routing change is persisted."""
        return self._config_store

    def update_routing_config(self, config: RoutingConfig) -> None:
        """Swap the routing config at runtime (Plan 020 WI-14).

        Replaces the frozen ``RoutingConfig`` wholesale — the proxy reads it
        per-request so the change takes effect on the next routing decision.
        Strategy, pace knobs, and dwell/failback intervals are all mutable;
        ``affinity_max_entries`` is NOT (resizing the live table would evict
        active pins) — pass the same value back.

        ``quarantine_threshold`` lives on this config but is *held* by the
        quarantine tracker, so it is pushed across here. Without that the knob
        was mutable in name only: the PUT was accepted and persisted, the
        tracker kept counting to the old number, and the change appeared at the
        next restart instead.
        """
        self._routing_config = config
        if self._quarantine is not None:
            self._quarantine.set_threshold(config.quarantine_threshold)

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
                    "/admin/config/effective", "/admin/config/reset",
                    "/admin/config/routing",
                    "/admin/model-map", "/admin/providers",
                    "/admin/preview-path",
                    "/admin/threshold-events", "/admin/usage-history",
                    "/login", "/logout",
                )
                or (
                    path == "/admin/routes/default"
                )
                or (
                    path.startswith("/admin/providers/")
                )
                or (
                    path.startswith("/admin/model-map/")
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
                    usage_history_tracker=self._usage_history_tracker,
                    model_map_mgr=self._model_map_mgr,
                    speed_sampler=self._speed_sampler,
                    routing_config=self._routing_config,
                    quarantine=self._quarantine,
                )
                return
            if path == "/metrics":
                await send_prometheus(
                    send, self._providers, self._metrics,
                    self._cors_allow_origin,
                    overload_tracker=self._overload_tracker,
                    budget_tracker=self._budget_tracker,
                    usage_history_tracker=self._usage_history_tracker,
                    estimator=self._estimator,
                    speed_sampler=self._speed_sampler,
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
                    self._route_key_secrets[0] if self._route_key_secrets else None,
                )
                return
            await send_text(send, 405, "Method not allowed")
            return

        # MUST precede the generic /admin/routes/<key> branch below: "default"
        # is not a hashed key, and letting DELETE fall through would answer
        # "route not found" for an entry that cannot be deleted at all.
        if path == "/admin/routes/default":
            if method == "PUT":
                await handle_route_default_set(
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

        if path == "/admin/model-map":
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
                await handle_model_map_list(
                    send, self._model_map_mgr, self._cors_allow_origin,
                    self._providers,
                )
                return
            if method == "POST":
                await handle_model_map_set(
                    send, receive, self._model_map_mgr,
                    self._admin_token, scope, self._cors_allow_origin,
                    self._providers,
                )
                return
            await send_text(send, 405, "Method not allowed")
            return

        if path.startswith("/admin/model-map/") and method == "DELETE":
            model_name = path[len("/admin/model-map/"):]
            from urllib.parse import unquote

            model_name = unquote(model_name)
            await handle_model_map_delete(
                send, self._model_map_mgr, self._admin_token,
                scope, model_name, self._cors_allow_origin,
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

        if path == "/admin/config/reset":
            if method == "POST":
                await handle_config_reset(
                    send, receive, self._route_table,
                    self._admin_token, scope, self._cors_allow_origin,
                )
                return
            await send_text(send, 405, "Method not allowed")
            return

        if path == "/admin/quarantine" and method == "GET":
            await handle_quarantine_list(
                send, self._quarantine, self._admin_token, scope,
                self._cors_allow_origin,
            )
            return

        if path.startswith("/admin/quarantine/") and method == "DELETE":
            from urllib.parse import unquote

            rest = path[len("/admin/quarantine/"):]
            # provider/model — the model may itself contain slashes
            # ("vendor/name-v2"), so split once from the left only.
            provider, _, model = rest.partition("/")
            if not provider or not model:
                await send_json(
                    send, 400,
                    {"error": "expected /admin/quarantine/<provider>/<model>"},
                    extra_headers=cors_extra_headers(
                        self._cors_allow_origin, None
                    ),
                )
                return
            await handle_quarantine_release(
                send, self._quarantine, self._admin_token, scope,
                unquote(provider), unquote(model), self._cors_allow_origin,
            )
            return

        if path == "/admin/config/routing" and method == "PUT":
            await handle_routing_config_update(
                send, receive, self,
                self._admin_token, scope, self._cors_allow_origin,
            )
            return

        if path == "/admin/config/effective" and method == "GET":
            authed = check_admin_auth(scope, self._admin_token)
            if not authed and self._admin_token:
                await send_json(
                    send, 401, {"error": "unauthorized"},
                    extra_headers=cors_extra_headers(
                        self._cors_allow_origin, None
                    ),
                )
                return
            await handle_config_effective(
                send, self._config_store,
                self._toml_provider_names, self._toml_provider_sections,
                self._cors_allow_origin,
                self._env_field_sources, self._unmatched_env,
            )
            return

        if path == "/admin/providers":
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
                await handle_providers_list(
                    send, self._providers, self._config_store,
                    self._cors_allow_origin,
                    toml_provider_sections=self._toml_provider_sections,
                )
                return
            if method == "POST":
                await handle_provider_create(
                    send, receive, self._provider_manager,
                    self._config_store, self._admin_token, scope,
                    self._cors_allow_origin,
                )
                return
            await send_text(send, 405, "Method not allowed")
            return

        # Plan 021 Wave 2: registry + discovery. MUST precede the generic
        # /admin/providers/<name> branch below, or "registry"/"discover" would
        # be treated as a provider name.
        if path == "/admin/providers/registry" and method == "GET":
            authed = check_admin_auth(scope, self._admin_token)
            if not authed and self._admin_token:
                await send_json(
                    send, 401, {"error": "unauthorized"},
                    extra_headers=cors_extra_headers(
                        self._cors_allow_origin, None
                    ),
                )
                return
            await handle_provider_registry(
                send, self._cors_allow_origin,
            )
            return

        if path == "/admin/providers/discover" and method == "POST":
            await handle_provider_discover(
                send, receive, self._admin_token, scope,
                self._cors_allow_origin,
            )
            return

        if path == "/admin/preview-path" and method == "GET":
            authed = check_admin_auth(scope, self._admin_token)
            if not authed and self._admin_token:
                await send_json(
                    send, 401, {"error": "unauthorized"},
                    extra_headers=cors_extra_headers(
                        self._cors_allow_origin, None
                    ),
                )
                return
            await handle_preview_path(
                send, scope, self._cors_allow_origin,
            )
            return

        if path == "/admin/usage-history" and method == "GET":
            await handle_usage_history(
                send, scope, self._admin_token, self._providers,
                self._cors_allow_origin,
            )
            return

        if path == "/admin/threshold-events" and method == "GET":
            await handle_threshold_events(
                send, scope, self._admin_token, self._estimator,
                self._cors_allow_origin,
            )
            return

        if (
            path.startswith("/admin/providers/")
            and path.endswith("/override")
        ):
            from urllib.parse import unquote

            from switchboard.admin import (
                handle_provider_override,
            )

            parts = path.split("/")
            if len(parts) == 5 and parts[4] == "override":
                prov_name = unquote(parts[3])
                await handle_provider_override(
                    send, receive, self._providers,
                    self._admin_token, scope, prov_name,
                    method, self._cors_allow_origin,
                )
                return

        if path.startswith("/admin/providers/"):
            from urllib.parse import unquote

            parts = path.split("/")
            if len(parts) == 5 and parts[4] == "test":
                if method == "POST":
                    await handle_provider_test(
                        send, self._providers, self._admin_token,
                        scope, unquote(parts[3]), self._cors_allow_origin,
                    )
                    return
                await send_text(send, 405, "Method not allowed")
                return
            if len(parts) == 4 and parts[3]:
                prov_name = unquote(parts[3])
                if method == "PUT":
                    await handle_provider_update(
                        send, receive, self._provider_manager,
                        self._config_store, self._admin_token, scope,
                        prov_name, self._cors_allow_origin,
                    )
                    return
                if method == "DELETE":
                    await handle_provider_delete(
                        send, self._provider_manager, self._config_store,
                        self._admin_token, scope, prov_name,
                        self._toml_provider_names,
                        self._toml_provider_sections,
                        self._cors_allow_origin,
                    )
                    return
                await send_text(send, 405, "Method not allowed")
                return

        await self._proxy_request(scope, receive, send)

    async def _handle_lifespan(self, receive: Receive, send: Send) -> None:
        """ASGI lifespan: start/stop all reconcile loops, drain, close clients."""
        prune_task: asyncio.Task[None] | None = None
        usage_history_task: asyncio.Task[None] | None = None
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                for ctx in self._providers.values():
                    await ctx.reconcile.start()
                if self._budget_tracker is not None:
                    prune_task = asyncio.create_task(
                        self._budget_prune_loop()
                    )
                if self._usage_history_tracker is not None:
                    usage_history_task = asyncio.create_task(
                        self._usage_history_loop()
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
                if usage_history_task is not None:
                    usage_history_task.cancel()
                    with contextlib.suppress(
                        asyncio.CancelledError, Exception
                    ):
                        await usage_history_task
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
                # Contexts removed/replaced at runtime drain on their own
                # tasks; settle them so exit never abandons a half-closed
                # context (Plan 020 WI-2).
                await self._provider_manager.shutdown()
                self._route_table.close()
                if self._budget_tracker is not None:
                    self._budget_tracker.prune_all(now=time.monotonic())
                if self._usage_history_tracker is not None:
                    await self._usage_history_tracker.close()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _budget_prune_loop(self) -> None:
        """Periodically prune the token-budget SQLite table (every 5 min)."""
        while True:
            try:
                await asyncio.sleep(300)
                if self._budget_tracker is not None:
                    self._budget_tracker.prune_all(now=time.monotonic())
                if self._estimator is not None:
                    self._estimator.prune_events()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("budget prune failed", exc_info=True)

    async def _usage_history_loop(self) -> None:
        """Periodically refresh usage-history token counts.

        Ticks every 10 s so penalty transitions are picked up promptly; the
        tracker itself throttles successful refreshes to 5 min and failed
        attempts to 60 s, so the fast tick costs nothing when healthy.
        """
        while True:
            try:
                await asyncio.sleep(10)
                if self._usage_history_tracker is not None:
                    for name, ctx in self._providers.items():
                        if not self._usage_history_tracker.has_provider(name):
                            continue
                        penalty_at = ctx.reconcile.penalty_started_at
                        await self._usage_history_tracker.refresh(
                            name,
                            penalty_started_at=penalty_at,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("usage-history refresh failed", exc_info=True)

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
        # _match_route returns the providers AND the digest under which a
        # keyed entry was actually found (current secret, or the previous
        # secret during the rotation dual-read window). route_decision
        # re-resolves the entry by that digest, so it must be the matched
        # one — not always the current-secret hash.
        candidates, hashed_key = self._match_route(raw_key)

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

        # Snapshot the model map once per request so a mid-request admin edit
        # cannot splice two mappings together.  Empty map = feature off.
        model_map = self._model_map_mgr.get_model_map()
        has_model_map = bool(model_map.routes)

        # Buffer the body when any feature needs it: model rewriting reads it,
        # usage-error reroute must replay it, and conversation pinning reads
        # the first user message for a fingerprint (Plan 019 §6).
        pin_conversations = self._routing_config.pin_conversations
        if has_model_map or self._reroute_max_attempts > 0 or pin_conversations:
            buffered_body, overflow = await self._buffer_request_body(receive)
            if overflow:
                await send_json(
                    send, 413, {"error": "request body too large"}
                )
                return
            if buffered_body is None:
                return
            if has_model_map:
                request_model = _extract_model(buffered_body)
            if (
                request_model is not None
                and model_map is not None
                and request_model in model_map
            ):
                servable_providers = model_map.providers_for(
                    request_model
                )

        # Affinity key: the conversation fingerprint when pinning is opt-in
        # (Plan 019 §6.4), else the API-key hash.  route_key (hashed_key)
        # stays the API-key hash for table lookup + metrics — never mix the
        # two (review finding 12).
        if pin_conversations and buffered_body is not None:
            fingerprint = extract_conversation_fingerprint(buffered_body)
            affinity_key: str = fingerprint or hashed_key
        else:
            affinity_key = hashed_key

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
                    usage_history_tracker=self._usage_history_tracker,
                )

        primary = candidates[0]

        # Track a continuous-healthy clock per provider, not just the
        # configured primary: when a model map excludes the original primary,
        # route_decision re-derives the effective primary and needs *that*
        # provider's clock for the failback-hysteresis check (Plan 014).
        for cand in candidates:
            cst = states.get(cand)
            cand_healthy = (
                cst is not None
                and cst.signal_freshness is SignalFreshness.FRESH
                and cst.availability is Availability.AVAILABLE
            )
            if cand_healthy:
                self._provider_healthy_since.setdefault(cand, now_mono)
            else:
                self._provider_healthy_since.pop(cand, None)

        # Quarantine (Plan 023): drop (provider, model) pairs a human has not
        # cleared. Applied per model, so a pair broken for one model costs the
        # provider nothing for the others it serves.
        if self._quarantine is not None and request_model is not None:
            blocked = self._quarantine.quarantined_for(request_model)
            if blocked:
                base = (
                    servable_providers
                    if servable_providers is not None
                    else frozenset(candidates)
                )
                servable_providers = base - frozenset(blocked)
                if not servable_providers:
                    # Every provider for this model is quarantined. Say so
                    # rather than returning a bare 503 that reads like quota
                    # exhaustion — the operator needs to know a human decision
                    # is what unblocks this, and which pairs to release.
                    pairs = [f"{p}/{request_model}" for p in blocked]
                    log.warning(
                        "all providers for model '%s' are quarantined: %s",
                        request_model, ", ".join(pairs),
                    )
                    await send_json(
                        send, 503,
                        {
                            "error": (
                                f"every provider for model "
                                f"'{request_model}' is quarantined after "
                                f"repeated failures; release one to resume"
                            ),
                            "reason": "quarantined",
                            "quarantined": pairs,
                            "release_with": (
                                f"DELETE /admin/quarantine/<provider>/"
                                f"{request_model}"
                            ),
                        },
                    )
                    return

        table = self._route_table.get_route_table()
        affinity = self._affinity.get(affinity_key)
        plan = route_decision(
            states, table, hashed_key, self._routing_config,
            now=now_mono,
            affinity=affinity,
            servable_providers=servable_providers,
            healthy_since=dict(self._provider_healthy_since),
        )

        admitted: tuple[str, ProviderContext] | None = None
        try:
            admitted = await self._admit(
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

        if admitted is None:
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

        # From here on, the provider identity is the (name, ctx) pair the
        # permit was acquired on — never a fresh map lookup. The map is
        # copy-on-swap and may have changed during the admission awaits;
        # re-indexing it here KeyErrors on a removed provider and, worse,
        # releases the WRONG gate on a replaced one (wave 0+1 review,
        # blocking finding 1).
        acquired_provider, acquired_ctx = admitted

        if self._draining:
            await acquired_ctx.gate.release()
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
            self._affinity[affinity_key] = RouteAffinity(
                provider=acquired_provider,
                selected_at=select_time,
                failover_reason=plan.reason,
            )
            self._affinity.move_to_end(affinity_key)
            self._evict_affinity()
            self._metrics.record_affinity_pin()
        elif pin_conversations and affinity is None:
            # Conversation pinning (Plan 019 §6.4): pin the FIRST request to
            # whichever provider served it — including the primary — so the
            # conversation stays there until the provider drops.
            select_time = time.monotonic()
            self._affinity[affinity_key] = RouteAffinity(
                provider=acquired_provider,
                selected_at=select_time,
                failover_reason=plan.reason,
            )
            self._affinity.move_to_end(affinity_key)
            self._evict_affinity()
        elif affinity is not None and affinity.provider != primary:
            if self._affinity.pop(affinity_key, None) is not None:
                self._metrics.record_affinity_failback()

        # Usage-error reroute loop (Plan 010, reactive half). One pass per
        # attempt: forward, and if the upstream answered with an exhaustion
        # status before any byte reached the client, hand the request to a
        # different provider. Each attempt owns its own permit — the failed
        # provider's is released before the next is acquired, so a reroute
        # never holds two gates at once.
        tried: set[str] = set()
        tried_statuses: dict[str, int] = {}
        rerouted_to: str | None = None
        probe = _RerouteProbe()
        reroutes_done = 0

        while True:
            ctx = acquired_ctx
            ctx.reconcile.record_request_forwarded()
            tried.add(acquired_provider)

            # Arm only when a retry could actually happen: the body must be
            # replayable, the budget unspent, and somebody else must be able
            # to serve. Unarmed means `_forward` behaves exactly as before.
            # Include the queue candidate: `_admit(exclude=...)` will happily
            # wait on it, so treating only immediate candidates as
            # alternatives would abandon a provider that could still serve.
            alternatives = [
                name
                for name in (
                    *plan.immediate_candidates,
                    *(
                        (plan.queue_candidate,)
                        if plan.queue_candidate is not None
                        else ()
                    ),
                )
                if name not in tried and name in self._providers
            ]
            probe.armed = (
                self._reroute_max_attempts > 0
                and buffered_body is not None
                and reroutes_done < self._reroute_max_attempts
                and bool(alternatives)
            )
            probe.triggered = False
            probe.status = None
            probe.headers = {}
            probe.body_prefix = ""

            acquire_mono = time.monotonic()
            forward_failed = False
            try:
                await self._forward(
                    ctx, scope, receive, send,
                    buffered_body=buffered_body,
                    request_model=request_model,
                    model_map=model_map,
                    probe=probe,
                )
                if not probe.triggered:
                    self._metrics.record_forwarded(acquired_provider)
            except Exception:
                forward_failed = True
                log.exception("proxy forward failed")
            finally:
                hold_seconds = time.monotonic() - acquire_mono
                await ctx.gate.release(
                    hold_seconds=None if forward_failed else hold_seconds,
                )

            # Quarantine bookkeeping (Plan 023). Only failures attributable
            # to the PROVIDER count; a caller-caused failure would reproduce
            # on every provider, so counting it would walk the estate into
            # quarantine one provider at a time.
            if self._quarantine is not None and request_model is not None:
                if forward_failed:
                    attribution = classify_failure(None)
                    status_for_q: int | None = None
                    detail = "forward failed"
                else:
                    status_for_q = probe.status
                    attribution = classify_failure(
                        status_for_q, probe.headers, probe.body_prefix,
                    )
                    detail = (
                        (probe.headers.get("content-type") or "")
                        if status_for_q and status_for_q >= 400 else ""
                    )
                self._quarantine.record(
                    acquired_provider, request_model, attribution,
                    status=status_for_q, detail=detail,
                )

            if not forward_failed and probe.status is not None:
                tried_statuses[acquired_provider] = probe.status

            if not probe.triggered:
                served = (
                    not forward_failed
                    and probe.status is not None
                    and probe.status not in self._reroute_statuses
                )
                if rerouted_to is not None and served and rerouted_to != primary:
                    # This attempt served. Re-pin so later requests in the
                    # conversation go straight here instead of repaying the
                    # exhausted provider's failed round trip.
                    self._affinity[affinity_key] = RouteAffinity(
                        provider=rerouted_to,
                        selected_at=time.monotonic(),
                        failover_reason="usage_error_reroute",
                    )
                    self._affinity.move_to_end(affinity_key)
                    self._evict_affinity()
                    self._metrics.record_affinity_pin()
                if (
                    not forward_failed
                    and self._reroute_max_attempts > 0
                    and probe.status is not None
                    and probe.status in self._reroute_statuses
                ):
                    # Terminal attempt: the probe was never armed because the
                    # retry budget was spent or no alternative remained, so
                    # the upstream's response passes through untouched. This
                    # is still a give-up — every eligible provider returned a
                    # usage error.
                    self._record_usage_give_up(tried, tried_statuses)
                break

            if not should_reroute(
                status=probe.status or 0,
                reroute_statuses=self._reroute_statuses,
                reroutes_done=reroutes_done,
                max_attempts=self._reroute_max_attempts,
                body_replayable=buffered_body is not None,
                response_started=False,
                alternatives_remain=bool(alternatives),
            ):
                self._record_usage_give_up(tried, tried_statuses)
                await self._send_usage_error(send, probe)
                return

            next_admitted = await self._admit(
                plan,
                receive=receive,
                body_buffered=True,
                exclude=frozenset(tried),
            )
            if next_admitted is None:
                # Nobody else could take it: surface the upstream's own
                # status so the client's backoff still sees the truth.
                self._record_usage_give_up(tried, tried_statuses)
                await self._send_usage_error(send, probe)
                return
            next_provider, next_ctx = next_admitted
            log.info(
                "rerouting after usage error: %s -> %s (status=%s)",
                acquired_provider,
                next_provider,
                probe.status,
            )
            self._metrics.record_usage_reroute(acquired_provider, next_provider)
            reroutes_done += 1
            acquired_provider = next_provider
            acquired_ctx = next_ctx
            # Affinity is NOT written here. A pin must record who actually
            # served, not who was merely selected: cancellation, a forwarding
            # failure, or another usage error would otherwise leave the
            # conversation pinned to a provider that never answered — the same
            # stale-pin problem in a new place. The write happens after the
            # loop, once an attempt has succeeded.
            rerouted_to = next_provider

        # Increment healthy observations on the affinity entry when a
        # failover provider served successfully (Plan 012 WI-C5).
        if (
            not forward_failed
            and acquired_provider != primary
            and affinity_key in self._affinity
        ):
            old = self._affinity[affinity_key]
            if old.provider == acquired_provider:
                self._affinity[affinity_key] = RouteAffinity(
                    provider=old.provider,
                    selected_at=old.selected_at,
                    failover_reason=old.failover_reason,
                    healthy_observations=old.healthy_observations + 1,
                )
                self._affinity.move_to_end(affinity_key)

    def _record_usage_give_up(
        self, tried: set[str], statuses: Mapping[str, int]
    ) -> None:
        """Record a give-up: a usage error with nowhere left to route.

        Every provider on this request's path answered with a usage error, so
        the reroute has nothing left to try and the upstream's error reaches
        the client.  This is the one exhaustion condition routing cannot fix,
        which is exactly why it must be observable: it is counted and logged
        at WARNING naming the providers tried and the status each returned,
        so the line alone is actionable.
        """
        self._metrics.record_usage_giveup()
        detail = ", ".join(
            f"{name}={statuses[name]}" for name in sorted(tried)
        )
        log.warning(
            "usage-error give-up: every eligible provider returned a usage "
            "error — providers tried: %s",
            detail,
        )

    async def _send_usage_error(
        self, send: Send, probe: _RerouteProbe
    ) -> None:
        """Answer a request whose upstream response was already closed.

        Narrow by design. When the retry budget is spent or no alternative
        exists, the probe is never armed and the upstream's own response —
        status, headers and body — streams through untouched, exactly as it
        would without this feature. This path is only for the case where
        switchboard *had* armed the probe, closed an exhausted upstream's
        response, and then failed to admit anywhere else: there is no longer a
        body to relay, so it synthesises one under the upstream's status and
        Retry-After, keeping the client's backoff honest.
        """
        status = probe.status or 503
        # Retry-After is integer seconds on the wire (RFC 7231); round a
        # fractional upstream value up so the client never retries early.
        retry_after = (
            math.ceil(probe.retry_after)
            if probe.retry_after is not None
            else RETRY_AFTER_SHORT
        )
        await send_json(
            send,
            status,
            {
                "error": "all eligible providers returned a usage error",
                "reason": "usage_error_exhausted",
                "upstream_status": status,
                "retry_after": retry_after,
            },
            retry_after=retry_after,
        )

    async def _admit(
        self,
        plan: AdmissionPlan,
        *,
        receive: Receive | None = None,
        body_buffered: bool = False,
        exclude: frozenset[str] = frozenset(),
    ) -> tuple[str, ProviderContext] | None:
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
        4. Return the ``(name, context)`` pair the permit was acquired on,
           or None. Returning the CONTEXT and not just the name is
           load-bearing: the provider map is copy-on-swap and may have
           changed during the queue-wait awaits above — the caller must
           forward through, and release on, the exact context whose gate
           granted the permit, never a fresh map lookup (wave 0+1 review,
           blocking finding 1).
        """
        admit_start = time.monotonic()

        for name in plan.immediate_candidates:
            if name in exclude:
                continue
            ctx = self._providers.get(name)
            if ctx is None:
                continue
            acquired = await ctx.gate.acquire(timeout=0.0)
            if acquired:
                return name, ctx

        for name in plan.immediate_candidates:
            if name in exclude:
                continue
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
                return name, ctx

        if plan.queue_candidate is not None and plan.queue_candidate not in exclude:
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
                        return plan.queue_candidate, ctx

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
        # True once the permit belongs to the CALLER. Until then this function
        # owns it and must reclaim it on any exit; afterwards it must not
        # touch it — releasing a transferred permit hands the caller a permit
        # it does not hold, and its own later release then decrements some
        # OTHER request's permit. That corruption is worse than the leak this
        # bookkeeping exists to prevent.
        transferred = False
        try:
            await asyncio.wait(
                {acquire_task, disc_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Disconnect wins a tie. When both complete together, treating the
            # acquisition as authoritative consumes the disconnect silently and
            # lets the caller forward — or retry — on behalf of a client that
            # has already gone.
            if disc_task.done() and not disc_task.cancelled():
                await _cancel_task(acquire_task)
                return False
            if acquire_task.done() and not acquire_task.cancelled():
                result = acquire_task.result()
                if result:
                    transferred = True
                    return True
            # Either disconnected or timed out — cancel the acquire.
            await _cancel_task(acquire_task)
            return False
        finally:
            # Cancellation of the enclosing request unwinds through the await
            # above without touching acquire_task, which could then win the
            # race and hold a permit nobody is left to release — capacity the
            # gate never gets back. Reclaim it, but only while it is still ours.
            if not transferred:
                if not acquire_task.done():
                    await _cancel_task(acquire_task)
                elif not acquire_task.cancelled():
                    with contextlib.suppress(Exception):
                        if acquire_task.result():
                            await ctx.gate.release()
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
        model_map: ModelMap | None = None,
        probe: _RerouteProbe | None = None,
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

        headers = self._apply_provider_credential(ctx, headers)

        try:
            if buffered_body is not None:
                content: Any = buffered_body
                if (
                    request_model is not None
                    and model_map is not None
                ):
                    alias = model_map.alias_for(
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

            # Speed statistics (Plan 020 Wave 3): request-open timestamp for
            # TTFB/duration. Timing only — no body content read for this.
            req_start = time.monotonic()

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
            ttfb_ms = (time.monotonic() - req_start) * 1000.0

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
                    dict(response.headers),
                    response.status_code,
                    now_monotonic=time.monotonic(),
                )

                # Usage-error reroute (Plan 010, reactive half). Nothing has
                # been sent to the client yet, so the request is still free to
                # be served by somebody else. Stamp the probe and return
                # WITHOUT starting the response; the caller retries elsewhere
                # or surfaces the error. Closing the response releases the
                # upstream connection rather than leaking it into the pool.
                # Disconnect first: a client that has gone away must not
                # cause switchboard to open a *second* upstream request on its
                # behalf. Rerouting a dead request is the phantom-request bug
                # the streaming core exists to prevent.
                if disconnect.is_set():
                    return

                # Record the status unconditionally: even when no reroute is
                # possible the caller needs to know whether this attempt
                # actually SERVED, so it does not pin affinity to a provider
                # that merely handed back an exhaustion response.
                if probe is not None:
                    probe.status = response.status_code
                    if response.status_code >= 400:
                        probe.headers = {
                            k.lower(): v for k, v in response.headers.items()
                        }
                if (
                    probe is not None
                    and probe.armed
                    and response.status_code in self._reroute_statuses
                ):
                    probe.triggered = True
                    probe.retry_after = _parse_retry_after_seconds(
                        response.headers.get("retry-after")
                    )
                    log.info(
                        "usage error from %s: status=%s — rerouting",
                        ctx.name,
                        response.status_code,
                    )
                    with contextlib.suppress(Exception):
                        await response.aclose()
                    return

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

                    # Speed statistics (Plan 020 Wave 3): record TTFB +
                    # duration for every successful, fully-served response.
                    # Completion tokens ride along only when the opt-in usage
                    # observer already parsed them — no extra body reading.
                    if (
                        self._speed_sampler is not None
                        and 200 <= response.status_code < 300
                        and not upstream_idle
                        and not disconnect.is_set()
                    ):
                        comp_tokens: int | None = None
                        if observer is not None:
                            ou = observer.usage
                            comp_tokens = ou[1] if ou is not None else None
                        self._speed_sampler.record(
                            ctx.name,
                            ttfb_ms=ttfb_ms,
                            duration_ms=(time.monotonic() - req_start)
                            * 1000.0,
                            completion_tokens=comp_tokens,
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

    def _evict_affinity(self) -> None:
        """Evict the oldest affinity entry if the LRU table exceeds its
        configured bound, counting the eviction so operators can detect
        pin loss (Plan 019 §6.6)."""
        bound = self._routing_config.affinity_max_entries
        if len(self._affinity) > bound:
            self._affinity.popitem(last=False)
            self._metrics.record_affinity_eviction()

    def _build_url(self, ctx: ProviderContext, scope: Scope) -> str:
        """Build the upstream URL from the provider's base URL + path + query.

        Composition (not concatenation) since Plan 021: the provider base
        declares the API version when it carries one, so a client sending the
        conventional ``/v1/...`` is not forced to drop it. See
        :func:`switchboard.control.compose_upstream_path`.

        Called once per attempt by the reroute loop, so a request that moves
        to another provider composes against THAT provider's base — the whole
        point, since the two rarely share a path shape.
        """
        path: str = scope["path"]
        qs: bytes = scope.get("query_string", b"")
        if qs:
            path += "?" + qs.decode("latin-1")
        return compose_upstream_path(ctx.upstream_url, path)

    @staticmethod
    def _apply_provider_credential(
        ctx: ProviderContext, headers: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """Present this provider's own credential instead of the client's.

        Cross-vendor failover is impossible without it: every provider issues
        its own key, so a request rerouted from one to another would arrive
        with a credential the new upstream has never seen and be rejected —
        turning "your provider is out of quota" into "401", which is worse
        than the problem the reroute exists to solve.

        This narrows byte-identical egress by exactly one header, and only for
        providers that configure a key. With none configured the client's
        headers pass through untouched, so a single-vendor deployment keeps
        full cache-transparency.
        """
        if not ctx.api_key:
            return headers
        # Strip EVERY credential header, not just the one this provider uses.
        # Removing only `ctx.auth_header` leaks across header styles: a client
        # sending `Authorization` to a provider that wants `x-api-key` would
        # have its Authorization forwarded intact — one vendor's key handed to
        # another, which is the exact leak this function exists to prevent.
        value = f"{ctx.auth_prefix}{ctx.api_key}"
        out = [
            (k, v) for k, v in headers if k.lower() not in _CREDENTIAL_HEADERS
        ]
        out.append((ctx.auth_header, value))
        return out

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

