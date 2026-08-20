"""Persisted model-capability observations (Plan 027 W3).

One row per ``(provider, alias)`` in the shared route-table SQLite file
(the same connection the route table, model map, and budget tracker use):
the freshest discovery observation wins, stamped with its wall-clock
`observed_at` and the provider-config fingerprint it was taken under, so a
config change makes the stored evidence visibly stale rather than silently
wrong.

Generation-safe by the family's rules:

* **DB-first writes** (WI-12b pattern) — a failed store write raises before
  memory is touched, so a restart can never revert a reported save.
* **Distrust the store on load** — a corrupt row is logged and skipped,
  boot never crashes on it, and the rest still loads.
* **Generation-bound state is never persisted** — nothing here is derived
  from transient health; the contract computed from these rows is the same
  after a restart as before it (modulo freshness, which the rows carry).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from switchboard.model_capabilities import (
    ModelObservation,
    TriState,
)

log = logging.getLogger("switchboard.capability_store")


def _to_tristate(raw: Any) -> TriState:
    for state in TriState:
        if state.value == raw:
            return state
    return TriState.UNKNOWN


class CapabilityStore:
    """Freshest model observation per (provider, upstream alias)."""

    def __init__(self, db: sqlite3.Connection | None = None) -> None:
        self._db = db
        self._rows: dict[tuple[str, str], ModelObservation] = {}
        if db is not None:
            try:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS model_capability_observations "
                    "(provider TEXT NOT NULL, alias TEXT NOT NULL, "
                    "observed_at REAL NOT NULL, fingerprint TEXT NOT NULL, "
                    "payload TEXT NOT NULL, "
                    "PRIMARY KEY (provider, alias))"
                )
                db.commit()
            except sqlite3.Error:
                log.warning(
                    "capability store: could not ensure table; observations "
                    "start empty and writes may fail",
                    exc_info=True,
                )
            else:
                self._load_from_db()

    def _load_from_db(self) -> None:
        db = self._db
        if db is None:
            return
        cursor = db.execute(
            "SELECT provider, alias, observed_at, fingerprint, payload "
            "FROM model_capability_observations"
        )
        for provider, alias, observed_at, fingerprint, payload in cursor:
            try:
                data = json.loads(payload)
            except (TypeError, ValueError) as exc:
                log.warning(
                    "capability row %s/%s has corrupt JSON, skipping: %s",
                    provider, alias, exc,
                )
                continue
            if not isinstance(data, dict):
                log.warning(
                    "capability row %s/%s is not an object, skipping",
                    provider, alias,
                )
                continue
            obs = _coerce_observation(
                provider, alias,
                observed_at=observed_at,
                fingerprint=fingerprint,
                data=data,
            )
            if obs is not None:
                self._rows[(str(provider), str(alias))] = obs

    def upsert(
        self,
        provider: str,
        observed_observations: list[ModelObservation],
    ) -> int:
        """Store one provider's fresh listing. Returns rows written.

        The listing REPLACES the provider's previous row set in one
        transaction: a successful /models listing is the provider's CURRENT
        model set, so an alias that was present in the previous list but is
        absent from this one must stop certifying contracts (stale evidence
        is why the store carries fingerprints in the first place). The
        delete + inserts run as one unit — a failed write leaves the
        previous listing intact and the memory untouched (DB-first, WI-12b).
        An empty listing is refused (0): the probe only reports OK with
        models, and storing "the provider serves nothing" from a parse
        quirk would erase every contract for the provider.
        """
        if not observed_observations:
            return 0
        db = self._db
        if db is not None:
            try:
                db.execute(
                    "DELETE FROM model_capability_observations "
                    "WHERE provider = ?",
                    (provider,),
                )
                for obs in observed_observations:
                    db.execute(
                        "INSERT INTO model_capability_observations "
                        "(provider, alias, observed_at, fingerprint, payload) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            provider,
                            obs.alias,
                            obs.observed_at,
                            obs.config_fingerprint,
                            json.dumps(_observation_payload(obs)),
                        ),
                    )
                db.commit()
            except sqlite3.Error:
                db.rollback()
                raise
        for k in [k for k in self._rows if k[0] == provider]:
            del self._rows[k]
        for obs in observed_observations:
            self._rows[(provider, obs.alias)] = obs
        return len(observed_observations)

    def drop_provider(self, provider: str) -> int:
        """Remove every observation a provider contributed (provider delete)."""
        gone = [k for k in self._rows if k[0] == provider]
        if not gone:
            return 0
        db = self._db
        if db is not None:
            try:
                db.execute(
                    "DELETE FROM model_capability_observations WHERE provider = ?",
                    (provider,),
                )
                db.commit()
            except sqlite3.Error:
                db.rollback()
                raise
        for k in gone:
            del self._rows[k]
        return len(gone)

    def for_alias(self, provider: str, alias: str) -> ModelObservation | None:
        """The freshest stored observation for one (provider, alias) pair."""
        return self._rows.get((provider, alias))

    def for_provider(self, provider: str) -> dict[str, ModelObservation]:
        """Every stored observation for a provider, keyed by alias."""
        return {a: o for (p, a), o in self._rows.items() if p == provider}

    def all(self) -> list[ModelObservation]:
        """Every stored observation (the admin matrix's raw material)."""
        return sorted(
            self._rows.values(),
            key=lambda o: (o.provider, o.alias),
        )


def _observation_payload(obs: ModelObservation) -> dict[str, Any]:
    return {
        "alias": obs.alias,
        "context_limit": obs.context_limit,
        "input_limit": obs.input_limit,
        "output_limit": obs.output_limit,
        "input_modalities": list(obs.input_modalities),
        "output_modalities": list(obs.output_modalities),
        "tool_calling": obs.tool_calling.value,
        "reasoning": obs.reasoning.value,
        "reasoning_format": obs.reasoning_format,
        "reasoning_levels": list(obs.reasoning_levels),
        "observed_at": obs.observed_at,
        "source": obs.source,
    }


def _coerce_observation(
    provider: str,
    alias: str,
    *,
    observed_at: Any,
    fingerprint: Any,
    data: dict[str, Any],
) -> ModelObservation | None:
    """Rebuild a stored row into a ModelObservation, distrusting every field."""
    import math

    alias = str(data.get("alias") or alias)
    if not alias:
        return None

    def opt_int(key: str) -> int | None:
        v = data.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None
        n = int(v)
        return n if n > 0 else None

    def opt_str_list(key: str) -> tuple[str, ...]:
        v = data.get(key)
        if not isinstance(v, list) or not all(
            isinstance(s, str) and s for s in v
        ):
            return ()
        return tuple(v)

    at = data.get("observed_at", observed_at)
    if not isinstance(at, (int, float)) or isinstance(at, bool) or at < 0:
        at = observed_at if isinstance(observed_at, (int, float)) else 0.0
    fp = fingerprint if isinstance(fingerprint, str) else ""
    source = data.get("source")
    if not isinstance(source, str) or not source:
        source = "discovery"

    return ModelObservation(
        provider=provider,
        alias=alias,
        context_limit=opt_int("context_limit"),
        input_limit=opt_int("input_limit"),
        output_limit=opt_int("output_limit"),
        input_modalities=opt_str_list("input_modalities"),
        output_modalities=opt_str_list("output_modalities"),
        tool_calling=_to_tristate(data.get("tool_calling")),
        reasoning=_to_tristate(data.get("reasoning")),
        reasoning_format=(
            data.get("reasoning_format")
            if isinstance(data.get("reasoning_format"), str)
            else None
        ),
        reasoning_levels=opt_str_list("reasoning_levels"),
        observed_at=float(at),
        source=source,
        config_fingerprint=fp,
    )
