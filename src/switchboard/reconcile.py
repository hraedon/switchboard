"""Simplified reconciliation loop — poll truth, resize gate, track breaker/box.

Absorbed from sluice.reconcile (Plan 017). Key simplifications:
- Static max_concurrency (no AIMD, no adaptive controller).
- For polled providers (umans): capacity = min(max_concurrency,
  provider_limit - external_sessions) where external_sessions =
  max(0, observed - local_held). Avoids double-counting local holds.
- For header-driven providers (Anthropic/OpenAI/generic): static
  capacity = max_concurrency, but tighten to 0 on: (a) stale headers,
  (b) zero requests_remaining and zero tokens_remaining, (c) positive
  Retry-After on rate-limit 429s. This replaces AIMD with a fail-safe
  static policy (review finding 10).
- Simple breaker: only record_429() (concurrency) feeds the failure
  counter. record_rate_limit_429() and record_gateway_429() are
  observability-only (review finding 7).
- Half-open -> closed on successful poll (not inference probe), since
  multi-provider routing won't route traffic to a CLOSED provider
  (review finding 14).
- Stale data never widens the gate (review finding 13).
- No phantom estimate, no singleton guard.
- History recording restored (Plan 018 WI-2a): optional in-memory ring +
  SQLite store via switchboard.history, generalized from sluice — see
  switchboard/history.py for the field keep/drop decisions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from switchboard.gate import PermitGate
from switchboard.history import History, HistoryEntry, HistoryStore
from switchboard.limit import (
    RETRY_AFTER_SHORT,
    BreakerConfig,
    BreakerSnapshot,
    BreakerState,
    CachedReading,
    LimitState,
)
from switchboard.truth import TruthSource

log = logging.getLogger("switchboard.reconcile")

_RETRY_AFTER_STALE_CAP = 300
_RETRY_AFTER_SATURATION_FLOOR = 5
_RETRY_AFTER_SATURATION_CAP = 60
_RETRY_AFTER_LOW_INTERACTIVITY_FLOOR = 5
_PRUNE_INTERVAL_TICKS = 60
_DEFAULT_HISTORY_TTL = 604800.0  # 7 days

_OVERRIDE_WHITELIST = frozenset({"max_concurrency", "target"})

LOW_INTERACTIVITY = "low_interactivity"


def _in_penalty_window(reading: LimitState, *, now: float) -> bool:
    return (
        reading.boxed_until_epoch is not None
        and now < reading.boxed_until_epoch
    )


def _is_hard_boxed(reading: LimitState, *, now: float) -> bool:
    return _in_penalty_window(reading, now=now) and (
        reading.priority_reason != "rate_limited"
    )


def _is_low_interactivity(reading: LimitState, *, now: float) -> bool:
    return (
        reading.service_mode == LOW_INTERACTIVITY
        and reading.service_mode_resets_at_epoch is not None
        and now < reading.service_mode_resets_at_epoch
    )


def _classify_penalty_band(reading: LimitState, *, now: float) -> str:
    """Honest penalty band for history entries (pure).

    switchboard has no controller band classifier; this derives the band
    from the same reading checks the loop already performs each tick.
    sluice's transient "reject" band (observed above hard_cap) is not
    classified — the static loop has no concept for it.
    """
    if _is_hard_boxed(reading, now=now):
        return "boxed"
    if _is_low_interactivity(reading, now=now):
        return LOW_INTERACTIVITY
    if reading.priority_low:
        return "low"
    return "normal"


def _saturation_retry_after(
    *,
    queue_depth: int,
    capacity: int,
    avg_hold_seconds: float,
    floor: int = _RETRY_AFTER_SATURATION_FLOOR,
    cap: int = _RETRY_AFTER_SATURATION_CAP,
) -> int:
    """Pressure-derived Retry-After hint for saturated 503s (pure)."""
    if avg_hold_seconds <= 0:
        return floor
    if capacity <= 0:
        return cap
    raw = (queue_depth + 1) * avg_hold_seconds / capacity
    return max(floor, min(cap, math.ceil(raw)))


class ReconciliationLoop:
    """Background task that reconciles the gate against upstream truth.

    Simplified from sluice.reconcile.ReconciliationLoop (Plan 017).
    """

    def __init__(
        self,
        *,
        truth_source: TruthSource,
        gate: PermitGate,
        max_concurrency: int,
        poll_interval: float = 5.0,
        breaker_config: BreakerConfig | None = None,
        provider_type: str = "umans",
        fresh_ttl: float = 15.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        poll_interval_idle: float | None = None,
        rng: Callable[[], float] = random.random,
        history: History | None = None,
        history_store: HistoryStore | None = None,
        history_ttl: float = _DEFAULT_HISTORY_TTL,
    ) -> None:
        self._truth = truth_source
        self._gate = gate
        self._max_concurrency = max_concurrency
        self._boot_max_concurrency = max_concurrency
        self._poll_interval = poll_interval
        self._brk_cfg = breaker_config or BreakerConfig()
        self._provider_type = provider_type
        self._fresh_ttl = fresh_ttl
        self._mono = monotonic_clock
        self._wall = wall_clock
        self._poll_interval_idle_cfg = poll_interval_idle
        self._idle = False
        self._poll_now: asyncio.Event | None = None
        self._rng = rng
        self._history = history
        self._history_store = history_store
        self._history_ttl = history_ttl
        self._tick_count = 0

        self._breaker = BreakerSnapshot(
            state=BreakerState.CLOSED,
            consecutive_failures=0,
        )
        self._recent_429s: deque[float] = deque()
        self._recent_rate_limit_429s: deque[float] = deque()
        self._total_429s = 0
        self._total_gateway_429s = 0
        self._total_rate_limit_429s = 0

        self._last_permits = gate.capacity
        self._last_reading_cached: CachedReading | None = None
        self._last_age: float = 0.0

        self._penalty_started_at: float | None = None
        self._prev_in_penalty = False

        self._total_requests_forwarded = 0
        self._last_throughput = 0
        self._prev_total_requests_forwarded = 0

        self._task: asyncio.Task[None] | None = None
        self._first_poll_ok = False
        self._stopped = False

        self._overrides: dict[str, dict[str, Any]] = {}

    def abort(self) -> None:
        """Mark the loop as stopped (public replacement for _stopped)."""
        self._stopped = True

    def apply_override(self, field: str, value: int) -> str | None:
        """Apply a runtime config override for a whitelisted field."""
        if field not in _OVERRIDE_WHITELIST:
            raise ValueError(
                f"field '{field}' is not in the override whitelist"
            )
        if field == "target":
            field = "max_concurrency"
        if not self._first_poll_ok:
            raise ValueError(
                "cannot apply override before first successful usage poll"
            )
        cached = self._last_reading_cached
        if cached is None or not cached.ok:
            raise ValueError(
                "usage reading stale; override unavailable until fresh reading"
            )
        if value < 0:
            raise ValueError("max_concurrency must be >= 0")
        self._max_concurrency = value
        self._overrides["max_concurrency"] = {
            "value": value,
            "since": self._wall(),
        }
        return None

    def clear_override(self, field: str) -> None:
        """Revert a runtime override to its boot value."""
        if field not in _OVERRIDE_WHITELIST:
            raise ValueError(
                f"field '{field}' is not in the override whitelist"
            )
        if field == "target":
            field = "max_concurrency"
        if field not in self._overrides:
            return
        self._max_concurrency = self._boot_max_concurrency
        del self._overrides[field]

    @property
    def overrides(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field, info in self._overrides.items():
            result[field] = {
                "boot": self._boot_max_concurrency,
                "override": info["value"],
                "since": info["since"],
            }
        return result

    def record_429(self) -> None:
        """A concurrency 429 was received. Feeds the breaker."""
        now = self._mono()
        self._recent_429s.append(now)
        self._total_429s += 1
        self._prune_429s(now)
        self._update_breaker_on_429(now)
        self._wake_poll()

    def record_gateway_429(self) -> None:
        """A gateway/CDN 429. Observability only — does NOT feed breaker."""
        self._total_gateway_429s += 1

    def record_rate_limit_429(self) -> None:
        """A rate-limit 429 (positive retry-after). Does NOT feed breaker."""
        now = self._mono()
        self._total_rate_limit_429s += 1
        self._recent_rate_limit_429s.append(now)
        self._wake_poll()

    def record_success(self) -> None:
        """An upstream request completed normally."""
        if self._breaker.state is BreakerState.HALF_OPEN:
            self._breaker = BreakerSnapshot(
                state=BreakerState.CLOSED,
                consecutive_failures=0,
            )
            self._recent_429s.clear()
        elif self._breaker.state is BreakerState.CLOSED:
            if self._breaker.consecutive_failures > 0:
                self._breaker = BreakerSnapshot(
                    state=BreakerState.CLOSED,
                    consecutive_failures=0,
                )

    def record_response_headers(
        self,
        headers: dict[str, str],
        status: int,
        *,
        now_monotonic: float,
    ) -> None:
        """Feed in-band response headers to the truth source."""
        if self._stopped:
            return
        self._truth.record_response_headers(
            headers, status, now_monotonic=now_monotonic
        )

    def record_request_forwarded(self) -> None:
        """A request was forwarded upstream."""
        self._total_requests_forwarded += 1
        self._wake_poll()

    def _prune_429s(self, now: float) -> None:
        cutoff = now - self._brk_cfg.cooldown_seconds
        while self._recent_429s and self._recent_429s[0] < cutoff:
            self._recent_429s.popleft()
        while (
            self._recent_rate_limit_429s
            and self._recent_rate_limit_429s[0] < cutoff
        ):
            self._recent_rate_limit_429s.popleft()

    def _update_breaker_on_429(self, now: float) -> None:
        """Simple consecutive-failure breaker (concurrency 429s only)."""
        if self._breaker.state is BreakerState.OPEN:
            return
        failures = self._breaker.consecutive_failures + 1
        if failures >= self._brk_cfg.failure_threshold:
            self._breaker = BreakerSnapshot(
                state=BreakerState.OPEN,
                consecutive_failures=failures,
                opened_at=now,
            )
        else:
            self._breaker = BreakerSnapshot(
                state=BreakerState.CLOSED,
                consecutive_failures=failures,
            )

    def _wake_poll(self) -> None:
        if self._poll_now is not None:
            self._poll_now.set()

    async def tick(self) -> None:
        """One reconciliation cycle: fetch -> compute -> resize."""
        now_mono = self._mono()
        now_wall = self._wall()

        self._prune_429s(now_mono)

        held_at_fetch = self._gate.held

        cached = await self._truth.fetch(now_monotonic=now_mono)
        age = now_mono - cached.fetched_at_monotonic
        reading = CachedReading(
            reading=LimitState(**{
                k: v
                for k, v in cached.reading.__dict__.items()
                if k != "age_seconds"
            }),
            fetched_at_monotonic=cached.fetched_at_monotonic,
            ok=cached.ok,
        )
        reading = CachedReading(
            reading=LimitState(
                **{
                    **cached.reading.__dict__,
                    "age_seconds": age,
                }
            ),
            fetched_at_monotonic=cached.fetched_at_monotonic,
            ok=cached.ok,
        )

        breaker = self._breaker
        if (
            breaker.state is BreakerState.OPEN
            and breaker.opened_at is not None
        ):
            elapsed = now_mono - breaker.opened_at
            if elapsed >= self._brk_cfg.cooldown_seconds:
                breaker = BreakerSnapshot(
                    state=BreakerState.HALF_OPEN,
                    consecutive_failures=breaker.consecutive_failures,
                    opened_at=breaker.opened_at,
                    half_opened_at=now_mono,
                )
        self._breaker = breaker

        if breaker.state is BreakerState.OPEN or _is_hard_boxed(reading.reading, now=now_wall):
            permits = 0
        elif self._provider_type == "umans":
            permits = self._compute_polled_permits(
                reading, held_at_fetch, cached.ok
            )
        else:
            permits = self._compute_header_permits(reading, cached.ok)

        if not cached.ok and len(self._recent_rate_limit_429s) > 0:
            permits = min(permits, 1)

        await self._gate.resize(permits)

        if breaker.state is BreakerState.HALF_OPEN and cached.ok:
            self._breaker = BreakerSnapshot(
                state=BreakerState.CLOSED,
                consecutive_failures=0,
            )
            self._recent_429s.clear()

        self._last_permits = permits
        self._last_reading_cached = reading
        self._last_age = age

        in_penalty = (
            _is_hard_boxed(reading.reading, now=now_wall)
            or _is_low_interactivity(reading.reading, now=now_wall)
            or reading.reading.priority_low
        )
        if in_penalty and not self._prev_in_penalty:
            self._penalty_started_at = (
                self._derive_penalty_start(reading.reading) or now_wall
            )
        elif not in_penalty:
            self._penalty_started_at = None
        self._prev_in_penalty = in_penalty

        self._last_throughput = (
            self._total_requests_forwarded
            - self._prev_total_requests_forwarded
        )
        self._prev_total_requests_forwarded = (
            self._total_requests_forwarded
        )

        if cached.ok:
            self._first_poll_ok = True

        self._idle = (
            cached.ok
            and self._gate.held == 0
            and len(self._recent_429s) == 0
            and len(self._recent_rate_limit_429s) == 0
            and self._breaker.state is BreakerState.CLOSED
            and not _is_low_interactivity(reading.reading, now=now_wall)
            and not _is_hard_boxed(reading.reading, now=now_wall)
        )

        # Record this tick's state for trend analysis.  The entry is frozen
        # at capture time so the history forms an immutable time series.
        # Recorded when either the in-memory ring or the persistent store is
        # configured.  Fields the static loop does not track (503 counts,
        # queue timeouts, request-window reconciliation) stay None — see
        # switchboard/history.py for the generalization decisions.
        if self._history is not None or self._history_store is not None:
            r = reading.reading
            entry = HistoryEntry(
                timestamp=now_wall,
                concurrent_sessions=r.concurrent_sessions if cached.ok else None,
                local_in_flight=self._gate.held,
                effective_permits=permits,
                limit=r.limit if cached.ok else None,
                hard_cap=r.hard_cap if cached.ok else None,
                band=_classify_penalty_band(r, now=now_wall),
                breaker=self._breaker.state.value,
                priority_low=r.priority_low,
                usage_age=age,
                stale=not cached.ok,
                recent_429s=len(self._recent_429s),
                total_429s=self._total_429s,
                queue_depth=self._gate.queue_depth,
                rate_limit_429s=self._total_rate_limit_429s,
                low_interactivity=_is_low_interactivity(r, now=now_wall),
                requests_in_window=r.requests_in_window if cached.ok else None,
                requests_limit=r.requests_limit if cached.ok else None,
                requests_remaining=r.requests_remaining if cached.ok else None,
                throughput=self._last_throughput,
            )
            if self._history is not None:
                self._history.append(entry)
            if self._history_store is not None:
                self._history_store.append(entry)

    def _record_failed_tick(self) -> None:
        """Record a fail-safe history entry when tick() raises.

        Uses the last-known state (which may be stale) and marks
        ``effective_permits=0``, ``stale=True``, ``tick_failed=True`` so the
        trend shows the gap rather than silently skipping it.
        """
        if self._history is None and self._history_store is None:
            return
        now_wall = self._wall()
        reading = (
            self._last_reading_cached.reading
            if self._last_reading_cached is not None
            else None
        )
        entry = HistoryEntry(
            timestamp=now_wall,
            concurrent_sessions=(
                reading.concurrent_sessions if reading else None
            ),
            local_in_flight=self._gate.held,
            effective_permits=0,
            limit=reading.limit if reading else None,
            hard_cap=reading.hard_cap if reading else None,
            band=(
                _classify_penalty_band(reading, now=now_wall)
                if reading
                else "normal"
            ),
            breaker=self._breaker.state.value,
            priority_low=reading.priority_low if reading else False,
            usage_age=self._last_age,
            stale=True,
            recent_429s=len(self._recent_429s),
            total_429s=self._total_429s,
            queue_depth=self._gate.queue_depth,
            rate_limit_429s=self._total_rate_limit_429s,
            low_interactivity=(
                _is_low_interactivity(reading, now=now_wall)
                if reading
                else False
            ),
            requests_in_window=(
                reading.requests_in_window if reading else None
            ),
            requests_limit=reading.requests_limit if reading else None,
            requests_remaining=(
                reading.requests_remaining if reading else None
            ),
            throughput=0,
            tick_failed=True,
        )
        if self._history is not None:
            self._history.append(entry)
        if self._history_store is not None:
            self._history_store.append(entry)

    def _compute_polled_permits(
        self,
        reading: CachedReading,
        held_at_fetch: int,
        ok: bool,
    ) -> int:
        """Compute gate capacity for polled (umans) providers.

        capacity = min(max_concurrency, provider_limit - external_sessions)
        where external_sessions = max(0, observed - local_held).

        This avoids double-counting locally-held requests (review finding 9).
        Stale data never widens: when ok=False, use the LKG but cap at
        current permits (never increase on stale).
        """
        r = reading.reading
        if not ok:
            return min(self._last_permits, self._max_concurrency)

        external = max(0, r.concurrent_sessions - held_at_fetch)
        capacity = min(
            self._max_concurrency,
            max(0, r.limit - external),
        )
        return capacity

    def _compute_header_permits(
        self,
        reading: CachedReading,
        ok: bool,
    ) -> int:
        """Compute gate capacity for header-driven providers.

        Static capacity = max_concurrency, but tighten to 0 on:
        (a) stale headers (ok=False), (b) zero requests_remaining AND
        zero tokens_remaining, (c) recent rate-limit 429s with stale data.
        This replaces AIMD with a fail-safe static policy (review finding 10).
        """
        if not ok:
            return 0

        r = reading.reading
        if (
            r.requests_remaining is not None
            and r.requests_remaining <= 0
        ):
            return 0
        if (
            r.tokens_remaining is not None
            and r.tokens_remaining <= 0
        ):
            return 0

        return self._max_concurrency

    def _derive_penalty_start(
        self, reading: LimitState
    ) -> float | None:
        if reading.service_mode_resets_at_epoch is not None:
            return reading.service_mode_resets_at_epoch - 86400.0
        if reading.resets_at_epoch is not None:
            return reading.resets_at_epoch - 18000.0
        if reading.boxed_until_epoch is not None:
            duration = (
                float(reading.requests_window_seconds)
                if reading.requests_window_seconds is not None
                else 18000.0
            )
            return reading.boxed_until_epoch - duration
        return None

    async def run(self) -> None:
        """Run the reconciliation loop forever (until cancelled)."""
        if self._poll_now is None:
            self._poll_now = asyncio.Event()
        while True:
            try:
                await self.tick()
                self._tick_count += 1
                if (
                    self._history_store is not None
                    and self._tick_count % _PRUNE_INTERVAL_TICKS == 0
                ):
                    try:
                        self._history_store.prune(
                            ttl_seconds=self._history_ttl, now=self._wall()
                        )
                    except Exception:
                        log.warning(
                            "history store prune failed", exc_info=True
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "reconciliation tick failed — closing gate (fail-safe)"
                )
                self._last_permits = 0
                try:
                    await self._gate.resize(0)
                except Exception:
                    log.critical("failed to close gate after tick exception")
                if self._history is not None or self._history_store is not None:
                    try:
                        self._record_failed_tick()
                    except Exception:
                        log.warning("_record_failed_tick failed", exc_info=True)
            interval = self._effective_poll_interval()
            self._poll_now.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._poll_now.wait(), timeout=interval
                )

    def _effective_poll_interval(self) -> float:
        if self._idle and self._poll_interval_idle_cfg is not None:
            cap = self._fresh_ttl * 0.8
            return min(self._poll_interval_idle_cfg, cap)
        return self._poll_interval

    async def start(self) -> None:
        """Start the background loop as a task."""
        self._stopped = False
        if self._poll_now is None:
            self._poll_now = asyncio.Event()
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Cancel the background loop and close the truth source."""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._truth.close()
        if self._history_store is not None:
            self._history_store.close()

    @property
    def ready(self) -> bool:
        return self._first_poll_ok

    @property
    def last_fetch_ok(self) -> bool:
        return (
            self._last_reading_cached.ok
            if self._last_reading_cached
            else False
        )

    @property
    def last_reading(self) -> CachedReading | None:
        return self._last_reading_cached

    @property
    def penalty_started_at(self) -> float | None:
        return self._penalty_started_at

    def is_low_interactivity(self) -> bool:
        if self._last_reading_cached is None:
            return False
        return _is_low_interactivity(
            self._last_reading_cached.reading, now=self._wall()
        )

    def gate_closed_reason(self) -> str:
        if self._last_reading_cached is not None:
            r = self._last_reading_cached.reading
            if _is_hard_boxed(r, now=self._wall()):
                return "boxed"
        if self._breaker.state is BreakerState.OPEN:
            return "breaker"
        if self._last_permits == 0:
            return "saturated"
        return "open"

    def retry_after_seconds(self) -> int:
        reason = self.gate_closed_reason()
        if reason == "boxed":
            if self._last_reading_cached is not None:
                r = self._last_reading_cached.reading
                if r.resets_at_epoch is not None:
                    remaining = math.ceil(r.resets_at_epoch - self._wall())
                    result = max(30, remaining)
                    if not self._last_reading_cached.ok:
                        return min(result, _RETRY_AFTER_STALE_CAP)
                    return result
            return 30
        if reason == "breaker":
            if self._breaker.opened_at is not None:
                elapsed = self._mono() - self._breaker.opened_at
                cooldown_remaining = (
                    self._brk_cfg.cooldown_seconds - elapsed
                )
                return max(1, math.ceil(cooldown_remaining))
            return RETRY_AFTER_SHORT
        if reason == "saturated":
            poll_floor = min(
                _RETRY_AFTER_SATURATION_CAP,
                max(
                    _RETRY_AFTER_SATURATION_FLOOR,
                    math.ceil(self._poll_interval),
                ),
            )
            estimate = max(self.saturation_hint, poll_floor)
            jittered = estimate * (0.85 + self._rng() * 0.30)
            return max(
                poll_floor,
                min(_RETRY_AFTER_SATURATION_CAP, math.ceil(jittered)),
            )
        return _RETRY_AFTER_SATURATION_FLOOR

    @property
    def saturation_hint(self) -> int:
        return _saturation_retry_after(
            queue_depth=self._gate.queue_depth,
            capacity=self._gate.capacity,
            avg_hold_seconds=self._gate.avg_hold_seconds,
        )

    def saturation_retry_after(self) -> int:
        estimate = self.saturation_hint
        jittered = estimate * (0.85 + self._rng() * 0.30)
        return max(
            _RETRY_AFTER_SATURATION_FLOOR,
            min(_RETRY_AFTER_SATURATION_CAP, math.ceil(jittered)),
        )

    @property
    def breaker_state(self) -> BreakerState:
        return self._breaker.state

    @property
    def target(self) -> int:
        return self._max_concurrency

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def controller_name(self) -> str:
        return "static"

    @property
    def provider_name(self) -> str:
        if self._last_reading_cached is not None:
            return self._last_reading_cached.reading.provider
        return "unknown"

    @property
    def total_requests_forwarded(self) -> int:
        return self._total_requests_forwarded

    @property
    def last_throughput(self) -> int:
        return self._last_throughput

    @property
    def total_429s(self) -> int:
        return self._total_429s

    @property
    def total_rate_limit_429s(self) -> int:
        return self._total_rate_limit_429s

    @property
    def total_gateway_429s(self) -> int:
        return self._total_gateway_429s

    @property
    def last_age_seconds(self) -> float:
        return self._last_age

    @property
    def observed_concurrent_sessions(self) -> int | None:
        if self._last_reading_cached is None:
            return None
        return self._last_reading_cached.reading.concurrent_sessions

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @property
    def poll_interval_idle(self) -> float | None:
        return self._poll_interval_idle_cfg

    @property
    def is_idle(self) -> bool:
        return self._idle

    @property
    def effective_permits_count(self) -> int:
        return self._last_permits

    @property
    def breaker_threshold(self) -> int:
        return self._brk_cfg.failure_threshold

    @property
    def breaker_cooldown_seconds(self) -> float:
        return self._brk_cfg.cooldown_seconds

    # -- stub properties for dropped features (review finding 1) ----------
    # These return safe defaults so admin.py's status/metrics surface
    # doesn't crash. The dashboard HTML should be updated to hide/remove
    # these fields in a follow-up work item.

    @property
    def breaker_half_open_age_seconds(self) -> float | None:
        if self._breaker.half_opened_at is not None:
            return self._mono() - self._breaker.half_opened_at
        return None

    @property
    def band(self) -> Any:
        class _BandStub:
            value = "normal"
        return _BandStub()

    @property
    def phantom_estimate_value(self) -> int:
        return 0

    @property
    def avg_wait_seconds(self) -> float:
        return 0.0

    @property
    def p95_wait_seconds(self) -> float:
        return 0.0

    @property
    def avg_hold_seconds(self) -> float:
        return self._gate.avg_hold_seconds

    @property
    def cooling_down(self) -> int:
        return 0

    @property
    def min_floor(self) -> int:
        return 1

    @property
    def usage_fresh_ttl(self) -> float:
        return self._fresh_ttl

    @property
    def phantom_window(self) -> int:
        return 0

    @property
    def breaker_window_seconds(self) -> float:
        return self._brk_cfg.cooldown_seconds

    @property
    def local_requests_in_window(self) -> int | None:
        return None

    @property
    def request_window_delta(self) -> int | None:
        return None

    @property
    def history(self) -> History | None:
        """The history ring buffer, if configured."""
        return self._history

    @property
    def history_store(self) -> HistoryStore | None:
        """The optional SQLite persistence store, if configured."""
        return self._history_store

    @property
    def recent_429_count(self) -> int:
        return len(self._recent_429s)

    @property
    def queue_timeouts(self) -> int:
        return 0

    @property
    def gateway_429s(self) -> int:
        return self._total_gateway_429s

    @property
    def rate_limit_429s(self) -> int:
        return self._total_rate_limit_429s

    @property
    def total_503s(self) -> int:
        return 0

    @property
    def recent_503_count(self) -> int:
        return 0
