"""Pure, deterministic token-budget math — the token-cap switching core.

This module is the token-budget decision engine.  It imports **nothing outside
the standard library**, does **no I/O**, and reads **no clock**: the current
time and every observation are passed in as arguments so decisions are fully
reproducible and unit-testable without a network.

Enforced by tests/test_import_boundary.py (same as control.py).

The budget model: an operator sets a per-provider token cap per rolling
window (e.g. 5 M tokens / hour).  switchboard tracks tokens it routes (own
count) and reconciles with the usage-dashboard's provider-wide reading.  When
the projected utilization exceeds a soft threshold, the routing core demotes
the provider to BUSY (Plan 012 §4).

Key design decisions:

* **max(own, provider_wide)** — the provider-wide total is authoritative (it
  includes switchboard's contribution plus other clients).  Using own alone
  would under-count when the provider is shared.  Using max() means: if other
  clients consume budget, switchboard sees utilization rise and bleeds traffic
  accordingly (the "reconciled with stored hourly readings" the operator asked
  for).
* **Projection** — if switchboard used X tokens in the first 40 % of the
  window, linear projection estimates X / 0.4 for the full window.  This lets
  switchboard switch *before* the cap is hit, not after.  Conservative: only
  projects forward (never backward), and only when elapsed > 10 % of the
  window (avoids wild early-window swings).
* **None means no data** — when no budget is configured or no samples exist,
  utilization is None and the routing core applies no filtering (today's
  behaviour).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenBudgetConfig:
    """Operator-set token budget for one provider.

    * ``cap_tokens`` — the maximum tokens switchboard should route to this
      provider per ``window_seconds`` window.
    * ``window_seconds`` — the rolling window length (default 1 hour).
    * ``soft_threshold`` — the utilization fraction at which the routing core
      starts treating this provider as BUSY (bleeding traffic to alternatives).
      Default 0.85 (switch at 85 % of cap).
    """

    cap_tokens: int
    window_seconds: float = 3600.0
    soft_threshold: float = 0.85

    def __post_init__(self) -> None:
        if self.cap_tokens <= 0:
            raise ValueError(
                f"cap_tokens must be > 0, got {self.cap_tokens}"
            )
        if not math.isfinite(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError(
                f"window_seconds must be finite and > 0, got {self.window_seconds}"
            )
        if not math.isfinite(self.soft_threshold) or not (0.0 < self.soft_threshold <= 1.0):
            raise ValueError(
                f"soft_threshold must be finite and in (0.0, 1.0], got {self.soft_threshold}"
            )


@dataclass(frozen=True)
class TokenSnapshot:
    """One point-in-time view of a provider's token budget.

    Assembled by the shell from the tracker + dashboard reading.  Pure data.
    """

    own_tokens_in_window: int
    provider_tokens_in_window: int | None
    cap_tokens: int
    projected_utilization: float | None


def compute_utilization(
    own_tokens: int,
    provider_tokens: int | None,
    cap_tokens: int,
) -> float | None:
    """Compute current utilization as a fraction of the cap.

    Uses ``max(own_tokens, provider_tokens)`` when the provider-wide total is
    available — it is authoritative and includes switchboard's contribution.
    Returns ``None`` when ``cap_tokens <= 0`` (no cap configured).
    """
    if cap_tokens <= 0:
        return None
    effective = max(own_tokens, provider_tokens) if provider_tokens is not None else own_tokens
    return effective / cap_tokens


def project_utilization(
    current_tokens: int,
    elapsed_seconds: float,
    window_seconds: float,
    cap_tokens: int,
) -> float | None:
    """Linearly project utilization to the end of the window.

    If we used ``current_tokens`` in ``elapsed_seconds`` of a
    ``window_seconds`` window, the projected full-window total is::

        current_tokens / (elapsed_seconds / window_seconds)

    This lets switchboard switch *before* the cap is hit.  Conservative:

    * Only projects when ``elapsed_seconds > 10 %`` of the window — earlier
      than that, a single request can spike the projection wildly.
    * Never projects backward (``elapsed == 0`` → returns current utilization).
    * Returns ``None`` when ``cap_tokens <= 0``.

    The result is clamped to ``>= 0.0`` (never negative).
    """
    if cap_tokens <= 0:
        return None
    if elapsed_seconds <= 0:
        return current_tokens / cap_tokens
    if elapsed_seconds < window_seconds * 0.1:
        return current_tokens / cap_tokens
    fraction = elapsed_seconds / window_seconds
    projected = current_tokens / fraction
    return max(0.0, projected / cap_tokens)


def is_over_budget(
    utilization: float | None,
    threshold: float,
) -> bool:
    """True when utilization meets or exceeds the threshold.

    ``None`` utilization (no data) → ``False`` (no filtering, fail open to
    today's behaviour — consistent with headroom filtering).
    """
    if utilization is None:
        return False
    return utilization >= threshold
