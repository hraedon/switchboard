"""History ring buffer + optional SQLite persistence for control-state snapshots.

Absorbed from sluice.history and sluice.history_store (Plan 018). Records one
:class:`HistoryEntry` per reconciliation tick so the dashboard and external
tools can triage on how concurrency, breaker state, and gate sizing evolved
over time — not just the current point-in-time reading. Entries are frozen at
capture time so the history forms an immutable time series. The store is
telemetry, not the truth path: every store method is fail-safe (logs, never
raises), and losing it must never stop the reconciliation loop.

Generalization decisions (vs sluice's HistoryEntry — switchboard's vendored
ReconciliationLoop has no adaptive controller, no phantom estimator, and a
simplified gate):

- ``phantom_estimate`` (``ph``): DROPPED. Produced by sluice's windowed
  phantom-absorption estimator (observed umans sessions vs local holds), which
  the vendored static loop deliberately does not have. Meaningless outside
  sluice's umans-specific machinery.
- ``band``: KEPT, simplified. switchboard has no controller band classifier;
  the loop derives an honest penalty band from the reading it already
  evaluates each tick: ``boxed`` / ``low_interactivity`` / ``low``
  (priority_low) / ``normal``. sluice's transient ``reject`` band (observed
  above hard_cap) is not classified.
- ``total_503s`` (``t503``): KEPT, retyped ``int | None`` and recorded as
  None. 503 counts are plausibly useful for any provider, but the vendored
  loop dropped ``record_503()`` so it has nothing honest to record.
- ``queue_timeouts`` (``qt``): KEPT, retyped ``int | None`` and recorded as
  None. Queue-timeout counts are provider-agnostic, but the vendored
  PermitGate does not track them.
- ``local_requests_in_window`` (``rlw``) / ``request_window_delta``
  (``rdelta``): KEPT (already nullable), recorded as None. Local
  request-window reconciliation (leakage detection) applies to any provider
  that reports request windows, but the local timestamp-tracking machinery
  was not vendored.
- All other fields are populated from state the vendored loop genuinely
  tracks (reading, gate, breaker, 429 counters, throughput).

Storage simplifications:

- sluice's ``ALTER TABLE`` migration machinery was dropped: switchboard's
  schema is created whole and there are no pre-existing switchboard databases
  to migrate. sluice history databases are NOT compatible (their ``ph``
  column is NOT NULL without a default, so inserts fail — safely, per the
  fail-safe contract — if pointed at one).

The connection must only be accessed from the event loop thread.
``check_same_thread=False`` disables Python's thread-affinity check but does
not provide thread safety — a future contributor adding multi-threaded access
must introduce their own locking.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("switchboard.history")

_CONNECT_TIMEOUT = 5.0


@dataclass(frozen=True)
class HistoryEntry:
    """One reconciliation tick's control-state snapshot, for trend analysis.

    Frozen at capture time so the history forms an immutable time series.
    Compact field names in :meth:`to_dict` (used by JSON surfaces):

    ===    ==========================
    ts     timestamp (epoch seconds)
    obs    concurrent_sessions
    loc    local_in_flight
    ep     effective_permits
    lim    limit (provider concurrency limit)
    hc     hard_cap (provider reject threshold)
    band   penalty band (normal/low/boxed/low_interactivity)
    brk    breaker state (closed/open/half_open)
    pl     priority_low
    age    usage_age (seconds since reading)
    stl    stale (reading not ok)
    r429   recent_429s
    t429   total_429s
    rl429  rate_limit_429s
    t503   total_503s (None: not tracked by this loop)
    li     low_interactivity penalty active
    qd     queue_depth
    qt     queue_timeouts (None: not tracked by this gate)
    err    tick_failed (recorded during a tick exception)
    rwin   requests_in_window
    rlim   requests_limit
    rrem   requests_remaining
    rlw    local_requests_in_window (None: not tracked by this loop)
    rdelta request_window_delta (None: not tracked by this loop)
    tp     throughput (requests forwarded since previous tick)
    ===    ==========================
    """

    timestamp: float  # wall-clock epoch seconds (supplied by caller)
    concurrent_sessions: int | None
    local_in_flight: int
    effective_permits: int
    limit: int | None
    hard_cap: int | None
    band: str
    breaker: str
    priority_low: bool
    usage_age: float
    stale: bool
    recent_429s: int
    total_429s: int
    queue_depth: int
    rate_limit_429s: int = 0
    total_503s: int | None = None
    low_interactivity: bool = False
    queue_timeouts: int | None = None
    # Request-window budget (None when provider reports no request limit)
    requests_in_window: int | None = None
    requests_limit: int | None = None
    requests_remaining: int | None = None
    local_requests_in_window: int | None = None
    request_window_delta: int | None = None
    throughput: int = 0
    tick_failed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": round(self.timestamp, 1),
            "obs": self.concurrent_sessions,
            "loc": self.local_in_flight,
            "ep": self.effective_permits,
            "lim": self.limit,
            "hc": self.hard_cap,
            "band": self.band,
            "brk": self.breaker,
            "pl": self.priority_low,
            "age": round(self.usage_age, 1),
            "stl": self.stale,
            "r429": self.recent_429s,
            "t429": self.total_429s,
            "rl429": self.rate_limit_429s,
            "t503": self.total_503s,
            "li": self.low_interactivity,
            "qd": self.queue_depth,
            "qt": self.queue_timeouts,
            "err": self.tick_failed,
            "rwin": self.requests_in_window,
            "rlim": self.requests_limit,
            "rrem": self.requests_remaining,
            "rlw": self.local_requests_in_window,
            "rdelta": self.request_window_delta,
            "tp": self.throughput,
        }


class History:
    """Bounded ring buffer of :class:`HistoryEntry` snapshots.

    Thread-unsafe by design — the reconciliation loop is single-threaded
    (one ``tick()`` at a time, driven by ``asyncio``). Reads from status
    handlers happen on the same event loop, so no locking is needed.

    Default ``maxlen`` 2880 is ~4 hours at a 5 s poll interval, so memory
    is predictable regardless of uptime.
    """

    def __init__(self, maxlen: int = 2880) -> None:
        if maxlen < 1:
            raise ValueError(f"maxlen must be >= 1, got {maxlen}")
        self._entries: deque[HistoryEntry] = deque(maxlen=maxlen)
        self._maxlen = maxlen

    def append(self, entry: HistoryEntry) -> None:
        """Append a snapshot. Oldest entries are evicted when full."""
        self._entries.append(entry)

    def entries(self) -> list[HistoryEntry]:
        """Return a copy of all entries (oldest first)."""
        return list(self._entries)

    def to_dict_list(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Serialise to a list of dicts for JSON responses.

        If ``limit`` is given and > 0, only the last ``limit`` entries are
        returned. ``limit=0`` or negative returns an empty list.
        """
        if limit is None:
            return [e.to_dict() for e in self._entries]
        if limit <= 0:
            return []
        return [e.to_dict() for e in list(self._entries)[-limit:]]

    def clear(self) -> None:
        """Remove all entries."""
        self._entries.clear()

    @property
    def length(self) -> int:
        """Number of entries currently stored."""
        return len(self._entries)

    @property
    def maxlen(self) -> int:
        """Maximum number of entries the buffer can hold."""
        return self._maxlen


@runtime_checkable
class HistoryStore(Protocol):
    """Optional persistence layer for :class:`History`."""

    def append(self, entry: HistoryEntry) -> None:
        """Persist one entry. Fail-safe: logs on error, never raises."""
        ...

    def load_recent(self, limit: int) -> list[HistoryEntry]:
        """Return the last *limit* entries, oldest-first. Empty on error."""
        ...

    def prune(self, *, ttl_seconds: float, now: float) -> int:
        """Delete entries older than ``now - ttl_seconds``. Returns count deleted."""
        ...

    def close(self) -> None:
        """Close the underlying connection."""
        ...


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS history (
    ts   REAL    NOT NULL,
    obs  INTEGER,
    loc  INTEGER NOT NULL,
    ep   INTEGER NOT NULL,
    lim  INTEGER,
    hc   INTEGER,
    band TEXT    NOT NULL,
    brk  TEXT    NOT NULL,
    pl   INTEGER NOT NULL,
    age  REAL    NOT NULL,
    stl  INTEGER NOT NULL,
    r429 INTEGER NOT NULL,
    t429 INTEGER NOT NULL,
    rl429 INTEGER NOT NULL DEFAULT 0,
    t503 INTEGER,
    li   INTEGER NOT NULL DEFAULT 0,
    qd   INTEGER NOT NULL,
    qt   INTEGER,
    err  INTEGER NOT NULL,
    rwin INTEGER,
    rlim INTEGER,
    rrem INTEGER,
    rlw  INTEGER,
    rdelta INTEGER,
    tp   INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts)"

_COLUMNS = (
    "ts, obs, loc, ep, lim, hc, band, brk, pl, age, stl, r429, t429, "
    "rl429, t503, li, qd, qt, err, rwin, rlim, rrem, rlw, rdelta, tp"
)

_INSERT = f"""\
INSERT INTO history ({_COLUMNS})
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT = f"""\
SELECT {_COLUMNS}
FROM history ORDER BY ts DESC, rowid DESC LIMIT ?
"""


class SQLiteHistoryStore:
    """SQLite-backed persistence for :class:`HistoryEntry` snapshots.

    All public methods are fail-safe: any error is caught and logged. The
    store is telemetry, not the truth path — losing it is a degraded-but-safe
    state. Uses WAL mode for concurrent reads without blocking; writes are
    synchronous (one INSERT per tick, ~0.1 ms in WAL mode) and happen at the
    poll cadence, not per request.
    """

    def __init__(self, path: str) -> None:
        self._conn: sqlite3.Connection | None = None
        try:
            self._conn = sqlite3.connect(
                path,
                check_same_thread=False,
                isolation_level=None,
                timeout=_CONNECT_TIMEOUT,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA wal_autocheckpoint=1000")
            self._conn.execute(_CREATE_TABLE)
            self._conn.execute(_CREATE_INDEX)
        except Exception:
            log.exception("failed to open history store at %s — store disabled", path)
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    self._conn.close()
            self._conn = None

    @property
    def is_available(self) -> bool:
        """True if the store opened successfully and is accepting writes."""
        return self._conn is not None

    def append(self, entry: HistoryEntry) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            conn.execute(
                _INSERT,
                (
                    entry.timestamp,
                    entry.concurrent_sessions,
                    entry.local_in_flight,
                    entry.effective_permits,
                    entry.limit,
                    entry.hard_cap,
                    entry.band,
                    entry.breaker,
                    entry.priority_low,
                    entry.usage_age,
                    entry.stale,
                    entry.recent_429s,
                    entry.total_429s,
                    entry.rate_limit_429s,
                    entry.total_503s,
                    entry.low_interactivity,
                    entry.queue_depth,
                    entry.queue_timeouts,
                    entry.tick_failed,
                    entry.requests_in_window,
                    entry.requests_limit,
                    entry.requests_remaining,
                    entry.local_requests_in_window,
                    entry.request_window_delta,
                    entry.throughput,
                ),
            )
        except Exception:
            log.warning("history store append failed", exc_info=True)

    def load_recent(self, limit: int) -> list[HistoryEntry]:
        conn = self._conn
        if conn is None or limit <= 0:
            return []
        try:
            cur = conn.execute(_SELECT, (limit,))
            rows = cur.fetchall()
        except Exception:
            log.warning("history store load_recent failed", exc_info=True)
            return []
        rows.reverse()
        return [
            HistoryEntry(
                timestamp=row[0],
                concurrent_sessions=row[1],
                local_in_flight=row[2],
                effective_permits=row[3],
                limit=row[4],
                hard_cap=row[5],
                band=row[6],
                breaker=row[7],
                priority_low=bool(row[8]),
                usage_age=row[9],
                stale=bool(row[10]),
                recent_429s=row[11],
                total_429s=row[12],
                rate_limit_429s=row[13] if row[13] is not None else 0,
                total_503s=row[14],
                low_interactivity=bool(row[15]),
                queue_depth=row[16],
                queue_timeouts=row[17],
                tick_failed=bool(row[18]),
                requests_in_window=row[19],
                requests_limit=row[20],
                requests_remaining=row[21],
                local_requests_in_window=row[22],
                request_window_delta=row[23],
                throughput=row[24] if row[24] is not None else 0,
            )
            for row in rows
        ]

    def prune(self, *, ttl_seconds: float, now: float) -> int:
        conn = self._conn
        if conn is None:
            return 0
        cutoff = now - ttl_seconds
        try:
            cur = conn.execute("DELETE FROM history WHERE ts < ?", (cutoff,))
            return cur.rowcount
        except Exception:
            log.warning("history store prune failed", exc_info=True)
            return 0

    def close(self) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception:
            log.warning("history store close failed", exc_info=True)
        finally:
            self._conn = None
