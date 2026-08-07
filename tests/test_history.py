"""Tests for the vendored history ring, SQLite store, and reconcile recording (Plan 018).

Ported from sluice's test_history.py / test_history_store.py where the
behaviour survived the vendoring; adapted to the vendored (static, no
controller) ReconciliationLoop and switchboard's generalized HistoryEntry.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from switchboard.gate import PermitGate
from switchboard.history import History, HistoryEntry, SQLiteHistoryStore
from switchboard.limit import BreakerConfig, CachedReading, LimitState
from switchboard.reconcile import ReconciliationLoop

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeTruthSource:
    """Minimal TruthSource: serves a fixed reading, optionally stale."""

    def __init__(self, reading: LimitState) -> None:
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

    @property
    def last_cached(self) -> CachedReading | None:
        return None

    def record_response_headers(
        self, headers: dict[str, str], status: int, *, now_monotonic: float
    ) -> None:
        pass

    async def close(self) -> None:
        pass


class FailingTruthSource:
    """TruthSource whose fetch always raises (drives the fail-safe path)."""

    @property
    def last_cached(self) -> CachedReading | None:
        return None

    async def fetch(self, *, now_monotonic: float) -> CachedReading:
        raise RuntimeError("simulated fetch failure")

    def record_response_headers(
        self, headers: dict[str, str], status: int, *, now_monotonic: float
    ) -> None:
        pass

    async def close(self) -> None:
        pass


def _reading(**kw: Any) -> LimitState:
    base: dict[str, Any] = dict(concurrent_sessions=0, limit=4, hard_cap=8)
    base.update(kw)
    return LimitState(**base)


def _entry(**kw: Any) -> HistoryEntry:
    defaults: dict[str, Any] = dict(
        timestamp=1000.0,
        concurrent_sessions=0,
        local_in_flight=0,
        effective_permits=3,
        limit=4,
        hard_cap=8,
        band="normal",
        breaker="closed",
        priority_low=False,
        usage_age=0.0,
        stale=False,
        recent_429s=0,
        total_429s=0,
        queue_depth=0,
    )
    defaults.update(kw)
    return HistoryEntry(**defaults)


def _make_loop(
    initial: LimitState,
    *,
    maxlen: int = 100,
    history: History | None = None,
    history_store: SQLiteHistoryStore | None = None,
    history_ttl: float = 604800.0,
    truth: Any = None,
) -> tuple[ReconciliationLoop, FakeTruthSource, PermitGate, list[float], list[float], History]:
    m = [1000.0]
    w = [1_000_000.0]
    client = FakeTruthSource(initial)
    gate = PermitGate(initial_capacity=3, clock=lambda: m[0])
    if history is None:
        history = History(maxlen=maxlen)
    loop = ReconciliationLoop(
        truth_source=truth if truth is not None else client,
        gate=gate,
        max_concurrency=3,
        poll_interval=5.0,
        breaker_config=BreakerConfig(failure_threshold=5, cooldown_seconds=60.0),
        provider_type="umans",
        monotonic_clock=lambda: m[0],
        wall_clock=lambda: w[0],
        history=history,
        history_store=history_store,
        history_ttl=history_ttl,
    )
    return loop, client, gate, m, w, history


# ---------------------------------------------------------------------------
# HistoryEntry
# ---------------------------------------------------------------------------


def test_history_entry_to_dict_keys() -> None:
    e = _entry(concurrent_sessions=3, local_in_flight=2, effective_permits=2, usage_age=1.5)
    d = e.to_dict()
    assert d["ts"] == 1000.0
    assert d["obs"] == 3
    assert d["loc"] == 2
    assert d["ep"] == 2
    assert d["lim"] == 4
    assert d["hc"] == 8
    assert d["band"] == "normal"
    assert d["brk"] == "closed"
    assert d["pl"] is False
    assert d["age"] == 1.5
    assert d["stl"] is False
    assert d["r429"] == 0
    assert d["t429"] == 0
    assert d["rl429"] == 0
    assert d["qd"] == 0
    assert d["err"] is False
    assert d["rwin"] is None
    assert d["rlim"] is None
    assert d["rrem"] is None
    assert d["rlw"] is None
    assert d["rdelta"] is None
    assert d["tp"] == 0
    # Untracked-by-this-loop fields serialize as None, not fabricated zeros.
    assert d["t503"] is None
    assert d["qt"] is None
    # sluice's phantom estimate was dropped in the vendoring.
    assert "ph" not in d


def test_history_entry_to_dict_roundtrip_json() -> None:
    e = _entry(
        timestamp=1700000000.5,
        concurrent_sessions=None,
        effective_permits=0,
        limit=None,
        hard_cap=None,
        band="boxed",
        breaker="open",
        priority_low=True,
        usage_age=99.9,
        stale=True,
        recent_429s=5,
        total_429s=10,
        queue_depth=3,
        tick_failed=True,
    )
    d = e.to_dict()
    assert d["obs"] is None
    assert d["lim"] is None
    assert d["hc"] is None
    assert d["stl"] is True
    assert d["band"] == "boxed"
    assert d["brk"] == "open"
    assert d["pl"] is True
    assert d["err"] is True
    assert json.dumps(d)


# ---------------------------------------------------------------------------
# History ring buffer
# ---------------------------------------------------------------------------


def test_history_append_and_length() -> None:
    h = History(maxlen=10)
    assert h.length == 0
    for i in range(5):
        h.append(_entry(timestamp=1000.0 + i))
    assert h.length == 5


def test_history_evicts_oldest_when_full() -> None:
    h = History(maxlen=3)
    for i in range(5):
        h.append(_entry(timestamp=1000.0 + i))
    assert h.length == 3
    entries = h.entries()
    assert entries[0].timestamp == 1002.0
    assert entries[2].timestamp == 1004.0


def test_history_clear() -> None:
    h = History(maxlen=10)
    h.append(_entry())
    h.append(_entry())
    h.clear()
    assert h.length == 0


def test_history_maxlen_property() -> None:
    assert History(maxlen=42).maxlen == 42


def test_history_rejects_invalid_maxlen() -> None:
    with pytest.raises(ValueError):
        History(maxlen=0)
    with pytest.raises(ValueError):
        History(maxlen=-1)


def test_history_to_dict_list_and_limit() -> None:
    h = History(maxlen=10)
    h.append(_entry(timestamp=1000.0, concurrent_sessions=1))
    h.append(_entry(timestamp=1001.0, concurrent_sessions=2))
    h.append(_entry(timestamp=1002.0, concurrent_sessions=3))
    lst = h.to_dict_list()
    assert len(lst) == 3
    assert lst[0]["obs"] == 1
    assert h.to_dict_list(limit=2) == lst[-2:]
    assert h.to_dict_list(limit=0) == []
    assert h.to_dict_list(limit=-5) == []
    assert len(h.to_dict_list(limit=999)) == 3


def test_history_entries_returns_copy() -> None:
    h = History(maxlen=10)
    h.append(_entry(timestamp=1000.0))
    entries = h.entries()
    entries.clear()
    assert h.length == 1, "clearing the returned list must not affect the buffer"


# ---------------------------------------------------------------------------
# SQLiteHistoryStore
# ---------------------------------------------------------------------------


def test_store_append_and_load_recent(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    store.append(_entry(timestamp=1000.0, concurrent_sessions=1))
    store.append(_entry(timestamp=1001.0, concurrent_sessions=2))
    store.append(_entry(timestamp=1002.0, concurrent_sessions=3))
    entries = store.load_recent(10)
    assert len(entries) == 3
    assert entries[0].timestamp == 1000.0
    assert entries[0].concurrent_sessions == 1
    assert entries[2].timestamp == 1002.0
    assert entries[2].concurrent_sessions == 3
    store.close()


def test_store_load_recent_limit(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    for i in range(10):
        store.append(_entry(timestamp=1000.0 + i))
    entries = store.load_recent(3)
    assert len(entries) == 3
    assert entries[0].timestamp == 1007.0
    assert entries[2].timestamp == 1009.0
    assert store.load_recent(0) == []
    assert store.load_recent(-5) == []
    store.close()


def test_store_load_recent_empty(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    assert store.load_recent(10) == []
    store.close()


def test_store_prune(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    store.append(_entry(timestamp=1000.0))
    store.append(_entry(timestamp=2000.0))
    store.append(_entry(timestamp=3000.0))
    deleted = store.prune(ttl_seconds=500.0, now=3000.0)
    assert deleted == 2
    entries = store.load_recent(10)
    assert len(entries) == 1
    assert entries[0].timestamp == 3000.0
    # Nothing left to prune.
    assert store.prune(ttl_seconds=500.0, now=3000.0) == 0
    store.close()


def test_store_roundtrip_all_fields(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    entry = _entry(
        timestamp=1234567890.5,
        concurrent_sessions=7,
        local_in_flight=3,
        effective_permits=2,
        limit=10,
        hard_cap=20,
        band="boxed",
        breaker="open",
        priority_low=True,
        usage_age=42.5,
        stale=True,
        recent_429s=3,
        total_429s=15,
        rate_limit_429s=7,
        low_interactivity=True,
        queue_depth=5,
        requests_in_window=48,
        requests_limit=200,
        requests_remaining=152,
        throughput=9,
        tick_failed=True,
    )
    store.append(entry)
    entries = store.load_recent(1)
    assert len(entries) == 1
    assert entries[0] == entry
    store.close()


def test_store_null_fields_roundtrip(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    store.append(_entry(concurrent_sessions=None, limit=None, hard_cap=None))
    e = store.load_recent(1)[0]
    assert e.concurrent_sessions is None
    assert e.limit is None
    assert e.hard_cap is None
    assert e.total_503s is None
    assert e.queue_timeouts is None
    assert e.local_requests_in_window is None
    assert e.request_window_delta is None
    store.close()


def test_store_fail_safe_on_unopenable_path() -> None:
    store = SQLiteHistoryStore("/nonexistent_dir/missing/deep/path.db")
    assert store.is_available is False
    store.append(_entry(timestamp=1000.0))
    assert store.load_recent(10) == []
    assert store.prune(ttl_seconds=100.0, now=2000.0) == 0
    store.close()


def test_store_corrupt_db_file(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"NOT A SQLITE DATABASE")
    store = SQLiteHistoryStore(str(path))
    store.append(_entry(timestamp=1000.0))
    assert store.load_recent(10) == []
    store.close()


def test_store_locked_db_append_fail_safe(tmp_path: Path) -> None:
    """A write that hits a locked database logs and drops the entry, never raises."""
    path = tmp_path / "locked.db"
    store = SQLiteHistoryStore(str(path))
    assert store.is_available
    # Hold an exclusive lock from a second connection.
    blocker = sqlite3.connect(str(path), isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        store._conn.execute("PRAGMA busy_timeout=100")  # type: ignore[union-attr]
        store.append(_entry(timestamp=1000.0))  # must not raise
        assert store.prune(ttl_seconds=1.0, now=2000.0) == 0  # must not raise
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    store.close()


def test_store_close_idempotent(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    store.append(_entry(timestamp=1000.0))
    store.close()
    assert store.is_available is False
    store.close()
    store.append(_entry(timestamp=1001.0))
    assert store.load_recent(10) == []


def test_store_reopened_db_has_data(tmp_path: Path) -> None:
    path = str(tmp_path / "persist.db")
    store1 = SQLiteHistoryStore(path)
    store1.append(_entry(timestamp=1000.0, concurrent_sessions=1))
    store1.append(_entry(timestamp=1001.0, concurrent_sessions=2))
    store1.close()

    store2 = SQLiteHistoryStore(path)
    entries = store2.load_recent(10)
    assert len(entries) == 2
    assert entries[0].concurrent_sessions == 1
    assert entries[1].concurrent_sessions == 2
    store2.close()


def test_store_duplicate_timestamp_ordering(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "h.db"))
    store.append(_entry(timestamp=1000.0, concurrent_sessions=1))
    store.append(_entry(timestamp=1000.0, concurrent_sessions=2))
    store.append(_entry(timestamp=1000.0, concurrent_sessions=3))
    entries = store.load_recent(3)
    assert [e.concurrent_sessions for e in entries] == [1, 2, 3]
    store.close()


# ---------------------------------------------------------------------------
# ReconciliationLoop recording
# ---------------------------------------------------------------------------


async def test_tick_records_entry_to_ring_and_store(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "r.db"))
    loop, _client, _gate, _m, w, history = _make_loop(
        _reading(concurrent_sessions=0), history_store=store
    )
    await loop.tick()
    assert history.length == 1
    entry = history.entries()[0]
    assert entry.timestamp == w[0]
    assert entry.concurrent_sessions == 0
    assert entry.effective_permits == 3
    assert entry.band == "normal"
    assert entry.breaker == "closed"
    assert entry.stale is False
    assert entry.limit == 4
    assert entry.hard_cap == 8
    # Fields the vendored loop does not track are honestly None.
    assert entry.total_503s is None
    assert entry.queue_timeouts is None
    assert entry.local_requests_in_window is None
    assert entry.request_window_delta is None
    stored = store.load_recent(10)
    assert len(stored) == 1
    assert stored[0] == entry
    store.close()


async def test_multiple_ticks_accumulate_and_evict() -> None:
    loop, _client, _gate, m, w, history = _make_loop(
        _reading(concurrent_sessions=2), maxlen=3
    )
    for _ in range(5):
        m[0] += 5
        w[0] += 5
        await loop.tick()
    assert history.length == 3
    entries = history.entries()
    assert entries[0].timestamp < entries[-1].timestamp


async def test_history_records_stale_reading() -> None:
    loop, client, _gate, m, w, history = _make_loop(_reading(concurrent_sessions=0))
    await loop.tick()
    assert history.entries()[0].stale is False

    client.set_fail(True)
    m[0] += 100
    w[0] += 100
    await loop.tick()
    entry = history.entries()[-1]
    assert entry.stale is True
    # Reading-derived fields are not fabricated from the LKG on stale ticks.
    assert entry.concurrent_sessions is None
    assert entry.limit is None
    assert entry.hard_cap is None


async def test_history_records_breaker_and_429s() -> None:
    loop, _client, _gate, _m, _w, history = _make_loop(_reading(concurrent_sessions=0))
    for _ in range(5):
        loop.record_429()
    await loop.tick()
    entry = history.entries()[-1]
    assert entry.breaker == "open"
    assert entry.recent_429s == 5
    assert entry.total_429s == 5
    assert entry.effective_permits == 0


async def test_history_records_low_interactivity_band() -> None:
    loop, _client, _gate, _m, _w, history = _make_loop(
        _reading(
            concurrent_sessions=0,
            service_mode="low_interactivity",
            service_mode_resets_at_epoch=1_000_500.0,  # > wall clock 1_000_000
        )
    )
    await loop.tick()
    entry = history.entries()[0]
    assert entry.low_interactivity is True
    assert entry.band == "low_interactivity"


async def test_no_history_when_not_configured() -> None:
    m = [1000.0]
    client = FakeTruthSource(_reading(concurrent_sessions=0))
    gate = PermitGate(initial_capacity=3, clock=lambda: m[0])
    loop = ReconciliationLoop(
        truth_source=client,
        gate=gate,
        max_concurrency=3,
        monotonic_clock=lambda: m[0],
    )
    await loop.tick()
    assert loop.history is None
    assert loop.history_store is None
    assert gate.capacity == 3


async def test_history_property_returns_ring() -> None:
    loop, _client, _gate, _m, _w, history = _make_loop(_reading(concurrent_sessions=0))
    assert loop.history is history
    await loop.tick()
    assert loop.history is not None
    assert loop.history.length == 1


async def test_run_records_fail_safe_entry_on_tick_exception(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "f.db"))
    loop, _client, gate, _m, _w, history = _make_loop(
        _reading(concurrent_sessions=0),
        history_store=store,
        truth=FailingTruthSource(),
    )
    loop._poll_interval = 0.01

    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert history.length >= 1
    entry = history.entries()[-1]
    assert entry.tick_failed is True
    assert entry.effective_permits == 0
    assert entry.stale is True
    assert entry.band == "normal"  # no prior reading — honest default
    assert gate.capacity == 0

    stored = store.load_recent(100)
    assert any(e.tick_failed for e in stored)
    store.close()


async def test_fail_safe_entry_uses_prior_reading() -> None:
    class FailingAfterFirst:
        def __init__(self, inner: FakeTruthSource) -> None:
            self._inner = inner
            self._calls = 0

        @property
        def last_cached(self) -> CachedReading | None:
            return None

        async def fetch(self, *, now_monotonic: float) -> CachedReading:
            self._calls += 1
            if self._calls > 1:
                raise RuntimeError("fetch failure after first tick")
            return await self._inner.fetch(now_monotonic=now_monotonic)

        def record_response_headers(
            self, headers: dict[str, str], status: int, *, now_monotonic: float
        ) -> None:
            pass

        async def close(self) -> None:
            pass

    inner = FakeTruthSource(_reading(concurrent_sessions=2, limit=4, hard_cap=8))
    loop, _client, _gate, _m, _w, history = _make_loop(
        _reading(), truth=FailingAfterFirst(inner)
    )
    loop._poll_interval = 0.01

    await loop.tick()
    assert history.entries()[0].concurrent_sessions == 2

    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    failed = [e for e in history.entries() if e.tick_failed]
    assert failed
    e = failed[-1]
    assert e.concurrent_sessions == 2
    assert e.limit == 4
    assert e.hard_cap == 8
    assert e.effective_permits == 0
    assert e.stale is True


async def test_prune_called_during_run(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "p.db"))
    for i in range(100):
        store.append(_entry(timestamp=1000.0 + i))

    loop, _client, _gate, _m, w, _history = _make_loop(
        _reading(concurrent_sessions=0),
        history_store=store,
        history_ttl=10.0,
    )
    loop._poll_interval = 0.005
    w[0] = 100_000.0

    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.8)  # > 60 ticks at 5 ms → prune fires
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    entries = store.load_recent(1000)
    assert entries, "run() should have recorded fresh entries"
    for e in entries:
        assert e.timestamp >= 100_000.0 - 10.0, "old entries must be pruned by TTL"
    store.close()


async def test_store_failure_degrades_gracefully() -> None:
    store = SQLiteHistoryStore("/nonexistent/deep/path.db")
    assert store.is_available is False
    loop, _client, gate, _m, _w, history = _make_loop(
        _reading(concurrent_sessions=0), history_store=store
    )
    await loop.tick()
    assert history.length == 1
    assert gate.capacity == 3
    store.close()


async def test_stop_closes_history_store(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(str(tmp_path / "s.db"))
    loop, _client, _gate, _m, _w, _history = _make_loop(
        _reading(concurrent_sessions=0), history_store=store
    )
    await loop.tick()
    await loop.stop()
    assert store.is_available is False
