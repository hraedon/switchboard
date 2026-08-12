"""Advisory truth-source containment (Plan 022 follow-up).

A failed *advisory* fetch (a usage scrape or dashboard poll) must cost the
routing optimisation, never availability: the gate stays sized from the
last-known-good reading instead of dropping to zero. Authoritative header
sources keep the fail-closed policy (review finding 10). Before this fix,
one Cloudflare hiccup on the opencode-go scrape or one z.ai API blip zeroed
the provider's gate for a full poll interval.
"""

from __future__ import annotations

import pytest

from switchboard.dashboard import DashboardTruthSource
from switchboard.direct_usage import DirectUsageTruthSource
from switchboard.gate import PermitGate
from switchboard.limit import CachedReading, LimitState
from switchboard.reconcile import ReconciliationLoop
from switchboard.truth import HeaderTruthSource, NullTruthSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeTruthSource:
    """Minimal TruthSource: serves a fixed reading, optionally failing.

    ``advisory`` mirrors the class attribute the real sources carry; tests
    flip it to exercise both gate-sizing policies.
    """

    def __init__(self, reading: LimitState, *, advisory: bool) -> None:
        self.advisory = advisory
        self._reading = reading
        self._fail = False
        self._last_ok_mono = 0.0

    async def fetch(self, *, now_monotonic: float) -> CachedReading:
        if self._fail:
            return CachedReading(
                reading=self._reading,
                fetched_at_monotonic=self._last_ok_mono,
                ok=False,
            )
        self._last_ok_mono = now_monotonic
        return CachedReading(
            reading=self._reading,
            fetched_at_monotonic=now_monotonic,
            ok=True,
        )

    def set_fail(self, fail: bool) -> None:
        self._fail = fail

    def set_reading(self, reading: LimitState) -> None:
        self._reading = reading

    @property
    def last_cached(self) -> CachedReading | None:
        return None

    async def close(self) -> None:
        pass

    def record_response_headers(
        self, headers: dict[str, str], status: int, *, now_monotonic: float
    ) -> None:
        pass


def _reading(**kwargs: object) -> LimitState:
    return LimitState(provider="generic", age_seconds=0.0, **kwargs)  # type: ignore[arg-type]


def _loop(
    truth: FakeTruthSource, *, target: int = 3
) -> tuple[ReconciliationLoop, PermitGate]:
    gate = PermitGate(initial_capacity=0)
    loop = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        max_concurrency=target,
        provider_type="generic",
    )
    return loop, gate


# ---------------------------------------------------------------------------
# Gate sizing on fetch failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advisory_fetch_failure_keeps_gate_open() -> None:
    """A failed advisory fetch sizes the gate from last-known-good — the
    scrape is an optimisation, not an availability dependency."""
    truth = FakeTruthSource(_reading(), advisory=True)
    loop, gate = _loop(truth)
    await loop.tick()
    assert gate.available == 3

    truth.set_fail(True)
    await loop.tick()
    assert gate.available == 3
    assert loop.gate_closed_reason() == "open"


@pytest.mark.asyncio
async def test_authoritative_fetch_failure_closes_gate() -> None:
    """Header-driven (authoritative) sources keep the fail-closed policy:
    stale rate-limit truth means zero permits (review finding 10)."""
    truth = FakeTruthSource(_reading(), advisory=False)
    loop, gate = _loop(truth)
    await loop.tick()
    assert gate.available == 3

    truth.set_fail(True)
    await loop.tick()
    assert gate.available == 0
    assert loop.gate_closed_reason() == "saturated"


@pytest.mark.asyncio
async def test_advisory_failure_respects_lkg_exhaustion() -> None:
    """The data-driven zero still applies on a failed advisory fetch: a
    last-known-good reading that says "exhausted" keeps the gate closed
    until a successful poll says otherwise."""
    truth = FakeTruthSource(
        _reading(requests_remaining=0, requests_limit=100), advisory=True
    )
    loop, gate = _loop(truth)
    await loop.tick()
    assert gate.available == 0

    truth.set_fail(True)
    await loop.tick()
    assert gate.available == 0


@pytest.mark.asyncio
async def test_advisory_failure_with_recent_429_clamps_to_one() -> None:
    """The reactive brake survives the advisory exemption: stale signal plus
    a recent rate-limit 429 clamps the gate to a single probe permit."""
    truth = FakeTruthSource(_reading(), advisory=True)
    loop, gate = _loop(truth)
    await loop.tick()
    assert gate.available == 3

    truth.set_fail(True)
    loop.record_rate_limit_429()
    await loop.tick()
    assert gate.available == 1


@pytest.mark.asyncio
async def test_unknown_source_defaults_to_authoritative() -> None:
    """A truth source without the ``advisory`` attribute is treated as
    authoritative (fail-safe default)."""

    class BareTruth(FakeTruthSource):
        pass

    truth = BareTruth(_reading(), advisory=False)
    del truth.advisory  # simulate a source predating the attribute
    loop, gate = _loop(truth)
    await loop.tick()
    truth.set_fail(True)
    await loop.tick()
    assert gate.available == 0


# ---------------------------------------------------------------------------
# The real sources carry the right classification
# ---------------------------------------------------------------------------


def test_truth_source_advisory_classification() -> None:
    """Contract: scrape/dashboard/null sources are advisory; header sources
    are authoritative. A new source that omits the attribute is treated as
    authoritative by the loop (getattr default False)."""
    assert DirectUsageTruthSource.advisory is True
    assert DashboardTruthSource.advisory is True
    assert NullTruthSource.advisory is True
    assert HeaderTruthSource.advisory is False
