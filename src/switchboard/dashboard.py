"""TruthSource backed by the usage-dashboard /readings API.

Polls the dashboard for ollama usage data and normalizes into a
:class:`~sluice.control.LimitState` for the routing engine.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from sluice.control import LimitState
from sluice.usage import CachedReading

log = logging.getLogger("switchboard.dashboard")

_HTTP_TIMEOUT = 30.0
_FAIL_SAFE_AGE = 99999.0


def _parse_timestamp(ts: str | None) -> float | None:
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _reading_to_limit_state(
    reading: dict[str, Any],
    provider_name: str,
) -> LimitState:
    session_pct_raw = reading.get("session_percent")
    session_pct: float | None = None
    if session_pct_raw is not None:
        try:
            session_pct = float(session_pct_raw)
        except (ValueError, TypeError):
            session_pct = None

    requests_remaining = round(100 - session_pct) if session_pct is not None else None

    return LimitState(
        concurrent_sessions=0,
        limit=1,
        hard_cap=2,
        requests_remaining=requests_remaining,
        requests_limit=100 if requests_remaining is not None else None,
        provider=provider_name,
        age_seconds=0.0,
    )


def _fail_safe_reading(provider_name: str) -> LimitState:
    return LimitState(
        concurrent_sessions=2,
        limit=1,
        hard_cap=2,
        requests_remaining=0,
        requests_limit=100,
        provider=provider_name,
        age_seconds=0.0,
    )


class DashboardTruthSource:
    """TruthSource backed by the usage-dashboard /readings API.

    Polls the dashboard for ollama usage data (session_percent,
    weekly_percent) and normalizes into a LimitState.

    The dashboard's 5-30 min cadence means the reading is coarse. The
    routing decision treats stale data fail-safe: when older than
    dashboard_stale_ttl, pressure is unknown → provider is "available
    but uncertain" (not closed, not preferred for failover target).
    """

    def __init__(
        self,
        *,
        dashboard_url: str,
        bearer_token: str,
        provider_name: str = "ollama",
        stale_ttl: float = 900.0,
    ) -> None:
        self._url = dashboard_url.rstrip("/") + "/readings"
        self._headers: dict[str, str] = {
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
        }
        self._provider_name = provider_name
        self._stale_ttl = stale_ttl
        self._lkg: CachedReading | None = None
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        return self._client

    def _serve_lkg_or_failsafe(self, now_monotonic: float) -> CachedReading:
        if self._lkg is not None:
            return CachedReading(
                reading=self._lkg.reading,
                fetched_at_monotonic=self._lkg.fetched_at_monotonic,
                ok=False,
            )
        reading = _fail_safe_reading(self._provider_name)
        cached = CachedReading(
            reading=reading,
            fetched_at_monotonic=now_monotonic - _FAIL_SAFE_AGE,
            ok=False,
        )
        self._lkg = cached
        return cached

    async def fetch(self, *, now_monotonic: float) -> CachedReading:
        """Fetch /readings, extract the ollama reading, normalize."""
        try:
            client = await self._ensure_client()
            response = await client.get(self._url, headers=self._headers)
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, list):
                log.warning(
                    "dashboard: unexpected response type: %s", type(data).__name__
                )
                return self._serve_lkg_or_failsafe(now_monotonic)

            target: dict[str, Any] | None = None
            for r in data:
                if isinstance(r, dict) and r.get("provider") == self._provider_name:
                    target = r
                    break

            if target is None:
                log.warning(
                    "dashboard: no reading for provider '%s'", self._provider_name
                )
                return self._serve_lkg_or_failsafe(now_monotonic)

            reading = _reading_to_limit_state(target, self._provider_name)

            ts_raw = target.get("timestamp")
            ts_epoch = _parse_timestamp(ts_raw if isinstance(ts_raw, str) else None)

            stale = True
            if ts_epoch is not None:
                stale = (time.time() - ts_epoch) > self._stale_ttl

            cached = CachedReading(
                reading=reading,
                fetched_at_monotonic=now_monotonic,
                ok=not stale,
            )
            self._lkg = cached
            return cached
        except Exception as exc:
            log.warning("dashboard fetch failed: %s: %s", type(exc).__name__, exc)
            return self._serve_lkg_or_failsafe(now_monotonic)

    @property
    def last_cached(self) -> CachedReading | None:
        return self._lkg

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def record_response_headers(
        self, headers: dict[str, str], status: int, *, now_monotonic: float
    ) -> None:
        pass
