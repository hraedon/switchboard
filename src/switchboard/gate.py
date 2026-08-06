"""Resizeable permit gate with hold sampling.

Simplified from sluice.gate.PermitGate (Plan 017):
- No reserve, no release cooldown, no wait sampling, no p95.
- Retains hold sampling: reconcile.saturation_retry_after depends on
  avg_hold_seconds to produce a meaningful Retry-After estimate.

Shell module: imports asyncio (async acquire/release), but not httpx.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger("switchboard.gate")


class PermitGate:
    """A resizeable async semaphore with hold-time sampling.

    * ``acquire(timeout)`` blocks until a permit is available or the
      timeout elapses.
    * ``release()`` returns a permit.
    * ``resize(n)`` changes the capacity.  Shrinking below current
      holders does NOT revoke in-flight permits — it just prevents new
      grants until enough drain.
    """

    def __init__(
        self,
        initial_capacity: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        wait_window: int = 64,
    ) -> None:
        self._capacity = initial_capacity
        self._clock = clock
        self._held = 0
        self._waiters = 0
        self._cond = asyncio.Condition()
        self._hold_samples: deque[float] = deque(maxlen=wait_window)

    def _available(self) -> int:
        return max(0, self._capacity - self._held)

    async def acquire(self, *, timeout: float) -> bool:
        """Try to acquire a permit.  Returns True on success, False on timeout."""
        if timeout <= 0:
            async with self._cond:
                if self._available() > 0:
                    self._held += 1
                    return True
                return False

        async with self._cond:
            self._waiters += 1
            try:
                deadline = self._clock() + timeout
                while self._available() <= 0:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        return False
                    try:
                        await asyncio.wait_for(
                            self._cond.wait(), timeout=remaining
                        )
                    except TimeoutError:
                        return False
                self._held += 1
                return True
            finally:
                self._waiters -= 1

    async def release(
        self, *, hold_seconds: float | None = None
    ) -> None:
        """Release a permit.

        ``hold_seconds`` is the acquire->release duration, measured by
        the caller with the same monotonic clock.  When provided, it is
        sampled for the ``avg_hold_seconds`` advisory hint used by
        ``saturation_retry_after``.
        """
        async with self._cond:
            if self._held <= 0:
                log.warning(
                    "release called with no held permits (double-release?)"
                )
                return
            self._held -= 1
            if hold_seconds is not None:
                self._hold_samples.append(hold_seconds)
            self._cond.notify_all()

    async def resize(self, new_capacity: int) -> None:
        """Change the capacity.  Never revokes in-flight permits."""
        if new_capacity < 0:
            log.warning(
                "resize called with negative capacity %d — clamping to 0",
                new_capacity,
            )
            new_capacity = 0
        async with self._cond:
            self._capacity = new_capacity
            self._cond.notify_all()

    @property
    def held(self) -> int:
        return self._held

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def queue_depth(self) -> int:
        return self._waiters

    @property
    def available(self) -> int:
        return self._available()

    @property
    def avg_hold_seconds(self) -> float:
        """Mean hold duration over recent completed holds (0.0 if none).

        Only *completed* holds are sampled — long-running streams still
        in flight are invisible, so the average skews short under mixed
        workloads.  Acceptable for an advisory hint (the saturation
        Retry-After estimator); would not be acceptable for a control
        input.
        """
        if not self._hold_samples:
            return 0.0
        return sum(self._hold_samples) / len(self._hold_samples)

    @property
    def cooling_down(self) -> int:
        """Stubs: no release cooldown in the simplified gate (Plan 017)."""
        return 0
