"""Shell wiring for the low-interactivity threshold estimator (Plan 010 Feature C).

Polls the monitored provider's reconcile loop, builds a :class:`ThresholdSample`
from each fresh reading, feeds it to the pure core
(:func:`switchboard.threshold.update`), and persists the resulting
:class:`EstimatorState` to SQLite (reusing the route table's connection when
available).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from switchboard.threshold import (
    DimensionEstimate,
    EstimatorState,
    ThresholdEstimate,
    ThresholdEvent,
    ThresholdSample,
    _WindowProgress,
    update,
)

if TYPE_CHECKING:
    from switchboard.providers import ProviderContext

log = logging.getLogger("switchboard.estimator")


def _safe_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class ThresholdEstimator:
    """Shell wrapper around the pure threshold estimator.

    Polls the monitored provider's reconcile loop on each request, feeds
    samples to the pure core, and persists state to SQLite.
    """

    def __init__(
        self,
        *,
        provider_name: str,
        db: sqlite3.Connection | None = None,
    ) -> None:
        self.provider_name = provider_name
        self._db = db
        self._state = EstimatorState()
        if db is not None:
            db.execute(
                "CREATE TABLE IF NOT EXISTS threshold_state "
                "(provider TEXT PRIMARY KEY, "
                "state_json TEXT, updated_at REAL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS threshold_events "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "provider TEXT, window_id TEXT, timestamp REAL, "
                "requests INTEGER, tokens INTEGER, "
                "concurrent_sessions INTEGER, triggered INTEGER)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_threshold_events_provider_ts "
                "ON threshold_events (provider, timestamp)"
            )
            db.commit()

    def load(self) -> None:
        """Load state from SQLite. No-op if no DB configured."""
        if self._db is None:
            return
        try:
            cursor = self._db.execute(
                "SELECT state_json FROM threshold_state WHERE provider = ?",
                (self.provider_name,),
            )
            row = cursor.fetchone()
            if row is not None and row[0] is not None:
                self._state = _dict_to_state(json.loads(row[0]))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            log.warning(
                "failed to load threshold state for %s; using default",
                self.provider_name,
            )
            self._state = EstimatorState()

    def state(self) -> EstimatorState:
        """Return the current estimator state."""
        return self._state

    def maybe_sample(
        self, ctx: ProviderContext
    ) -> EstimatorState | None:
        """Poll the provider's reconcile loop and feed a sample if fresh.

        Returns the new state if a sample was taken, or None if the reading
        was stale/absent or the window boundary is unknown.
        """
        if ctx.name != self.provider_name:
            return None

        cached = ctx.reconcile.last_reading
        if cached is None or not cached.ok:
            return None

        reading = cached.reading
        resets_at = reading.service_mode_resets_at_epoch
        if resets_at is None or resets_at <= 0:
            return None

        window_id = str(int(resets_at))
        sample = ThresholdSample(
            window_id=window_id,
            requests=reading.requests_in_window or 0,
            tokens=(reading.tokens_in or 0) + (reading.tokens_out or 0),
            concurrent_sessions=reading.concurrent_sessions,
            low_interactivity=ctx.reconcile.is_low_interactivity(),
        )
        old_state = self._state
        new_state, event = update(self._state, sample)
        self._state = new_state
        if new_state != old_state:
            try:
                self._save(new_state)
            except Exception:
                log.warning(
                    "failed to persist threshold state for %s",
                    self.provider_name,
                    exc_info=True,
                )
        if event is not None:
            try:
                self._save_event(event)
            except Exception:
                log.warning(
                    "failed to persist threshold event for %s",
                    self.provider_name,
                    exc_info=True,
                )
        return new_state

    def _save(self, state: EstimatorState) -> None:
        """Persist state to SQLite. No-op if no DB configured."""
        if self._db is None:
            return
        self._db.execute(
            "INSERT OR REPLACE INTO threshold_state "
            "(provider, state_json, updated_at) VALUES (?, ?, ?)",
            (
                self.provider_name,
                json.dumps(dataclasses.asdict(state)),
                time.time(),
            ),
        )
        self._db.commit()

    def _save_event(self, event: ThresholdEvent) -> None:
        """Persist a threshold event to SQLite. No-op if no DB configured."""
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO threshold_events "
            "(provider, window_id, timestamp, requests, tokens, "
            "concurrent_sessions, triggered) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.provider_name,
                event.window_id,
                time.time(),
                event.requests,
                event.tokens,
                event.concurrent_sessions,
                1 if event.triggered else 0,
            ),
        )
        self._db.commit()

    def recent_events(
        self, *, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return recent threshold events, newest first."""
        if self._db is None:
            return []
        cursor = self._db.execute(
            "SELECT window_id, timestamp, requests, tokens, "
            "concurrent_sessions, triggered "
            "FROM threshold_events WHERE provider = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (self.provider_name, limit),
        )
        return [
            {
                "window_id": row[0],
                "timestamp": row[1],
                "requests": row[2],
                "tokens": row[3],
                "concurrent_sessions": row[4],
                "triggered": bool(row[5]),
            }
            for row in cursor
        ]

    def event_summary(self) -> dict[str, Any]:
        """Aggregate summary of threshold events for /status.json."""
        if self._db is None:
            return {
                "total_events": 0,
                "trigger_count": 0,
                "non_trigger_count": 0,
                "events": [],
            }
        cursor = self._db.execute(
            "SELECT "
            "COUNT(*) as total, "
            "SUM(CASE WHEN triggered = 1 THEN 1 ELSE 0 END) as triggers, "
            "SUM(CASE WHEN triggered = 0 THEN 1 ELSE 0 END) as non_triggers "
            "FROM threshold_events WHERE provider = ?",
            (self.provider_name,),
        )
        row = cursor.fetchone()
        total = row[0] if row else 0
        triggers = row[1] if row and row[1] is not None else 0
        non_triggers = row[2] if row and row[2] is not None else 0
        return {
            "total_events": total,
            "trigger_count": triggers,
            "non_trigger_count": non_triggers,
            "events": self.recent_events(limit=20),
        }

    def prune_events(self, *, max_age_seconds: float = 2_592_000.0) -> None:
        """Prune events older than ``max_age_seconds`` (default: 30 days)."""
        if self._db is None:
            return
        cutoff = time.time() - max_age_seconds
        self._db.execute(
            "DELETE FROM threshold_events "
            "WHERE provider = ? AND timestamp < ?",
            (self.provider_name, cutoff),
        )
        self._db.commit()


def _dict_to_state(d: dict[str, Any]) -> EstimatorState:
    """Reconstruct an :class:`EstimatorState` from a plain dict."""
    try:
        window_data = d.get("window")
        window: _WindowProgress | None = None
        if isinstance(window_data, dict):
            window = _WindowProgress(
                window_id=str(window_data.get("window_id", "")),
                saw_off=bool(window_data.get("saw_off", False)),
                max_off_requests=_safe_int(
                    window_data.get("max_off_requests", 0)
                ),
                max_off_tokens=_safe_int(
                    window_data.get("max_off_tokens", 0)
                ),
                prev_low=bool(window_data.get("prev_low", False)),
                edge_recorded=bool(window_data.get("edge_recorded", False)),
            )

        est_data = d.get("estimate", {})
        if not isinstance(est_data, dict):
            est_data = {}

        req_data = est_data.get("requests", {})
        if not isinstance(req_data, dict):
            req_data = {}
        requests = DimensionEstimate(
            lower=_safe_int_or_none(req_data.get("lower")),
            upper=_safe_int_or_none(req_data.get("upper")),
            edges=_safe_int(req_data.get("edges", 0)),
        )

        tok_data = est_data.get("tokens", {})
        if not isinstance(tok_data, dict):
            tok_data = {}
        tokens = DimensionEstimate(
            lower=_safe_int_or_none(tok_data.get("lower")),
            upper=_safe_int_or_none(tok_data.get("upper")),
            edges=_safe_int(tok_data.get("edges", 0)),
        )

        estimate = ThresholdEstimate(
            requests=requests,
            tokens=tokens,
            edges=_safe_int(est_data.get("edges", 0)),
            last_edge_concurrent_sessions=_safe_int_or_none(
                est_data.get("last_edge_concurrent_sessions")
            ),
        )
        return EstimatorState(estimate=estimate, window=window)
    except (TypeError, ValueError):
        return EstimatorState()
