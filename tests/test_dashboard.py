from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from switchboard.dashboard import DashboardTruthSource, _reading_to_limit_state
from switchboard.limit import CachedReading

# Recorded usage-dashboard /readings payload (WI-001): serialized by the
# usage-dashboard `Reading.to_dict()` serializer (models.py), which is exactly
# what the live endpoint emits. Regenerate from usage-dashboard when the
# reading schema changes — the contract tests below fail if it drifts.
_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "readings.json"

# Keys DashboardTruthSource reads out of a reading dict. The contract test
# asserts every one of these is present in the recorded fixture.
_READ_KEYS = ("provider", "session_percent", "session_resets_at", "fetched_at", "stale")

# The canonical key set usage-dashboard's Reading.to_dict() emits. Exact match
# is the drift guard: a renamed/added field fails this test.
_EXPECTED_READING_KEYS = {
    "provider",
    "status",
    "session_percent",
    "session_resets_at",
    "weekly_percent",
    "weekly_resets_at",
    "fetched_at",
    "stale",
    "detail",
    "models",
    "throttle",
    "alert",
    "scoped_limits",
}


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _load_readings_fixture() -> list[dict]:
    return json.loads(_FIXTURE_PATH.read_text())


def _reading_payload(
    provider: str = "ollama",
    session_percent: float = 30.0,
    fetched_at: str | None = None,
    stale: bool = False,
) -> list[dict]:
    """The recorded /readings payload with one provider's reading adapted for
    the scenario. `fetched_at` defaults to a fresh timestamp so the reading is
    ok=True unless a test explicitly ages it."""
    payload = _load_readings_fixture()
    target = next((r for r in payload if r.get("provider") == provider), None)
    if target is None:
        return payload
    adapted = dict(target)
    adapted["session_percent"] = session_percent
    adapted["fetched_at"] = fetched_at if fetched_at is not None else _fresh_iso()
    adapted["stale"] = stale
    return [adapted]


def _fresh_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.asyncio
async def test_fetch_successful_response_normalizes_to_limit_state() -> None:
    payload = _reading_payload(session_percent=40.0)
    transport = _mock_transport(lambda req: httpx.Response(200, text=json.dumps(payload)))
    ts = DashboardTruthSource(
        dashboard_url="https://dashboard.example.com",
        bearer_token="tok",
        provider_name="ollama",
    )
    ts._client = httpx.AsyncClient(transport=transport)
    cached = await ts.fetch(now_monotonic=0.0)
    assert cached.ok is True
    assert cached.reading.provider == "ollama"
    assert cached.reading.concurrent_sessions == 0
    assert cached.reading.requests_remaining == 60
    assert cached.reading.requests_limit == 100
    await ts.close()


@pytest.mark.asyncio
async def test_fetch_no_matching_provider_returns_fail_safe_reading() -> None:
    payload = [r for r in _load_readings_fixture() if r.get("provider") != "ollama"]
    transport = _mock_transport(lambda req: httpx.Response(200, text=json.dumps(payload)))
    ts = DashboardTruthSource(
        dashboard_url="https://dashboard.example.com",
        bearer_token="tok",
        provider_name="ollama",
    )
    ts._client = httpx.AsyncClient(transport=transport)
    cached = await ts.fetch(now_monotonic=0.0)
    assert cached.ok is False
    await ts.close()


@pytest.mark.asyncio
async def test_fetch_stale_fetched_at_returns_ok_false() -> None:
    old_ts = (datetime.now(tz=UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = _reading_payload(session_percent=10.0, fetched_at=old_ts)
    transport = _mock_transport(lambda req: httpx.Response(200, text=json.dumps(payload)))
    ts = DashboardTruthSource(
        dashboard_url="https://dashboard.example.com",
        bearer_token="tok",
        provider_name="ollama",
        stale_ttl=900.0,
    )
    ts._client = httpx.AsyncClient(transport=transport)
    cached = await ts.fetch(now_monotonic=0.0)
    assert cached.ok is False
    await ts.close()


@pytest.mark.asyncio
async def test_fetch_dashboard_stale_flag_returns_ok_false() -> None:
    """A reading the dashboard itself flags stale is uncertain even when
    fetched_at is recent (its last provider fetch failed, so the percentage
    fields are not current)."""
    payload = _reading_payload(session_percent=20.0, fetched_at=_fresh_iso(), stale=True)
    transport = _mock_transport(lambda req: httpx.Response(200, text=json.dumps(payload)))
    ts = DashboardTruthSource(
        dashboard_url="https://dashboard.example.com",
        bearer_token="tok",
        provider_name="ollama",
    )
    ts._client = httpx.AsyncClient(transport=transport)
    cached = await ts.fetch(now_monotonic=0.0)
    assert cached.ok is False
    await ts.close()


@pytest.mark.asyncio
async def test_fetch_http_error_serves_lkg() -> None:
    fresh_ts = _fresh_iso()
    payload = _reading_payload(session_percent=20.0, fetched_at=fresh_ts)
    call_count = 0

    def handler(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, text=json.dumps(payload))
        return httpx.Response(500, text="error")

    transport = _mock_transport(handler)
    ts = DashboardTruthSource(
        dashboard_url="https://dashboard.example.com",
        bearer_token="tok",
        provider_name="ollama",
    )
    ts._client = httpx.AsyncClient(transport=transport)
    first = await ts.fetch(now_monotonic=0.0)
    assert first.ok is True
    assert ts.last_cached is not None
    second = await ts.fetch(now_monotonic=1.0)
    assert second.ok is False
    assert second.reading.requests_remaining == 80
    await ts.close()


@pytest.mark.asyncio
async def test_fetch_no_lkg_serves_fail_safe_reading() -> None:
    transport = _mock_transport(lambda req: httpx.Response(500, text="error"))
    ts = DashboardTruthSource(
        dashboard_url="https://dashboard.example.com",
        bearer_token="tok",
        provider_name="ollama",
    )
    ts._client = httpx.AsyncClient(transport=transport)
    cached = await ts.fetch(now_monotonic=0.0)
    assert cached.ok is False
    assert cached.reading.concurrent_sessions == 2
    assert cached.reading.requests_remaining is None
    assert cached.reading.requests_limit is None
    await ts.close()


@pytest.mark.asyncio
async def test_last_cached_returns_last_successful_reading() -> None:
    payload = _reading_payload(session_percent=25.0, fetched_at=_fresh_iso())
    transport = _mock_transport(lambda req: httpx.Response(200, text=json.dumps(payload)))
    ts = DashboardTruthSource(
        dashboard_url="https://dashboard.example.com",
        bearer_token="tok",
        provider_name="ollama",
    )
    ts._client = httpx.AsyncClient(transport=transport)
    await ts.fetch(now_monotonic=0.0)
    assert ts.last_cached is not None
    assert ts.last_cached.ok is True
    assert isinstance(ts.last_cached, CachedReading)
    await ts.close()


@pytest.mark.asyncio
async def test_close_closes_httpx_client() -> None:
    transport = _mock_transport(lambda req: httpx.Response(200, text="[]"))
    ts = DashboardTruthSource(
        dashboard_url="https://dashboard.example.com",
        bearer_token="tok",
    )
    ts._client = httpx.AsyncClient(transport=transport)
    await ts.close()
    assert ts._client.is_closed


def test_record_response_headers_is_noop() -> None:
    ts = DashboardTruthSource(
        dashboard_url="https://dashboard.example.com",
        bearer_token="tok",
    )
    ts.record_response_headers({"x-foo": "bar"}, 200, now_monotonic=0.0)


# -- cross-repo contract (WI-001) -------------------------------------------


def test_contract_recorded_payload_has_every_key_dashboard_reads() -> None:
    """The recorded /readings fixture must carry every key DashboardTruthSource
    reads. If usage-dashboard renames a field, this fails instead of silently
    degrading every reading to stale."""
    for reading in _load_readings_fixture():
        missing = [k for k in _READ_KEYS if k not in reading]
        assert missing == [], (
            f"recorded /readings fixture {reading.get('provider')!r} missing keys: {missing}"
        )


def test_contract_recorded_payload_matches_serializer_key_set() -> None:
    """Every fixture reading's key set must equal usage-dashboard
    Reading.to_dict()'s output exactly, so a renamed or added field is caught
    rather than assumed away by a hand-written test dict."""
    for reading in _load_readings_fixture():
        assert set(reading) == _EXPECTED_READING_KEYS, reading.get("provider")


def test_contract_recorded_payload_has_no_timestamp_key() -> None:
    """usage-dashboard emits 'fetched_at', never 'timestamp'. Pin that so the
    timestamp-lookup bug cannot silently resurface."""
    for reading in _load_readings_fixture():
        assert "timestamp" not in reading, reading.get("provider")


def test_reading_to_limit_state_maps_session_resets_at_to_bucket_reset_epoch() -> None:
    """Plan 016: dashboard's session_resets_at ISO timestamp maps to bucket_reset_epoch."""
    ts = (datetime.now(tz=UTC) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reading = {
        "provider": "zai",
        "session_percent": 30.0,
        "session_resets_at": ts,
    }
    limit_state = _reading_to_limit_state(reading, "zai")
    assert limit_state.bucket_reset_epoch is not None
    expected_epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    assert abs(limit_state.bucket_reset_epoch - expected_epoch) < 1.0


def test_reading_to_limit_state_absent_session_resets_at_is_none() -> None:
    """Missing session_resets_at → bucket_reset_epoch is None (fail safe)."""
    reading = {
        "provider": "zai",
        "session_percent": 30.0,
    }
    limit_state = _reading_to_limit_state(reading, "zai")
    assert limit_state.bucket_reset_epoch is None
