"""TruthSource protocol + implementations + provider registry.

Absorbed from sluice.providers and sluice.usage (Plan 017). Simplified:
no ``controller`` field on ``Provider`` (only one strategy: static).
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from switchboard.limit import CachedReading, LimitState

log = logging.getLogger("switchboard.truth")

_DEFAULT_LIMIT = 4
_DEFAULT_HARD_CAP = 8
_USAGE_PATH = "/v1/usage"
_HTTP_TIMEOUT = 30.0
_FAIL_SAFE_AGE = 99999.0


class UsageParseError(Exception):
    """Raised when the usage payload cannot be parsed."""


# ---------------------------------------------------------------------------
# TruthSource protocol + implementations
# ---------------------------------------------------------------------------


@runtime_checkable
class TruthSource(Protocol):
    """How the controller learns the provider's current limit state."""

    async def fetch(self, *, now_monotonic: float) -> CachedReading: ...
    @property
    def last_cached(self) -> CachedReading | None: ...
    async def close(self) -> None: ...
    def record_response_headers(
        self, headers: dict[str, str], status: int, *, now_monotonic: float
    ) -> None: ...


class PolledTruthSource:
    """Wraps umans ``/v1/usage`` polling with LKG caching."""

    # Authoritative: the umans permit computation never consults this flag
    # (it has its own polled path), but the classification contract is that
    # every TruthSource carries the right value.
    advisory = False

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        auth_header: str = "authorization",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + _USAGE_PATH
        if auth_header.lower() == "x-api-key":
            self._headers: dict[str, str] = {
                "x-api-key": api_key,
                "Accept": "application/json",
            }
        else:
            self._headers = {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            }
        self._timeout = _HTTP_TIMEOUT
        self._transport = transport
        self._lkg: CachedReading | None = None
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._client

    async def fetch(self, *, now_monotonic: float) -> CachedReading:
        try:
            client = await self._ensure_client()
            response = await client.get(self._url, headers=self._headers)
            response.raise_for_status()
            data = response.json()
            reading = parse_usage(data)
            cached = CachedReading(
                reading=reading,
                fetched_at_monotonic=now_monotonic,
                ok=True,
            )
            self._lkg = cached
            return cached
        except Exception as exc:
            log.warning("usage fetch failed: %s: %s", type(exc).__name__, exc)
            if self._lkg is not None:
                return CachedReading(
                    reading=self._lkg.reading,
                    fetched_at_monotonic=self._lkg.fetched_at_monotonic,
                    ok=False,
                )
            reading = fail_safe_reading()
            cached = CachedReading(
                reading=reading,
                fetched_at_monotonic=now_monotonic - _FAIL_SAFE_AGE,
                ok=False,
            )
            self._lkg = cached
            return cached

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


class HeaderTruthSource:
    """Holds the latest LimitState built from response ratelimit headers."""

    # Authoritative: the headers ARE the provider's rate-limit truth, so a
    # stale reading fails the gate closed (see ReconciliationLoop).
    advisory = False

    def __init__(
        self, *, provider: str = "anthropic", fresh_ttl: float = 15.0
    ) -> None:
        self._provider = provider
        self._fresh_ttl = fresh_ttl
        self._cached: CachedReading | None = None

    async def fetch(self, *, now_monotonic: float) -> CachedReading:
        if self._cached is None:
            return CachedReading(
                reading=LimitState(
                    requests_remaining=0,
                    tokens_remaining=0,
                    provider=self._provider,
                    age_seconds=0.0,
                ),
                fetched_at_monotonic=now_monotonic,
                ok=True,
            )
        age = now_monotonic - self._cached.fetched_at_monotonic
        reading = dataclasses.replace(self._cached.reading, age_seconds=age)
        ok = age <= self._fresh_ttl
        return CachedReading(
            reading=reading,
            fetched_at_monotonic=self._cached.fetched_at_monotonic,
            ok=ok,
        )

    @property
    def last_cached(self) -> CachedReading | None:
        return self._cached

    async def close(self) -> None:
        pass

    def record_response_headers(
        self, headers: dict[str, str], status: int, *, now_monotonic: float
    ) -> None:
        ls = parse_ratelimit_headers(headers, provider=self._provider)
        has_data = (
            ls.requests_remaining is not None
            or ls.tokens_remaining is not None
        )
        if has_data:
            self._cached = CachedReading(
                reading=ls,
                fetched_at_monotonic=now_monotonic,
                ok=True,
            )
        elif self._cached is None:
            pass


class NullTruthSource:
    """No external truth — generic provider."""

    # No signal at all; nothing to fail closed on (fetch always returns ok).
    advisory = True

    def __init__(self, *, provider: str = "generic") -> None:
        self._provider = provider
        self._cached: CachedReading | None = None

    async def fetch(self, *, now_monotonic: float) -> CachedReading:
        self._cached = CachedReading(
            reading=LimitState(provider=self._provider, age_seconds=0.0),
            fetched_at_monotonic=now_monotonic,
            ok=True,
        )
        return self._cached

    @property
    def last_cached(self) -> CachedReading | None:
        return self._cached

    async def close(self) -> None:
        pass

    def record_response_headers(
        self, headers: dict[str, str], status: int, *, now_monotonic: float
    ) -> None:
        pass


# ---------------------------------------------------------------------------
# Ratelimit header parsing (pure)
# ---------------------------------------------------------------------------


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        return None


def parse_ratelimit_headers(
    headers: dict[str, str], *, provider: str = "anthropic"
) -> LimitState:
    """Parse an allowlist of ratelimit headers into a LimitState.

    Pure: no I/O, no clock.
    """
    h = {k.lower(): v for k, v in headers.items()}

    requests_limit = _safe_int(
        h.get("anthropic-ratelimit-requests-limit")
        or h.get("x-ratelimit-limit-requests")
    )
    requests_remaining = _safe_int(
        h.get("anthropic-ratelimit-requests-remaining")
        or h.get("x-ratelimit-remaining-requests")
    )
    tokens_limit = _safe_int(
        h.get("anthropic-ratelimit-tokens-limit")
        or h.get("x-ratelimit-limit-tokens")
    )
    tokens_remaining = _safe_int(
        h.get("anthropic-ratelimit-tokens-remaining")
        or h.get("x-ratelimit-remaining-tokens")
    )

    unified_remaining = _safe_int(
        h.get("anthropic-ratelimit-unified-40s-remaining")
    )
    if unified_remaining is not None and requests_remaining is None:
        requests_remaining = unified_remaining

    return LimitState(
        requests_limit=requests_limit,
        requests_remaining=requests_remaining,
        tokens_limit=tokens_limit,
        tokens_remaining=tokens_remaining,
        provider=provider,
        age_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# Usage payload parser (pure)
# ---------------------------------------------------------------------------


def _parse_iso_to_epoch(value: str | None) -> float | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _safe_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def parse_usage(data: dict[str, object]) -> LimitState:
    """Parse a ``/v1/usage`` JSON payload into a LimitState."""
    try:
        limits = data.get("limits")
        if not isinstance(limits, dict):
            limits = {}
        concurrency = limits.get("concurrency")
        if not isinstance(concurrency, dict):
            concurrency = {}
        limit = int(concurrency.get("limit", _DEFAULT_LIMIT))
        hard_cap = int(concurrency.get("hard_cap", _DEFAULT_HARD_CAP))

        usage = data.get("usage")
        if not isinstance(usage, dict):
            raise UsageParseError("usage section missing or not a dict")

        cs_raw = usage.get("concurrent_sessions")
        if cs_raw is None:
            raise UsageParseError(
                "concurrent_sessions missing from usage payload"
            )
        concurrent_sessions = int(cs_raw)

        priority = usage.get("priority")
        if not isinstance(priority, dict):
            priority = {}
        priority_low = bool(priority.get("low", False))
        boxed_until_epoch = _parse_iso_to_epoch(priority.get("boxed_until"))
        resets_at_epoch = _parse_iso_to_epoch(priority.get("resets_at"))
        reason_raw = priority.get("reason")
        priority_reason = (
            reason_raw if isinstance(reason_raw, str) else None
        )

        service_mode_block = usage.get("service_mode")
        if not isinstance(service_mode_block, dict):
            service_mode_block = {}
        sm_current_raw = service_mode_block.get("current")
        service_mode = (
            sm_current_raw if isinstance(sm_current_raw, str) else None
        )
        service_mode_resets_at_epoch = _parse_iso_to_epoch(
            service_mode_block.get("resets_at")
        )

        tokens_in = _safe_int_or_none(usage.get("tokens_in"))
        tokens_out = _safe_int_or_none(usage.get("tokens_out"))

        requests_block = limits.get("requests")
        if not isinstance(requests_block, dict):
            requests_block = {}
        requests_limit = _safe_int_or_none(requests_block.get("limit"))
        requests_hard_cap = _safe_int_or_none(
            requests_block.get("hard_cap")
        )
        requests_window_seconds = _safe_int_or_none(
            requests_block.get("window_seconds")
        )
        requests_in_window = _safe_int_or_none(
            usage.get("requests_in_window")
        )
        remaining_requests = _safe_int_or_none(
            usage.get("remaining_requests")
        )

        return LimitState(
            concurrent_sessions=concurrent_sessions,
            limit=limit,
            hard_cap=hard_cap,
            priority_low=priority_low,
            boxed_until_epoch=boxed_until_epoch,
            resets_at_epoch=resets_at_epoch,
            priority_reason=priority_reason,
            service_mode=service_mode,
            service_mode_resets_at_epoch=service_mode_resets_at_epoch,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            requests_limit=requests_limit,
            requests_remaining=remaining_requests,
            requests_in_window=requests_in_window,
            requests_hard_cap=requests_hard_cap,
            requests_window_seconds=requests_window_seconds,
            age_seconds=0.0,
        )
    except UsageParseError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise UsageParseError(
            f"usage parse error: {type(exc).__name__}: {exc}"
        ) from exc


def fail_safe_reading(
    *, limit: int = _DEFAULT_LIMIT, hard_cap: int = _DEFAULT_HARD_CAP
) -> LimitState:
    """Conservative reading for when no data is available."""
    return LimitState(
        concurrent_sessions=hard_cap,
        limit=limit,
        hard_cap=hard_cap,
        priority_low=True,
        age_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# Provider bundle + registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    """A provider's configuration bundle.

    Simplified from sluice.providers.Provider: no ``controller`` field
    (only one strategy: static max_concurrency with header-driven
    tightening for non-umans providers).

    The registry doubles as the GUI provider-picker source (Plan 021 WI-3):
    ``default_base_url`` is the vendor's documented API root (paste-ready),
    and ``probe_endpoint`` is the reachability path the discovery probe GETs.
    """

    name: str
    default_base_url: str
    auth_header: str
    needs_usage_key: bool = False
    probe_endpoint: str = "/models"


_PROVIDERS: dict[str, Provider] = {
    # ── original switchboard providers ──
    "umans": Provider(
        name="umans",
        default_base_url="https://api.code.umans.ai",
        auth_header="authorization",
        needs_usage_key=True,
    ),
    "anthropic": Provider(
        name="anthropic",
        default_base_url="https://api.anthropic.com",
        auth_header="x-api-key",
        needs_usage_key=False,
    ),
    "openai": Provider(
        name="openai",
        default_base_url="https://api.openai.com",
        auth_header="authorization",
        needs_usage_key=False,
    ),
    # ── validated live providers (Plan 021 D4; bases verified 2026-08-08) ──
    "opencode-go": Provider(
        name="opencode-go",
        default_base_url="https://opencode.ai/zen/go/v1",
        auth_header="authorization",
    ),
    "ollama-cloud": Provider(
        name="ollama-cloud",
        default_base_url="https://ollama.com/v1",
        auth_header="authorization",
    ),
    "zai-coding-plan": Provider(
        name="zai-coding-plan",
        default_base_url="https://api.z.ai/api/coding/paas/v4",
        auth_header="authorization",
    ),
    # ── common OpenAI-compatible fleet ──
    "deepseek": Provider(
        name="deepseek",
        default_base_url="https://api.deepseek.com",
        auth_header="authorization",
    ),
    "groq": Provider(
        name="groq",
        default_base_url="https://api.groq.com/openai/v1",
        auth_header="authorization",
    ),
    "openrouter": Provider(
        name="openrouter",
        default_base_url="https://openrouter.ai/api/v1",
        auth_header="authorization",
    ),
    "generic": Provider(
        name="generic",
        default_base_url="",
        auth_header="authorization",
        needs_usage_key=False,
    ),
}


def get_provider(name: str) -> Provider:
    """Resolve a provider by name. Raises ValueError if unknown."""
    if name not in _PROVIDERS:
        raise ValueError(
            f"unknown provider '{name}' — must be one of {sorted(_PROVIDERS)}"
        )
    return _PROVIDERS[name]


def registry_entries() -> list[Provider]:
    """All registry providers, sorted by name, for the GUI picker.

    ``generic`` is always present as the catch-all for providers not in the
    curated registry.  The list is a stable order so the picker does not
    reshuffle between calls.
    """
    return sorted(_PROVIDERS.values(), key=lambda p: p.name)


def make_truth_source(
    provider: Provider,
    *,
    base_url: str,
    api_key: str,
    auth_header: str | None = None,
    fresh_ttl: float = 15.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TruthSource:
    """Construct the appropriate TruthSource for a provider."""
    effective_auth_header = auth_header or provider.auth_header
    if provider.needs_usage_key:
        return PolledTruthSource(
            base_url=base_url,
            api_key=api_key,
            auth_header=effective_auth_header,
            transport=transport,
        )
    if provider.name == "generic":
        return NullTruthSource(provider=provider.name)
    return HeaderTruthSource(provider=provider.name, fresh_ttl=fresh_ttl)
