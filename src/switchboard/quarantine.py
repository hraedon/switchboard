"""Provider/model quarantine (Plan 023).

Five consecutive provider-attributable failures for a ``(provider, model)`` pair
take that pair out of service until a human clears it. Deliberately sticky: the
trigger means something needs looking at, and a timer-based release would
recreate the flapping this exists to stop.

The counting is here; the *attribution* — deciding whether a failure was the
provider's fault at all — is pure and lives in
:func:`switchboard.control.classify_failure`. That split matters. A failure
caused by the request itself reproduces on every provider, so counting it would
walk the whole estate into quarantine one provider at a time. See Plan 023 §2
for the live incident that made the point.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from switchboard.control import FailureAttribution

log = logging.getLogger("switchboard.quarantine")

DEFAULT_QUARANTINE_THRESHOLD = 5


@dataclass(frozen=True)
class QuarantineEntry:
    """Why a pair is quarantined — enough to decide without reading logs."""

    provider: str
    model: str
    failures: int
    first_failure_at: float
    last_failure_at: float
    last_status: int | None = None
    last_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Counter:
    consecutive: int = 0
    first_failure_at: float = 0.0
    last_failure_at: float = 0.0
    last_status: int | None = None
    last_detail: str = ""


@dataclass
class QuarantineStore:
    """Optional persistence hook, satisfied by ConfigStoreManager."""

    load: Callable[[], list[dict[str, Any]]]
    save: Callable[[list[dict[str, Any]]], None]


class QuarantineTracker:
    """Counts consecutive provider-attributable failures per (provider, model).

    ``threshold=0`` disables quarantining entirely — counting still happens, so
    the counters remain visible, but nothing is ever taken out of service.
    """

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_QUARANTINE_THRESHOLD,
        store: QuarantineStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._threshold = threshold
        self._store = store
        self._clock = clock
        self._counters: dict[tuple[str, str], _Counter] = {}
        self._entries: dict[tuple[str, str], QuarantineEntry] = {}
        if store is not None:
            self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        assert self._store is not None
        try:
            rows = self._store.load()
        except Exception:
            log.warning(
                "could not read persisted quarantine; starting with none",
                exc_info=True,
            )
            return
        for row in rows:
            try:
                entry = QuarantineEntry(
                    provider=str(row["provider"]),
                    model=str(row["model"]),
                    failures=int(row.get("failures", self._threshold)),
                    first_failure_at=float(row.get("first_failure_at", 0.0)),
                    last_failure_at=float(row.get("last_failure_at", 0.0)),
                    last_status=(
                        int(row["last_status"])
                        if row.get("last_status") is not None
                        else None
                    ),
                    last_detail=str(row.get("last_detail", "")),
                )
            except (KeyError, TypeError, ValueError):
                log.warning("ignoring malformed quarantine row: %r", row)
                continue
            self._entries[(entry.provider, entry.model)] = entry
        if self._entries:
            log.warning(
                "%d provider/model pair(s) are quarantined from a previous "
                "run and will take no traffic until released: %s",
                len(self._entries),
                ", ".join(f"{p}/{m}" for p, m in sorted(self._entries)),
            )

    def _persist(self) -> None:
        if self._store is None:
            return
        try:
            self._store.save([e.to_dict() for e in self._entries.values()])
        except Exception:
            log.warning(
                "could not persist quarantine; it is in force now but will "
                "not survive a restart",
                exc_info=True,
            )

    # -- recording -----------------------------------------------------------

    def record(
        self,
        provider: str,
        model: str,
        attribution: FailureAttribution,
        *,
        status: int | None = None,
        detail: str = "",
    ) -> bool:
        """Record one attempt. Returns True if this call caused a quarantine.

        ``CALLER`` is ignored outright — it is neither evidence against the
        provider nor evidence for it, so it must not reset a genuine streak
        either.
        """
        key = (provider, model)
        if attribution is FailureAttribution.CALLER:
            return False
        if attribution is FailureAttribution.NONE:
            self._counters.pop(key, None)
            return False

        now = self._clock()
        counter = self._counters.get(key)
        if counter is None:
            counter = _Counter(first_failure_at=now)
            self._counters[key] = counter
        counter.consecutive += 1
        counter.last_failure_at = now
        counter.last_status = status
        counter.last_detail = detail[:200]

        if self._threshold <= 0 or key in self._entries:
            return False
        if counter.consecutive < self._threshold:
            return False

        entry = QuarantineEntry(
            provider=provider,
            model=model,
            failures=counter.consecutive,
            first_failure_at=counter.first_failure_at,
            last_failure_at=counter.last_failure_at,
            last_status=counter.last_status,
            last_detail=counter.last_detail,
        )
        self._entries[key] = entry
        self._persist()
        log.error(
            "QUARANTINED %s/%s after %d consecutive provider failures "
            "(last status %s: %s). It will take no traffic for this model "
            "until released via DELETE /admin/quarantine/%s/%s.",
            provider, model, entry.failures, entry.last_status,
            entry.last_detail or "no detail", provider, model,
        )
        return True

    # -- querying ------------------------------------------------------------

    def is_quarantined(self, provider: str, model: str) -> bool:
        return (provider, model) in self._entries

    def filter_candidates(
        self, candidates: tuple[str, ...], model: str
    ) -> tuple[str, ...]:
        """Drop providers quarantined for this model, preserving order."""
        if not self._entries:
            return candidates
        return tuple(
            name for name in candidates
            if (name, model) not in self._entries
        )

    def quarantined_for(self, model: str) -> tuple[str, ...]:
        return tuple(
            sorted(p for (p, m) in self._entries if m == model)
        )

    def entries(self) -> list[QuarantineEntry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def counters(self) -> dict[str, int]:
        """Live consecutive-failure counts, for pairs partway to the threshold."""
        return {
            f"{p}/{m}": c.consecutive
            for (p, m), c in sorted(self._counters.items())
            if c.consecutive > 0
        }

    @property
    def threshold(self) -> int:
        return self._threshold

    def set_threshold(self, threshold: int) -> None:
        """Adopt a new threshold at runtime (``PUT /admin/config/routing``).

        The knob is listed as runtime-mutable, but the tracker is built once at
        boot from ``RoutingConfig.quarantine_threshold``, so without this the
        new value only reached the config object: the operator's change looked
        applied, changed nothing, and then took effect at the next restart when
        the persisted overlay was read — a delayed effect with no visible cause.

        Two deliberate non-behaviours:

        - **No retroactive quarantine.** Lowering the threshold below a pair's
          current streak does not quarantine it on the spot; the next
          provider-attributable failure does. Editing a config value must not
          take a provider out of service by itself.
        - **No retroactive release.** Raising the threshold, or setting 0 to
          disable, leaves existing entries quarantined. Only a human releases
          (Plan 023 §6) — otherwise a config edit silently clears a standing
          alarm nobody looked at.
        """
        if threshold == self._threshold:
            return
        log.info(
            "quarantine threshold %d -> %d (existing entries are unaffected; "
            "release them with DELETE /admin/quarantine/<provider>/<model>)",
            self._threshold, threshold,
        )
        self._threshold = threshold

    # -- releasing -----------------------------------------------------------

    def release(self, provider: str, model: str) -> bool:
        """Clear one pair. Returns False if it was not quarantined."""
        key = (provider, model)
        if key not in self._entries:
            return False
        del self._entries[key]
        self._counters.pop(key, None)
        self._persist()
        log.info("released %s/%s from quarantine", provider, model)
        return True

    def release_all(self) -> int:
        count = len(self._entries)
        self._entries.clear()
        self._counters.clear()
        self._persist()
        if count:
            log.info("released %d pair(s) from quarantine", count)
        return count


def config_store_quarantine_store(config_store: Any) -> QuarantineStore:
    """Adapt a ConfigStoreManager to the persistence hook."""

    def _load() -> list[dict[str, Any]]:
        raw = config_store.get_quarantine()
        return raw if isinstance(raw, list) else []

    def _save(rows: list[dict[str, Any]]) -> None:
        config_store.set_quarantine(rows)

    return QuarantineStore(load=_load, save=_save)


__all__ = [
    "DEFAULT_QUARANTINE_THRESHOLD",
    "QuarantineEntry",
    "QuarantineStore",
    "QuarantineTracker",
    "config_store_quarantine_store",
]
