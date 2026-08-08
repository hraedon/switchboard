"""Shell token-budget tracker — mutable rolling-window accounting (Plan 012 §4.4).

Maintains a per-provider rolling-window token counter from switchboard's own
in-flight usage observations, reconciled with the usage-dashboard's
provider-wide reading.  All time is passed as ``now`` (monotonic) so the
behaviour is deterministic and testable.

Persistence: when a SQLite connection is available (sharing the route-table
store), samples persist across restarts so the budget survives a deploy.

Same pattern as :class:`~switchboard.overload.OverloadTracker`: mutable state
keyed by provider name, every method takes ``now`` as an argument.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import deque
from typing import Any

from switchboard.budget import (
    TokenBudgetConfig,
    compute_utilization,
    project_utilization,
)

log = logging.getLogger("switchboard.token_budget")

_WINDOW_START_STORE = "token_usage"


class TokenBudgetTracker:
    """Per-provider rolling-window token tracker with dashboard reconciliation.

    Holds a :class:`TokenBudgetConfig` per provider and a rolling deque of
    ``(timestamp, token_count)`` samples.  ``utilization`` combines switchboard's
    own count with the dashboard's provider-wide total (if available) and
    projects forward so the routing core can switch *before* the cap is hit.
    """

    def __init__(
        self,
        configs: dict[str, TokenBudgetConfig] | None = None,
        *,
        db: sqlite3.Connection | None = None,
    ) -> None:
        self._configs = configs or {}
        self._db = db
        self._windows: dict[str, deque[tuple[float, int]]] = {}
        self._provider_wide: dict[str, int | None] = {}
        self._provider_wide_ts: dict[str, float] = {}

        if db is not None:
            db.execute(
                f"CREATE TABLE IF NOT EXISTS {_WINDOW_START_STORE} "
                "(provider TEXT, timestamp REAL, tokens INTEGER)"
            )
            # Wall-clock timestamp for cross-reboot age (Plan 012 fix: the
            # monotonic ``timestamp`` resets at every system reboot, so a row
            # written hours before a reboot looks "fresh" afterwards and poisons
            # the window.  ``wall_ts`` ages by the wall clock on load and is
            # translated back into the current monotonic frame so in-memory
            # windowing stays consistent.  Existing rows have NULL wall_ts and
            # are treated as stale (skipped on load, purged on prune).
            cols = {
                row[1]
                for row in db.execute(
                    f"PRAGMA table_info({_WINDOW_START_STORE})"
                )
            }
            if "wall_ts" not in cols:
                db.execute(
                    f"ALTER TABLE {_WINDOW_START_STORE} "
                    f"ADD COLUMN wall_ts REAL"
                )
            db.execute(
                f"CREATE INDEX IF NOT EXISTS "
                f"idx_{_WINDOW_START_STORE}_provider_ts "
                f"ON {_WINDOW_START_STORE} (provider, timestamp)"
            )
            db.commit()

    def has_budget(self, provider: str) -> bool:
        """True if a token budget is configured for this provider."""
        return provider in self._configs

    def soft_threshold_for(self, provider: str) -> float | None:
        """The configured ``soft_threshold`` for a provider, or None.

        None means no token budget is configured for this provider; the
        routing core then falls back to the global ``token_budget_threshold``.
        """
        cfg = self._configs.get(provider)
        return cfg.soft_threshold if cfg is not None else None

    def record_usage(
        self,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        now: float,
    ) -> None:
        """Record one request's token usage for a provider.

        Persists to SQLite if configured (for restart survival).  Samples are
        pruned to the provider's window on each call.
        """
        if provider not in self._configs:
            return
        total = prompt_tokens + completion_tokens
        if total <= 0:
            return
        window = self._window(provider)
        window.append((now, total))
        self._prune(provider, now)
        if self._db is not None:
            try:
                self._db.execute(
                    f"INSERT INTO {_WINDOW_START_STORE} "
                    "(provider, timestamp, tokens, wall_ts) "
                    "VALUES (?, ?, ?, ?)",
                    (provider, now, total, time.time()),
                )
                self._db.commit()
            except Exception:
                log.warning(
                    "failed to persist token usage for %s",
                    provider,
                    exc_info=True,
                )

    def reconcile(
        self,
        provider: str,
        provider_wide_tokens: int | None,
        *,
        now: float,
    ) -> None:
        """Store the dashboard's provider-wide token total for reconciliation.

        Called from the dashboard poll path.  The provider-wide total includes
        switchboard's contribution plus other clients.  ``utilization`` uses
        ``max(own, provider_wide)`` — the authoritative number.
        """
        self._provider_wide[provider] = provider_wide_tokens
        self._provider_wide_ts[provider] = now

    def own_tokens(self, provider: str, *, now: float) -> int:
        """switchboard's rolling-window token count for a provider."""
        if provider not in self._configs:
            return 0
        self._prune(provider, now)
        return sum(tokens for _, tokens in self._window(provider))

    def utilization(self, provider: str, *, now: float) -> float | None:
        """Projected token-budget utilization (0..1+), or None if no budget.

        Combines own count with the dashboard's provider-wide total and
        projects forward using the elapsed fraction of the window.  Returns
        ``None`` when no budget is configured for this provider.
        """
        cfg = self._configs.get(provider)
        if cfg is None:
            return None

        own = self.own_tokens(provider, now=now)
        pw = self._provider_wide.get(provider)
        pw_ts = self._provider_wide_ts.get(provider, now)
        pw_age = now - pw_ts

        # Expire stale provider-wide readings (older than the window).
        if pw is not None and pw_age > cfg.window_seconds:
            pw = None

        current = compute_utilization(own, pw, cfg.cap_tokens)
        if current is None:
            return None

        # Project forward for proactive switching.
        window_start = self._window_start(provider, now)
        elapsed = now - window_start
        projected = project_utilization(
            max(own, pw) if pw is not None else own,
            elapsed,
            cfg.window_seconds,
            cfg.cap_tokens,
        )
        return projected if projected is not None else current

    def budget_summary(self, provider: str) -> dict[str, Any] | None:
        """A summary dict for /status.json, or None if no budget configured."""
        cfg = self._configs.get(provider)
        if cfg is None:
            return None
        now = time.monotonic()
        own = self.own_tokens(provider, now=now)
        pw = self._provider_wide.get(provider)
        return {
            "cap_tokens": cfg.cap_tokens,
            "window_seconds": cfg.window_seconds,
            "soft_threshold": cfg.soft_threshold,
            "own_tokens_in_window": own,
            "provider_wide_tokens": pw,
            "projected_utilization": self.utilization(
                provider, now=now
            ),
        }

    # -- internals -----------------------------------------------------------

    def _window(self, provider: str) -> deque[tuple[float, int]]:
        w = self._windows.get(provider)
        if w is None:
            w = deque()
            self._windows[provider] = w
        return w

    def _prune(self, provider: str, now: float) -> None:
        cfg = self._configs.get(provider)
        if cfg is None:
            return
        cutoff = now - cfg.window_seconds
        w = self._window(provider)
        while w and w[0][0] < cutoff:
            w.popleft()

    def _window_start(self, provider: str, now: float) -> float:
        """The timestamp of the oldest sample in the window, or now if empty."""
        w = self._window(provider)
        if not w:
            return now
        return w[0][0]

    def load(self) -> None:
        """Load persisted samples from SQLite into the rolling windows.

        Called once at startup.  Ages samples by their **wall-clock** timestamp
        (``wall_ts``) so the window is correct across a system reboot, where the
        monotonic clock resets to zero.  Each loaded sample is translated into
        the current monotonic frame (``mono_now - age``) so in-memory pruning
        and projection agree.  Rows without ``wall_ts`` (pre-fix) or older than
        the largest configured window are skipped.
        """
        if self._db is None or not self._configs:
            return
        max_window = max(
            cfg.window_seconds for cfg in self._configs.values()
        )
        wall_now = time.time()
        mono_now = time.monotonic()
        try:
            cursor = self._db.execute(
                f"SELECT provider, tokens, wall_ts "
                f"FROM {_WINDOW_START_STORE} "
                f"WHERE wall_ts IS NOT NULL AND wall_ts > ?",
                (wall_now - max_window,),
            )
            for provider, tokens, wall_ts in cursor:
                if provider not in self._configs:
                    continue
                age = wall_now - wall_ts
                if age < 0:
                    age = 0.0
                self._window(provider).append((mono_now - age, tokens))
            # Drop rows too old to matter (or lacking wall_ts), once on load.
            self._db.execute(
                f"DELETE FROM {_WINDOW_START_STORE} "
                f"WHERE wall_ts IS NULL OR wall_ts <= ?",
                (wall_now - max_window,),
            )
            self._db.commit()
        except Exception:
            log.warning(
                "failed to load token usage history", exc_info=True
            )

    def prune_all(self, *, now: float) -> None:
        """Prune all provider windows.  Called periodically by the shell."""
        for provider in self._configs:
            self._prune(provider, now)
        if self._db is not None:
            wall_oldest = time.time() - max(
                (c.window_seconds for c in self._configs.values()),
                default=3600.0,
            )
            try:
                self._db.execute(
                    f"DELETE FROM {_WINDOW_START_STORE} "
                    f"WHERE wall_ts IS NULL OR wall_ts <= ?",
                    (wall_oldest,),
                )
                self._db.commit()
            except Exception:
                log.warning(
                    "failed to prune token usage history",
                    exc_info=True,
                )
