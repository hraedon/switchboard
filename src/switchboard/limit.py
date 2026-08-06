"""Pure limit state, cached reading, and breaker types.

Stdlib-only — enforced by the import-boundary test. This module contains
no I/O, no async, and no clock: all state is passed in as arguments.

Absorbed from sluice.control and sluice.usage (Plan 017).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class LimitState:
    """A normalized snapshot of the provider's limit state.

    Different providers expose different signals.  umans polls a live
    concurrency count; Anthropic/OpenAI surface token/request buckets via
    response headers; a generic compatible endpoint may expose neither.
    This dataclass unions all fields so any controller strategy can
    consume a single type.

    *Concurrency fields* (umans ``/v1/usage``):
        ``concurrent_sessions``, ``limit``, ``hard_cap``, ``priority_low``,
        ``boxed_until_epoch``, ``resets_at_epoch``.

    *Token-bucket fields* (Anthropic/OpenAI response headers):
        ``requests_remaining``, ``tokens_remaining``, ``bucket_reset_epoch``.

    Only the fields the provider supplies are populated; the rest keep
    their defaults.  Controller strategies read only the fields relevant
    to them.
    """

    concurrent_sessions: int = 0
    limit: int = 4
    hard_cap: int = 8
    priority_low: bool = False
    boxed_until_epoch: float | None = None
    resets_at_epoch: float | None = None
    priority_reason: str | None = None

    service_mode: str | None = None
    service_mode_resets_at_epoch: float | None = None

    tokens_in: int | None = None
    tokens_out: int | None = None

    requests_limit: int | None = None
    requests_remaining: int | None = None
    requests_in_window: int | None = None
    requests_hard_cap: int | None = None
    requests_window_seconds: int | None = None
    tokens_limit: int | None = None
    tokens_remaining: int | None = None
    bucket_reset_epoch: float | None = None

    age_seconds: float = 0.0
    provider: str = "umans"


UsageReading = LimitState


@dataclass
class CachedReading:
    """A usage reading paired with its fetch timestamp and success flag."""

    reading: LimitState
    fetched_at_monotonic: float
    ok: bool


RETRY_AFTER_SHORT = 5


class BreakerState(Enum):
    """Simplified breaker states (same as sluice.control.BreakerState)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerConfig:
    """Simplified breaker: consecutive concurrency-429s -> open -> cooldown.

    Only ``record_429()`` (concurrency-classified) feeds the failure
    counter.  ``record_rate_limit_429()`` and ``record_gateway_429()`` are
    observability-only and do NOT trip the breaker.
    """

    failure_threshold: int = 5
    cooldown_seconds: float = 30.0


@dataclass
class BreakerSnapshot:
    """Immutable snapshot of breaker state at a point in time."""

    state: BreakerState
    consecutive_failures: int
    opened_at: float | None = None
    half_opened_at: float | None = None
