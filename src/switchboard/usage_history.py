"""Server-side usage-history tracker — 24h rolling token total + penalty event tokens.

Fetches hourly token buckets from the umans ``/v1/usage/history`` API and
computes:

* **24h rolling token total** — sum of ``tokens_in + tokens_out`` across the
  last 24 hours of hourly buckets.  Refreshed every 5 minutes (a 24h total
  shifts slowly).
* **Penalty event token tracking** — when a provider enters a penalty state
  (``penalty_started_at`` is set on the reconcile loop), tracks:
  - **24h-before** — tokens consumed in the 24h *before* the penalty started
    (immutable, fetched once per penalty event).
  - **since-penalty** — tokens consumed *since* the penalty started (grows,
    re-fetched on each refresh).

This is the "usage history-based account" — the poll-based, provider-wide
view from the umans API.  It complements the streaming-tracked token counts
(:mod:`switchboard.usage_observer` + :mod:`switchboard.token_budget`) which
are per-request, from response bodies.

All fetching is async (httpx).  The tracker is mutable and keyed by provider
name.  Results are cached and surfaced in ``/status.json`` and ``/metrics``.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger("switchboard.usage_history")

_USAGE_HISTORY_PATH = "/v1/usage/history"
_HTTP_TIMEOUT = 30.0
_REFRESH_INTERVAL = 300.0  # 5 minutes
_ERROR_RETRY_INTERVAL = 60.0  # min seconds between attempts after a failure


@dataclass
class PenaltyTokenSummary:
    """Token totals around a penalty event."""

    penalty_started_at: float
    before_total: int | None = None
    before_tokens_in: int = 0
    before_tokens_out: int = 0
    before_requests: int = 0
    since_total: int | None = None
    since_tokens_in: int = 0
    since_tokens_out: int = 0
    since_requests: int = 0


@dataclass
class UsageHistorySnapshot:
    """Point-in-time view of a provider's usage-history token counts."""

    tokens_24h: int | None = None
    tokens_24h_in: int = 0
    tokens_24h_out: int = 0
    tokens_24h_requests: int = 0
    penalty: PenaltyTokenSummary | None = None
    last_refresh: float = 0.0
    last_error: str | None = None


def _sum_buckets(buckets: list[dict[str, Any]]) -> dict[str, int]:
    """Sum tokens_in, tokens_out, requests across hourly buckets."""
    tin = 0
    tout = 0
    reqs = 0
    for b in buckets:
        if not isinstance(b, dict):
            continue
        tin += int(b.get("tokens_in") or 0)
        tout += int(b.get("tokens_out") or 0)
        reqs += int(b.get("requests") or 0)
    return {"tin": tin, "tout": tout, "reqs": reqs, "total": tin + tout}


def _iso_from_epoch(ep: float) -> str:
    """ISO 8601 string from an epoch timestamp (for the API query params)."""
    return datetime.fromtimestamp(ep, tz=UTC).isoformat().replace(
        "+00:00", "Z"
    )


class UsageHistoryTracker:
    """Per-provider usage-history token tracker.

    Fetches hourly token buckets from the umans ``/v1/usage/history`` API
    and computes a 24h rolling token total plus penalty event token tracking.

    The tracker is async — call :meth:`refresh` from a background task.
    Results are cached in :attr:`_snapshots` and surfaced via
    :meth:`snapshot`.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, UsageHistorySnapshot] = {}
        self._penalty_seen: dict[str, float] = {}
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._provider_urls: dict[str, str] = {}
        self._provider_keys: dict[str, str] = {}
        self._provider_auth: dict[str, str] = {}
        self._last_attempt: dict[str, float] = {}
        self._caps: dict[str, int] = {}

    def has_provider(self, provider: str) -> bool:
        """True if this provider has a usage-history endpoint configured."""
        return provider in self._clients

    def register(
        self,
        provider: str,
        *,
        base_url: str,
        api_key: str,
        auth_header: str = "authorization",
        cap_tokens: int | None = None,
    ) -> None:
        """Register a provider for usage-history tracking.

        ``cap_tokens`` enables routing utilization (Plan 013): with a cap,
        :meth:`utilization` reports ``tokens_24h / cap_tokens``.  Without
        one the provider is tracked for display only.
        """
        self._clients[provider] = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        self._snapshots.setdefault(provider, UsageHistorySnapshot())
        self._provider_urls[provider] = base_url.rstrip("/")
        self._provider_keys[provider] = api_key
        self._provider_auth[provider] = auth_header
        if cap_tokens is not None and cap_tokens > 0:
            self._caps[provider] = cap_tokens

    def utilization(self, provider: str) -> float | None:
        """Trailing-24h utilization ``tokens_24h / cap_tokens`` (Plan 013).

        Returns ``None`` when no cap is configured for this provider or no
        successful fetch has landed yet — fail safe: no data, no filtering.
        """
        cap = self._caps.get(provider)
        if cap is None or cap <= 0:
            return None
        snap = self._snapshots.get(provider)
        if snap is None or snap.tokens_24h is None:
            return None
        return snap.tokens_24h / cap

    async def refresh(
        self,
        provider: str,
        *,
        penalty_started_at: float | None = None,
    ) -> UsageHistorySnapshot | None:
        """Fetch usage-history buckets and update the snapshot.

        Called from a background task.  Fetches the 24h rolling total and,
        if ``penalty_started_at`` is set, the penalty event token totals.
        """
        if provider not in self._clients:
            return None

        snap = self._snapshots.setdefault(provider, UsageHistorySnapshot())
        now = time.time()

        # Success throttle: a fresh 24h total shifts slowly — reuse it for
        # the refresh interval.  Failure throttle: after a failed attempt,
        # back off for _ERROR_RETRY_INTERVAL so a down endpoint is not
        # hammered on every background-loop tick.
        if now - snap.last_refresh < _REFRESH_INTERVAL and snap.tokens_24h is not None:
            return snap
        if now - self._last_attempt.get(provider, 0.0) < _ERROR_RETRY_INTERVAL:
            return snap

        client = self._clients[provider]
        url = self._provider_urls[provider] + _USAGE_HISTORY_PATH
        auth_header = self._provider_auth[provider]
        api_key = self._provider_keys[provider]

        if auth_header.lower() == "x-api-key":
            headers = {"x-api-key": api_key, "Accept": "application/json"}
        else:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            }

        try:
            # 1) Fetch 24h rolling total.
            params = {
                "from": _iso_from_epoch(now - 86400),
                "to": _iso_from_epoch(now),
                "granularity": "hour",
            }
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            buckets = data.get("buckets", []) if isinstance(data, dict) else []
            summed = _sum_buckets(buckets)
            snap.tokens_24h = summed["total"]
            snap.tokens_24h_in = summed["tin"]
            snap.tokens_24h_out = summed["tout"]
            snap.tokens_24h_requests = summed["reqs"]
            snap.last_error = None

            # 2) Penalty event token tracking.
            if penalty_started_at is not None and penalty_started_at > 0:
                seen = self._penalty_seen.get(provider)
                if seen != penalty_started_at:
                    self._penalty_seen[provider] = penalty_started_at
                    snap.penalty = PenaltyTokenSummary(
                        penalty_started_at=penalty_started_at,
                    )

                if snap.penalty is not None:
                    await self._refresh_penalty(
                        client, url, headers, snap.penalty, now
                    )
            else:
                snap.penalty = None
                self._penalty_seen.pop(provider, None)

            snap.last_refresh = now
        except Exception as exc:
            log.warning(
                "usage-history refresh failed for %s: %s: %s",
                provider,
                type(exc).__name__,
                exc,
            )
            snap.last_error = f"{type(exc).__name__}: {exc}"
            self._last_attempt[provider] = now

        return snap

    async def _refresh_penalty(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        penalty: PenaltyTokenSummary,
        now: float,
    ) -> None:
        """Fetch 24h-before and since-penalty token totals."""
        # 24h before penalty — immutable, fetched once.  On failure, leave
        # before_total as None so the next refresh retries; latching a 0
        # here would permanently misreport the penalty window.
        if penalty.before_total is None:
            try:
                params = {
                    "from": _iso_from_epoch(
                        penalty.penalty_started_at - 86400
                    ),
                    "to": _iso_from_epoch(penalty.penalty_started_at),
                    "granularity": "hour",
                }
                resp = await client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                data = resp.json()
                buckets = (
                    data.get("buckets", []) if isinstance(data, dict) else []
                )
                summed = _sum_buckets(buckets)
                penalty.before_total = summed["total"]
                penalty.before_tokens_in = summed["tin"]
                penalty.before_tokens_out = summed["tout"]
                penalty.before_requests = summed["reqs"]
            except Exception as exc:
                log.warning(
                    "penalty before-tokens fetch failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )

        # Since penalty — grows, re-fetched each refresh.
        try:
            params = {
                "from": _iso_from_epoch(penalty.penalty_started_at),
                "to": _iso_from_epoch(now),
                "granularity": "hour",
            }
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            buckets = data.get("buckets", []) if isinstance(data, dict) else []
            summed = _sum_buckets(buckets)
            penalty.since_total = summed["total"]
            penalty.since_tokens_in = summed["tin"]
            penalty.since_tokens_out = summed["tout"]
            penalty.since_requests = summed["reqs"]
        except Exception as exc:
            log.warning(
                "penalty since-tokens fetch failed: %s: %s",
                type(exc).__name__,
                exc,
            )

    def snapshot(self, provider: str) -> UsageHistorySnapshot | None:
        """Return the cached snapshot for a provider, or None."""
        return self._snapshots.get(provider)

    def status_dict(self, provider: str) -> dict[str, Any] | None:
        """Build a status dict for /status.json, or None if not tracked."""
        snap = self._snapshots.get(provider)
        if snap is None:
            return None
        d: dict[str, Any] = {
            "tokens_24h": snap.tokens_24h,
            "tokens_24h_in": snap.tokens_24h_in,
            "tokens_24h_out": snap.tokens_24h_out,
            "tokens_24h_requests": snap.tokens_24h_requests,
            "last_refresh": round(snap.last_refresh, 1) if snap.last_refresh else None,
            "last_error": snap.last_error,
        }
        if snap.penalty is not None:
            p = snap.penalty
            d["penalty"] = {
                "started_at": round(p.penalty_started_at, 1),
                "before_total": p.before_total,
                "before_tokens_in": p.before_tokens_in,
                "before_tokens_out": p.before_tokens_out,
                "before_requests": p.before_requests,
                "since_total": p.since_total,
                "since_tokens_in": p.since_tokens_in,
                "since_tokens_out": p.since_tokens_out,
                "since_requests": p.since_requests,
            }
        return d

    async def close(self) -> None:
        """Close all HTTP clients."""
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.aclose()
        self._clients.clear()
