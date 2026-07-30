from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sluice.usage import CachedReading

from switchboard.dashboard import DashboardTruthSource, _reading_to_limit_state


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _reading_payload(
    provider: str = "ollama",
    session_percent: float = 30.0,
    timestamp: str | None = None,
) -> list[dict]:
    ts = datetime.now(tz=UTC).isoformat() if timestamp is None else timestamp
    return [{"provider": provider, "session_percent": session_percent, "timestamp": ts}]


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
    payload = [
        {
            "provider": "other",
            "session_percent": 10.0,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
    ]
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
async def test_fetch_stale_timestamp_returns_ok_false() -> None:
    old_ts = (datetime.now(tz=UTC) - timedelta(hours=2)).isoformat()
    payload = _reading_payload(session_percent=10.0, timestamp=old_ts)
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
async def test_fetch_http_error_serves_lkg() -> None:
    fresh_ts = datetime.now(tz=UTC).isoformat()
    payload = _reading_payload(session_percent=20.0, timestamp=fresh_ts)
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
    fresh_ts = datetime.now(tz=UTC).isoformat()
    payload = _reading_payload(session_percent=25.0, timestamp=fresh_ts)
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
