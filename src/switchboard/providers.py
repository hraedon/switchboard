"""Provider context: gate + reconcile + truth source per upstream.

Each :class:`ProviderContext` bundles the vendored building blocks
(:class:`~switchboard.gate.PermitGate`,
:class:`~switchboard.reconcile.ReconciliationLoop`,
:class:`~switchboard.truth.TruthSource`) for one upstream provider, plus an
:class:`httpx.AsyncClient` for forwarding.  The factory functions construct
these from a provider type and optional TOML config;
:func:`snapshot_provider_state` bridges the live shell state into the pure
:class:`~switchboard.control.ProviderState` used by the routing core.
"""

from __future__ import annotations

import os
import time as _time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import httpx

from switchboard.control import Availability, ProviderState, SignalFreshness
from switchboard.dashboard import DashboardTruthSource
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.peak import PeakWindow, parse_peak_windows
from switchboard.peak import in_peak as _peak_in_peak
from switchboard.reconcile import ReconciliationLoop
from switchboard.truth import (
    Provider,
    TruthSource,
    get_provider,
    make_truth_source,
)

if TYPE_CHECKING:
    from switchboard.history import History, HistoryStore
    from switchboard.overload import OverloadTracker
    from switchboard.token_budget import TokenBudgetTracker
    from switchboard.usage_history import UsageHistoryTracker


@dataclass
class ProviderContext:
    """One upstream provider: gate + reconcile + truth source + HTTP client.

    ``usage_base_url`` / ``usage_api_key`` / ``usage_auth_header`` are set
    for umans-type providers that expose ``/v1/usage/history``.  They enable
    the ``/admin/usage-history`` endpoint and the background
    :class:`~switchboard.usage_history.UsageHistoryTracker`.
    """

    name: str
    upstream_url: str
    gate: PermitGate
    reconcile: ReconciliationLoop
    truth_source: TruthSource
    http_client: httpx.AsyncClient
    usage_base_url: str | None = None
    usage_api_key: str = ""
    usage_auth_header: str = "authorization"
    #: Credential to present to THIS upstream, replacing whatever the client
    #: sent. Required for cross-vendor failover: each provider issues its own
    #: key, so forwarding the client's verbatim would 401 the moment a request
    #: moves. Empty means passthrough (byte-identical egress preserved).
    api_key: str = ""
    #: Header the upstream expects the credential in. OpenAI-compatible
    #: gateways use `authorization: Bearer <key>`; some want `x-api-key`.
    auth_header: str = "authorization"
    #: Prefix applied to the credential — "Bearer " for authorization,
    #: usually empty for x-api-key.
    auth_prefix: str = "Bearer "
    #: Parsed peak-pricing windows (Plan 025). Empty = no peak pricing.
    #: snapshot_provider_state evaluates these against the wall clock to set
    #: ProviderState.in_peak; the routing core never reads a clock.
    peak_windows: tuple[PeakWindow, ...] = ()


_UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)


def build_provider_context(
    name: str,
    upstream_url: str,
    provider_type: str,
    target: int,
    *,
    usage_key: str = "",
    auth_header: str | None = None,
    fresh_ttl: float = 15.0,
    poll_interval: float = 5.0,
    dashboard_url: str | None = None,
    dashboard_token: str | None = None,
    dashboard_provider: str | None = None,
    dashboard_poll_interval: float = 30.0,
    dashboard_stale_ttl: float = 900.0,
    poll_interval_idle: float | None = None,
    history_store: HistoryStore | None = None,
    history: History | None = None,
    api_key: str = "",
    auth_header_name: str = "authorization",
    auth_prefix: str | None = None,
    direct_usage_enabled: bool = False,
    direct_usage_workspace_id: str = "",
    direct_usage_cookie: str = "",
    direct_usage_stale_ttl: float = 900.0,
    direct_usage_poll_interval: float = 30.0,
    peak_windows: tuple[PeakWindow, ...] = (),
) -> ProviderContext:
    """Construct a :class:`ProviderContext` from the vendored building blocks.

    Resolves the provider type via :func:`switchboard.truth.get_provider`,
    constructs the appropriate :class:`~switchboard.truth.TruthSource`, creates
    a :class:`~switchboard.gate.PermitGate` (initial capacity 0 — the reconcile
    loop resizes it), and wires a :class:`~switchboard.reconcile.ReconciliationLoop`
    sized by the provider's target concurrency.

    Truth source precedence, first match wins:

    1. ``direct_usage_enabled`` — fetch usage from the provider's own surface,
       no usage-dashboard required (Plan 022). Only provider types with a
       fetcher and configured credentials are affected; anything else falls
       through rather than failing, so a half-configured provider loses its
       weekly signal instead of the boot.
    2. ``dashboard_url`` + ``dashboard_token`` — the usage-dashboard
       ``/readings`` API.
    3. The provider-type default (``PolledTruthSource`` for umans,
       ``HeaderTruthSource`` for anthropic/openai, ``NullTruthSource`` for
       generic).
    """
    # Imported here rather than at module scope: direct_usage pulls in httpx
    # and is optional, and providers.py is imported by paths that never touch
    # a network.
    from switchboard.direct_usage import (
        DirectUsageTruthSource,
        make_direct_fetcher,
    )

    provider: Provider = get_provider(provider_type)

    truth_source: TruthSource | None = None

    if direct_usage_enabled:
        fetcher = make_direct_fetcher(
            provider_type,
            api_key=api_key,
            usage_key=usage_key,
            workspace_id=direct_usage_workspace_id,
            cookie=direct_usage_cookie,
            name=name,
        )
        if fetcher is not None:
            truth_source = DirectUsageTruthSource(
                fetcher,
                stale_ttl=direct_usage_stale_ttl,
                poll_interval=direct_usage_poll_interval,
            )
            poll_interval = direct_usage_poll_interval

    if (
        truth_source is None
        and dashboard_url is not None
        and dashboard_token is not None
    ):
        # The dashboard names providers by its own ids ("zai", "ollama",
        # "opencode", ...), which rarely match switchboard's route keys
        # ("zai-coding-plan", "ollama-cloud", "opencode-go"). Without the
        # dashboard_provider mapping, the lookup silently never matched and
        # the truth source served the fail-safe forever.
        truth_source = DashboardTruthSource(
            dashboard_url=dashboard_url,
            bearer_token=dashboard_token,
            provider_name=dashboard_provider or name,
            stale_ttl=dashboard_stale_ttl,
        )
        poll_interval = dashboard_poll_interval

    if truth_source is None:
        truth_source = make_truth_source(
            provider,
            base_url=upstream_url,
            api_key=usage_key,
            auth_header=auth_header,
            fresh_ttl=fresh_ttl,
        )

    gate = PermitGate(initial_capacity=0)

    reconcile = ReconciliationLoop(
        truth_source=truth_source,
        gate=gate,
        max_concurrency=target,
        poll_interval=poll_interval,
        breaker_config=BreakerConfig(),
        provider_type=provider_type,
        fresh_ttl=fresh_ttl,
        poll_interval_idle=poll_interval_idle,
        history=history,
        history_store=history_store,
    )

    http_client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT)

    usage_base_url: str | None = None
    usage_api_key = ""
    usage_auth_header = provider.auth_header
    if provider.needs_usage_key and usage_key:
        usage_base_url = upstream_url
        usage_api_key = usage_key

    return ProviderContext(
        name=name,
        upstream_url=upstream_url,
        gate=gate,
        reconcile=reconcile,
        truth_source=truth_source,
        http_client=http_client,
        usage_base_url=usage_base_url,
        usage_api_key=usage_api_key,
        usage_auth_header=usage_auth_header,
        api_key=api_key,
        auth_header=auth_header_name,
        # Derive rather than default: a caller constructing a context directly
        # for an x-api-key gateway should not have to know that "Bearer " is
        # wrong for it.
        auth_prefix=(
            auth_prefix
            if auth_prefix is not None
            else ("Bearer " if auth_header_name.lower() == "authorization" else "")
        ),
        peak_windows=peak_windows,
    )


def build_provider_contexts_from_config(
    config: dict[str, object],
    *,
    history_store_path: str | None = None,
) -> dict[str, ProviderContext]:
    """Build a ``dict`` of :class:`ProviderContext` instances from a parsed TOML config.

    Iterates over the ``[provider.*]`` sections, resolves environment
    variables (``usage_key_env``, ``dashboard_token_env``), and calls
    :func:`build_provider_context` for each provider.

    When ``history_store_path`` is provided, each provider gets a per-provider
    :class:`~switchboard.history.SQLiteHistoryStore` (separate file per
    provider to avoid table-name conflicts) and a bounded in-memory
    :class:`~switchboard.history.History` ring — giving trend analysis that
    survives restarts (Plan 012 WI-2).
    """
    raw_providers = config.get("provider")
    if not isinstance(raw_providers, dict):
        return {}

    contexts: dict[str, ProviderContext] = {}
    for name, raw in raw_providers.items():
        if not isinstance(raw, dict):
            continue
        provider_cfg = cast(dict[str, object], raw)

        upstream = _str_or(provider_cfg, "upstream", "")
        provider_type = _str_or(provider_cfg, "type", "generic")
        target = _int_or(provider_cfg, "target", 3)

        # Upstream credential. Env indirection is the norm here so keys never
        # land in a committed config; an inline `api_key` stays available for
        # throwaway local runs.
        api_key = _str_or(provider_cfg, "api_key", "")
        api_key_env = _optional_str(provider_cfg, "api_key_env")
        if api_key_env:
            api_key = os.environ.get(api_key_env, "")
            if not api_key:
                # Fail closed. Falling back to passthrough would forward the
                # CLIENT's credential to this upstream — silently recreating
                # the cross-provider leak that per-provider credentials exist
                # to prevent, from nothing worse than a typo in a Secret.
                raise ValueError(
                    f"provider '{name}': api_key_env={api_key_env!r} is not "
                    "set or is empty; refusing to start rather than fall back "
                    "to forwarding the client's credential"
                )
        auth_header_name = _str_or(provider_cfg, "auth_header", "authorization")
        auth_prefix = _str_or(
            provider_cfg,
            "auth_prefix",
            "Bearer " if auth_header_name.lower() == "authorization" else "",
        )

        usage_key = ""
        usage_key_env = _optional_str(provider_cfg, "usage_key_env")
        if usage_key_env is not None:
            usage_key = os.environ.get(usage_key_env, "")

        dashboard_url = _optional_str(provider_cfg, "dashboard_url")
        # Optional mapping to the dashboard's provider id when it differs
        # from this provider's name (e.g. "opencode-go" reads the dashboard's
        # "opencode" reading). Defaults to the provider name.
        dashboard_provider = _optional_str(provider_cfg, "dashboard_provider")

        dashboard_token: str | None = None
        dashboard_token_env = _optional_str(provider_cfg, "dashboard_token_env")
        if dashboard_token_env is not None:
            dashboard_token = os.environ.get(dashboard_token_env)

        dashboard_stale_ttl = _float_or(
            provider_cfg, "dashboard_stale_ttl", 900.0
        )
        dashboard_poll_interval = _float_or(
            provider_cfg, "dashboard_poll_interval", 30.0
        )

        # Direct usage solicitation (Plan 022): fetch usage from the provider's
        # own surface instead of a usage-dashboard. Opt-in per provider; types
        # without a fetcher fall through to the default truth source. The
        # cookie is env-only — it is a session credential for a whole account
        # and must not sit in a config file that gets committed.
        direct_usage_enabled = bool(provider_cfg.get("direct_usage", False))
        direct_usage_workspace_id = _str_or(
            provider_cfg, "direct_usage_workspace_id", ""
        )
        direct_usage_workspace_id_env = _optional_str(
            provider_cfg, "direct_usage_workspace_id_env"
        )
        if direct_usage_workspace_id_env:
            direct_usage_workspace_id = os.environ.get(
                direct_usage_workspace_id_env, ""
            )
        direct_usage_cookie_env = _optional_str(
            provider_cfg, "direct_usage_cookie_env"
        )
        direct_usage_cookie = (
            os.environ.get(direct_usage_cookie_env, "")
            if direct_usage_cookie_env
            else ""
        )
        direct_usage_stale_ttl = _float_or(
            provider_cfg, "direct_usage_stale_ttl", 900.0
        )
        direct_usage_poll_interval = _float_or(
            provider_cfg, "direct_usage_poll_interval", 30.0
        )

        poll_interval_idle = _optional_float(provider_cfg, "poll_interval_idle")

        # Peak-pricing windows (Plan 025). A bad spec fails construction with
        # the provider and spec named — a window that silently didn't parse
        # would mean burning quota at the expensive rate with no sign why.
        peak_windows: tuple[PeakWindow, ...] = ()
        raw_windows = provider_cfg.get("peak_windows")
        if raw_windows is not None:
            if not isinstance(raw_windows, list) or not all(
                isinstance(w, str) for w in raw_windows
            ):
                raise ValueError(
                    f"provider '{name}': peak_windows must be a list of "
                    "window strings, e.g. [\"mon-fri 14:00-18:00 +08:00\"]"
                )
            try:
                peak_windows = parse_peak_windows(raw_windows)
            except ValueError as exc:
                raise ValueError(f"provider '{name}': {exc}") from None

        history_store: HistoryStore | None = None
        history: History | None = None
        if history_store_path:
            from switchboard.history import History as HistoryRing
            from switchboard.history import SQLiteHistoryStore

            store_file = f"{history_store_path}.{_safe_filename(name)}.history"
            history_store = SQLiteHistoryStore(store_file)
            history = HistoryRing(maxlen=2880)
            # Warm the ring from the store so the trend surface has data
            # immediately after a restart — otherwise it stays empty for
            # hours while the ring refills at poll cadence.
            for entry in history_store.load_recent(2880):
                history.append(entry)

        contexts[name] = build_provider_context(
            name=name,
            upstream_url=upstream,
            provider_type=provider_type,
            target=target,
            usage_key=usage_key,
            dashboard_url=dashboard_url,
            dashboard_token=dashboard_token,
            dashboard_provider=dashboard_provider,
            dashboard_stale_ttl=dashboard_stale_ttl,
            dashboard_poll_interval=dashboard_poll_interval,
            poll_interval_idle=poll_interval_idle,
            history_store=history_store,
            history=history,
            api_key=api_key,
            auth_header_name=auth_header_name,
            auth_prefix=auth_prefix,
            direct_usage_enabled=direct_usage_enabled,
            direct_usage_workspace_id=direct_usage_workspace_id,
            direct_usage_cookie=direct_usage_cookie,
            direct_usage_stale_ttl=direct_usage_stale_ttl,
            direct_usage_poll_interval=direct_usage_poll_interval,
            peak_windows=peak_windows,
        )

    return contexts


def snapshot_provider_state(
    name: str,
    ctx: ProviderContext,
    *,
    now: float,
    overload_tracker: OverloadTracker | None = None,
    budget_tracker: TokenBudgetTracker | None = None,
    usage_history_tracker: UsageHistoryTracker | None = None,
) -> ProviderState:
    """Read live state from a provider's reconcile loop and gate.

    Pure: takes the context as argument, reads its current state, and returns
    a frozen :class:`~switchboard.control.ProviderState`. No I/O.

    Derives :class:`~switchboard.control.Availability` and
    :class:`~switchboard.control.SignalFreshness` from the reconcile loop's
    live state (Plan 006 §3, extended by Plan 010):

    * **Availability**:
      - ``CLOSED`` if the reconcile loop reports low-interactivity (Plan 010 Feature 0,
        proactive), or the overload tracker is cooling (Plan 010 Feature A,
        reactive), or gate_closed_reason is boxed or breaker.
      - ``UNKNOWN`` if not ready (first poll not complete).
      - ``BUSY`` if gate_closed_reason is saturated, or available_permits == 0,
        or queue_depth > 0 (WI-006.1: live saturation from held permits).
      - ``AVAILABLE`` otherwise.
    * **SignalFreshness**:
      - ``UNKNOWN`` if not ready.
      - ``DEGRADED`` if ready but last fetch failed (stale last-known-good).
      - ``FRESH`` if ready and last fetch succeeded.
    """
    gate_reason = ctx.reconcile.gate_closed_reason()
    ready = ctx.reconcile.ready
    last_fetch_ok = ctx.reconcile.last_fetch_ok

    low_interactivity = ctx.reconcile.is_low_interactivity()
    overload_cooling = (
        overload_tracker is not None
        and overload_tracker.is_cooling(name, now=now)
    )

    if low_interactivity or overload_cooling:
        availability = Availability.CLOSED
    elif not ready:
        availability = Availability.UNKNOWN
    elif gate_reason in ("boxed", "breaker"):
        availability = Availability.CLOSED
    elif (
        gate_reason == "saturated"
        or ctx.gate.available == 0
        or ctx.gate.queue_depth > 0
    ):
        availability = Availability.BUSY
    else:
        availability = Availability.AVAILABLE

    if not ready:
        freshness = SignalFreshness.UNKNOWN
    elif last_fetch_ok:
        freshness = SignalFreshness.FRESH
    else:
        freshness = SignalFreshness.DEGRADED

    retry_after: int | None = None
    if availability == Availability.CLOSED:
        if low_interactivity:
            cached = ctx.reconcile.last_reading
            if cached is not None:
                resets_at = cached.reading.service_mode_resets_at_epoch
                if resets_at is not None and resets_at > 0:
                    remaining = int(resets_at - _time.time())
                    retry_after = max(1, remaining)
            if retry_after is None:
                retry_after = ctx.reconcile.retry_after_seconds()
        elif overload_cooling and overload_tracker is not None:
            retry_after = overload_tracker.cooldown_remaining(name, now=now)
        else:
            retry_after = ctx.reconcile.retry_after_seconds()
    elif availability == Availability.BUSY:
        retry_after = ctx.reconcile.saturation_retry_after()

    usage_headroom: float | None = None
    quota_resets_in: float | None = None
    cached_reading = ctx.reconcile.last_reading
    if cached_reading is not None and cached_reading.ok:
        limit_state = cached_reading.reading
        if (
            limit_state.requests_remaining is not None
            and limit_state.requests_limit is not None
            and limit_state.requests_limit > 0
        ):
            usage_headroom = (
                limit_state.requests_remaining / limit_state.requests_limit
            )
        bre = limit_state.bucket_reset_epoch
        if bre is not None and bre > 0:
            reset_in = bre - _time.time()
            if reset_in > 0:
                quota_resets_in = reset_in

    # Weekly-window quota (Plan 020 D6) — the pace strategy's signal, mapped
    # from the reading's weekly fields. Only read from a fresh reading (``ok``):
    # the pace strategy must never score a provider on stale quota data, and
    # leaving these None is what makes it rank that provider in table order
    # instead.
    weekly_remaining_fraction: float | None = None
    weekly_reset_in: float | None = None
    if cached_reading is not None and cached_reading.ok:
        limit_state = cached_reading.reading
        if limit_state.weekly_remaining_fraction is not None:
            weekly_remaining_fraction = limit_state.weekly_remaining_fraction
        wre = limit_state.weekly_reset_epoch
        if wre is not None and wre > 0:
            w_reset_in = wre - _time.time()
            if w_reset_in > 0:
                weekly_reset_in = w_reset_in

    # Token-budget utilization (Plan 012 Feature B).
    token_utilization: float | None = None
    token_soft_threshold: float | None = None
    if budget_tracker is not None:
        token_utilization = budget_tracker.utilization(name, now=now)
        token_soft_threshold = budget_tracker.soft_threshold_for(name)

    # Trailing-24h usage utilization (Plan 013).
    usage_24h_utilization: float | None = None
    if usage_history_tracker is not None:
        usage_24h_utilization = usage_history_tracker.utilization(name)

    # Peak-pricing window (Plan 025). The wall-clock read lives HERE, in the
    # shell — the routing core receives the boolean and stays clock-free.
    provider_in_peak = bool(ctx.peak_windows) and _peak_in_peak(
        ctx.peak_windows, _time.time()
    )

    return ProviderState(
        name=name,
        availability=availability,
        available_permits=ctx.gate.available,
        queue_depth=ctx.gate.queue_depth,
        retry_after_seconds=retry_after,
        signal_freshness=freshness,
        usage_headroom=usage_headroom,
        quota_resets_in=quota_resets_in,
        token_utilization=token_utilization,
        token_soft_threshold=token_soft_threshold,
        usage_24h_utilization=usage_24h_utilization,
        weekly_remaining_fraction=weekly_remaining_fraction,
        weekly_reset_in=weekly_reset_in,
        in_peak=provider_in_peak,
    )


def _str_or(d: dict[str, object], key: str, default: str) -> str:
    v = d.get(key)
    return v if isinstance(v, str) else default


def _int_or(d: dict[str, object], key: str, default: int) -> int:
    v = d.get(key)
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return default


def _float_or(d: dict[str, object], key: str, default: float) -> float:
    v = d.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return default


def _optional_str(d: dict[str, object], key: str) -> str | None:
    v = d.get(key)
    return v if isinstance(v, str) else None


def _optional_float(d: dict[str, object], key: str) -> float | None:
    v = d.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _safe_filename(name: str) -> str:
    """Sanitize a provider name for use in a filename.

    A short hash suffix makes the mapping collision-resistant: distinct
    provider names that normalize to the same safe string (e.g.
    ``opencode-go`` and ``opencode_go``, or ``a.b`` and ``a_b``) must not
    share one history-store file, or their per-tick time series
    cross-contaminate.  The hash is deterministic, so a name maps to one
    stable file across restarts.
    """
    import hashlib
    import re

    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{safe}.{digest}"
