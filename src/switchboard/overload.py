"""Per-provider overloaded-response breaker — the reactive failover backstop.

Kept in switchboard (not sluice): the desired behaviour is *route away to another
provider*, which is inherently multi-provider.  sluice's 429 concurrency breaker
is a different signal and stays untouched.

When an upstream returns "overloaded" responses (HTTP 503/529 — e.g. umans during
low-interactivity mode returns 503 "The service is temporarily overloaded"),
:class:`OverloadTracker` counts them per provider.  After ``threshold``
consecutive overloaded responses it opens a cooldown during which
:func:`~switchboard.providers.snapshot_provider_state` reports the provider
``CLOSED``, so the pure router fails over.  Any non-overloaded response resets the
provider — a recovered provider is eligible again immediately.

Pure-time: every method takes ``now`` (monotonic) as an argument so the state
machine is fully reproducible and unit-testable without a clock.  All state is
keyed by provider name (bounded by config, not by client input).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class OverloadState:
    """One provider's overload state."""

    consecutive: int = 0
    cooldown_until: float = 0.0  # monotonic; provider is CLOSED while now < this


@dataclass
class OverloadConfig:
    """Breaker parameters."""

    threshold: int = 3  # consecutive overloaded responses before opening
    cooldown_default: float = 30.0  # cooldown when the response gives no Retry-After
    cooldown_min: float = 5.0
    cooldown_max: float = 300.0

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {self.threshold}")
        if not (self.cooldown_min <= self.cooldown_default <= self.cooldown_max):
            raise ValueError(
                "require cooldown_min <= cooldown_default <= cooldown_max, got "
                f"{self.cooldown_min} / {self.cooldown_default} / {self.cooldown_max}"
            )


class OverloadTracker:
    """Per-provider consecutive-overload counter with a cooldown breaker."""

    def __init__(self, config: OverloadConfig | None = None) -> None:
        self._cfg = config or OverloadConfig()
        self._states: dict[str, OverloadState] = {}

    def _state(self, provider: str) -> OverloadState:
        st = self._states.get(provider)
        if st is None:
            st = OverloadState()
            self._states[provider] = st
        return st

    def _clamp_cooldown(self, retry_after: float | None) -> float:
        base = (
            retry_after
            if retry_after is not None and retry_after > 0
            else self._cfg.cooldown_default
        )
        return max(self._cfg.cooldown_min, min(self._cfg.cooldown_max, base))

    def record_overloaded(
        self, provider: str, *, now: float, retry_after: float | None = None
    ) -> None:
        """Record an overloaded response.  Opens/extends the cooldown at threshold."""
        st = self._state(provider)
        st.consecutive += 1
        if st.consecutive >= self._cfg.threshold:
            st.cooldown_until = now + self._clamp_cooldown(retry_after)

    def record_ok(self, provider: str) -> None:
        """Record a non-overloaded response — provider recovered, reset it.

        Clears both the counter and any active cooldown: a response that got
        through means the provider is serving again.  (While cooling the provider
        is CLOSED and normally receives no traffic, so this fires either before
        the breaker opens or once the cooldown has already lapsed and a request
        succeeded — in both cases resetting is correct.)
        """
        st = self._states.get(provider)
        if st is not None:
            st.consecutive = 0
            st.cooldown_until = 0.0

    def is_cooling(self, provider: str, *, now: float) -> bool:
        """True while the provider is in an open overload cooldown."""
        st = self._states.get(provider)
        return st is not None and now < st.cooldown_until

    def cooldown_remaining(self, provider: str, *, now: float) -> int:
        """Whole seconds of cooldown left, or 0 if not cooling."""
        st = self._states.get(provider)
        if st is None or now >= st.cooldown_until:
            return 0
        return max(1, math.ceil(st.cooldown_until - now))

    def consecutive(self, provider: str) -> int:
        """Current consecutive-overload count (for observability)."""
        st = self._states.get(provider)
        return st.consecutive if st is not None else 0
