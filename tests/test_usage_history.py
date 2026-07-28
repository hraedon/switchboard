"""Tests for the usage-history tracker and /admin/usage-history endpoint."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from switchboard.usage_history import (
    PenaltyTokenSummary,
    UsageHistoryTracker,
    _sum_buckets,
)


class TestSumBuckets:
    def test_empty(self) -> None:
        assert _sum_buckets([]) == {"tin": 0, "tout": 0, "reqs": 0, "total": 0}

    def test_single_bucket(self) -> None:
        buckets = [{"tokens_in": 100, "tokens_out": 200, "requests": 5}]
        result = _sum_buckets(buckets)
        assert result["tin"] == 100
        assert result["tout"] == 200
        assert result["total"] == 300
        assert result["reqs"] == 5

    def test_multiple_buckets(self) -> None:
        buckets = [
            {"tokens_in": 100, "tokens_out": 200, "requests": 5},
            {"tokens_in": 50, "tokens_out": 150, "requests": 3},
            {"tokens_in": 0, "tokens_out": 0, "requests": 0},
        ]
        result = _sum_buckets(buckets)
        assert result["tin"] == 150
        assert result["tout"] == 350
        assert result["total"] == 500
        assert result["reqs"] == 8

    def test_missing_fields(self) -> None:
        buckets = [{"tokens_in": 100}, {}]
        result = _sum_buckets(buckets)
        assert result["tin"] == 100
        assert result["tout"] == 0
        assert result["total"] == 100

    def test_non_dict_entries(self) -> None:
        buckets = [{"tokens_in": 10, "tokens_out": 20, "requests": 1}, "bad", None]
        result = _sum_buckets(buckets)
        assert result["tin"] == 10
        assert result["tout"] == 20
        assert result["total"] == 30


class TestUsageHistoryTracker:
    def test_register_and_has_provider(self) -> None:
        tracker = UsageHistoryTracker()
        assert not tracker.has_provider("umans")
        tracker.register(
            "umans",
            base_url="https://api.example.com",
            api_key="test-key",
        )
        assert tracker.has_provider("umans")
        assert not tracker.has_provider("ollama")

    def test_snapshot_returns_none_for_unregistered(self) -> None:
        tracker = UsageHistoryTracker()
        assert tracker.snapshot("unknown") is None

    def test_snapshot_returns_default_for_registered(self) -> None:
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans",
            base_url="https://api.example.com",
            api_key="test-key",
        )
        snap = tracker.snapshot("umans")
        assert snap is not None
        assert snap.tokens_24h is None
        assert snap.penalty is None

    def test_status_dict_returns_none_for_unregistered(self) -> None:
        tracker = UsageHistoryTracker()
        assert tracker.status_dict("unknown") is None

    def test_status_dict_returns_data_for_registered(self) -> None:
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans",
            base_url="https://api.example.com",
            api_key="test-key",
        )
        snap = tracker.snapshot("umans")
        assert snap is not None
        snap.tokens_24h = 5000000
        snap.tokens_24h_in = 2000000
        snap.tokens_24h_out = 3000000
        snap.tokens_24h_requests = 42
        snap.last_refresh = time.time()

        d = tracker.status_dict("umans")
        assert d is not None
        assert d["tokens_24h"] == 5000000
        assert d["tokens_24h_in"] == 2000000
        assert d["tokens_24h_out"] == 3000000
        assert d["tokens_24h_requests"] == 42
        assert "penalty" not in d

    def test_status_dict_includes_penalty(self) -> None:
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans",
            base_url="https://api.example.com",
            api_key="test-key",
        )
        snap = tracker.snapshot("umans")
        assert snap is not None
        snap.tokens_24h = 1000000
        snap.penalty = PenaltyTokenSummary(
            penalty_started_at=time.time() - 3600,
            before_total=2000000,
            before_tokens_in=800000,
            before_tokens_out=1200000,
            before_requests=10,
            since_total=500000,
            since_tokens_in=200000,
            since_tokens_out=300000,
            since_requests=3,
        )

        d = tracker.status_dict("umans")
        assert d is not None
        assert "penalty" in d
        pen = d["penalty"]
        assert pen["before_total"] == 2000000
        assert pen["since_total"] == 500000
        assert pen["before_requests"] == 10
        assert pen["since_requests"] == 3


class TestUsageHistoryRefresh:
    """Test the async refresh method with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_refresh_fetches_24h_total(self) -> None:
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans",
            base_url="https://api.example.com",
            api_key="test-key",
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "buckets": [
                {"tokens_in": 100, "tokens_out": 200, "requests": 5},
                {"tokens_in": 50, "tokens_out": 150, "requests": 3},
            ]
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        tracker._clients["umans"] = mock_client

        snap = await tracker.refresh("umans", penalty_started_at=None)
        assert snap is not None
        assert snap.tokens_24h == 500
        assert snap.tokens_24h_in == 150
        assert snap.tokens_24h_out == 350
        assert snap.tokens_24h_requests == 8
        assert snap.last_error is None
        assert snap.penalty is None

    @pytest.mark.asyncio
    async def test_refresh_handles_error(self) -> None:
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans",
            base_url="https://api.example.com",
            api_key="test-key",
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        tracker._clients["umans"] = mock_client

        snap = await tracker.refresh("umans", penalty_started_at=None)
        assert snap is not None
        assert snap.tokens_24h is None
        assert snap.last_error is not None
        assert "ConnectError" in snap.last_error

    @pytest.mark.asyncio
    async def test_refresh_with_penalty_fetches_before_and_since(self) -> None:
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans",
            base_url="https://api.example.com",
            api_key="test-key",
        )

        penalty_start = time.time() - 3600

        # First call: 24h total
        # Second call: 24h before penalty
        # Third call: since penalty
        responses = [
            MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={
                    "buckets": [{"tokens_in": 100, "tokens_out": 200, "requests": 5}]
                })
            ),
            MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={
                    "buckets": [{"tokens_in": 1000, "tokens_out": 2000, "requests": 10}]
                })
            ),
            MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={
                    "buckets": [{"tokens_in": 500, "tokens_out": 700, "requests": 3}]
                })
            ),
        ]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=responses)
        tracker._clients["umans"] = mock_client

        snap = await tracker.refresh("umans", penalty_started_at=penalty_start)
        assert snap is not None
        assert snap.tokens_24h == 300
        assert snap.penalty is not None
        assert snap.penalty.before_total == 3000
        assert snap.penalty.since_total == 1200
        assert snap.penalty.before_requests == 10
        assert snap.penalty.since_requests == 3

    @pytest.mark.asyncio
    async def test_refresh_skips_penalty_when_none(self) -> None:
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans",
            base_url="https://api.example.com",
            api_key="test-key",
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"buckets": []}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        tracker._clients["umans"] = mock_client

        snap = await tracker.refresh("umans", penalty_started_at=None)
        assert snap is not None
        assert snap.penalty is None

    @pytest.mark.asyncio
    async def test_refresh_returns_none_for_unregistered(self) -> None:
        tracker = UsageHistoryTracker()
        result = await tracker.refresh("unknown", penalty_started_at=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_refresh_caches_before_total_across_calls(self) -> None:
        """The 24h-before total is immutable — fetched once per penalty event."""
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans",
            base_url="https://api.example.com",
            api_key="test-key",
        )

        penalty_start = time.time() - 3600

        # First refresh: 3 calls (24h, before, since)
        responses_1 = [
            MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={"buckets": [
                    {"tokens_in": 10, "tokens_out": 20, "requests": 1}
                ]})
            ),
            MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={"buckets": [
                    {"tokens_in": 100, "tokens_out": 200, "requests": 5}
                ]})
            ),
            MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={"buckets": [
                    {"tokens_in": 50, "tokens_out": 60, "requests": 2}
                ]})
            ),
        ]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=responses_1)
        tracker._clients["umans"] = mock_client

        snap1 = await tracker.refresh("umans", penalty_started_at=penalty_start)
        assert snap1 is not None
        assert snap1.penalty is not None
        assert snap1.penalty.before_total == 300

        # Reset call count by creating new mock
        call_count = 0

        async def mock_get(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.raise_for_status = MagicMock()
            if call_count == 1:
                # 24h total
                r.json.return_value = {"buckets": [
                    {"tokens_in": 15, "tokens_out": 25, "requests": 2}
                ]}
            else:
                # Since-penalty (before should NOT be re-fetched)
                r.json.return_value = {"buckets": [
                    {"tokens_in": 55, "tokens_out": 65, "requests": 3}
                ]}
            return r

        mock_client2 = AsyncMock()
        mock_client2.get = mock_get
        tracker._clients["umans"] = mock_client2

        # Force refresh by resetting last_refresh
        snap1.last_refresh = 0

        snap2 = await tracker.refresh("umans", penalty_started_at=penalty_start)
        assert snap2 is not None
        assert snap2.penalty is not None
        # Before total should be cached from first fetch
        assert snap2.penalty.before_total == 300
        # Since total should be updated
        assert snap2.penalty.since_total == 120
        # Only 2 calls (24h + since), not 3 (before was cached)
        assert call_count == 2


class TestUtilization:
    """Plan 013: routing utilization from the trailing-24h total + cap."""

    def test_no_cap_returns_none(self) -> None:
        tracker = UsageHistoryTracker()
        tracker.register("umans", base_url="https://api.example.com", api_key="k")
        assert tracker.utilization("umans") is None

    def test_no_data_returns_none(self) -> None:
        """Fail safe: cap configured but no successful fetch yet → None."""
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans", base_url="https://api.example.com", api_key="k",
            cap_tokens=1_000_000,
        )
        assert tracker.utilization("umans") is None

    def test_unregistered_returns_none(self) -> None:
        tracker = UsageHistoryTracker()
        assert tracker.utilization("umans") is None

    def test_utilization_computed(self) -> None:
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans", base_url="https://api.example.com", api_key="k",
            cap_tokens=1_000_000,
        )
        snap = tracker.snapshot("umans")
        assert snap is not None
        snap.tokens_24h = 850_000
        assert tracker.utilization("umans") == 0.85

    def test_utilization_can_exceed_one(self) -> None:
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans", base_url="https://api.example.com", api_key="k",
            cap_tokens=1_000_000,
        )
        snap = tracker.snapshot("umans")
        assert snap is not None
        snap.tokens_24h = 1_200_000
        assert tracker.utilization("umans") == 1.2

    def test_zero_or_negative_cap_treated_as_unset(self) -> None:
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans", base_url="https://api.example.com", api_key="k",
            cap_tokens=0,
        )
        snap = tracker.snapshot("umans")
        assert snap is not None
        snap.tokens_24h = 500
        assert tracker.utilization("umans") is None


class TestUsageHistoryFailureHandling:
    """Failure semantics: retries, backoff, and no permanent latch."""

    @pytest.mark.asyncio
    async def test_before_total_not_latched_on_failure(self) -> None:
        """A failed before-fetch must NOT record 0 — it stays None so the
        next refresh retries (a latched 0 would permanently misreport the
        penalty window)."""
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans", base_url="https://api.example.com", api_key="k",
        )
        penalty_start = time.time() - 3600

        call_count = 0

        async def mock_get(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                # before-fetch fails
                raise httpx.ConnectError("refused")
            r = MagicMock()
            r.raise_for_status = MagicMock()
            r.json.return_value = {"buckets": [
                {"tokens_in": 10, "tokens_out": 20, "requests": 1}
            ]}
            return r

        mock_client = AsyncMock()
        mock_client.get = mock_get
        tracker._clients["umans"] = mock_client

        snap = await tracker.refresh("umans", penalty_started_at=penalty_start)
        assert snap is not None
        assert snap.penalty is not None
        # Failed before-fetch: still None, not latched to 0
        assert snap.penalty.before_total is None
        # since-fetch still succeeded
        assert snap.penalty.since_total == 30

        # Next refresh retries the before-fetch and succeeds (force the
        # refresh past the 5-minute success throttle)
        snap.last_refresh = 0
        snap2 = await tracker.refresh("umans", penalty_started_at=penalty_start)
        assert snap2 is not None
        assert snap2.penalty is not None
        assert snap2.penalty.before_total == 30

    @pytest.mark.asyncio
    async def test_failure_throttle_backs_off(self) -> None:
        """After a failed refresh, further attempts are throttled for
        _ERROR_RETRY_INTERVAL so a down endpoint is not hammered."""
        from switchboard.usage_history import _ERROR_RETRY_INTERVAL

        tracker = UsageHistoryTracker()
        tracker.register(
            "umans", base_url="https://api.example.com", api_key="k",
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        tracker._clients["umans"] = mock_client

        snap1 = await tracker.refresh("umans", penalty_started_at=None)
        assert snap1 is not None
        assert snap1.last_error is not None
        assert mock_client.get.call_count == 1

        # Immediate retry: throttled, no HTTP call
        snap2 = await tracker.refresh("umans", penalty_started_at=None)
        assert snap2 is not None
        assert mock_client.get.call_count == 1

        # After the retry interval: retries
        tracker._last_attempt["umans"] -= _ERROR_RETRY_INTERVAL + 1
        snap3 = await tracker.refresh("umans", penalty_started_at=None)
        assert snap3 is not None
        assert mock_client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_success_served_from_cache(self) -> None:
        """A provider with fresh successful data is served from cache without
        a new HTTP call."""
        tracker = UsageHistoryTracker()
        tracker.register(
            "umans", base_url="https://api.example.com", api_key="k",
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"buckets": [
            {"tokens_in": 10, "tokens_out": 20, "requests": 1}
        ]}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        tracker._clients["umans"] = mock_client

        snap1 = await tracker.refresh("umans", penalty_started_at=None)
        assert snap1 is not None and snap1.tokens_24h == 30

        # Within the refresh interval: cached, no new HTTP call
        snap2 = await tracker.refresh("umans", penalty_started_at=None)
        assert snap2 is not None
        assert mock_client.get.call_count == 1


class TestUsageHistoryEndpoint:
    """Test the /admin/usage-history admin endpoint."""

    @pytest.mark.asyncio
    async def test_requires_auth(self) -> None:
        from switchboard.admin import handle_usage_history

        send_calls: list[dict[str, Any]] = []

        async def mock_send(msg: dict[str, Any]) -> None:
            send_calls.append(msg)

        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "query_string": b"provider=umans&from=2025-01-01T00:00:00Z&to=2025-01-02T00:00:00Z",
            "headers": [],
        }

        await handle_usage_history(
            mock_send, scope, "secret-token", {}, None,
        )

        assert send_calls[0]["status"] == 401

    @pytest.mark.asyncio
    async def test_missing_provider_param(self) -> None:
        from switchboard.admin import handle_usage_history

        send_calls: list[dict[str, Any]] = []

        async def mock_send(msg: dict[str, Any]) -> None:
            send_calls.append(msg)

        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "query_string": b"from=2025-01-01T00:00:00Z&to=2025-01-02T00:00:00Z",
            "headers": [(b"authorization", b"Bearer secret-token")],
        }

        await handle_usage_history(
            mock_send, scope, "secret-token", {}, None,
        )

        assert send_calls[0]["status"] == 400

    @pytest.mark.asyncio
    async def test_unknown_provider(self) -> None:
        from switchboard.admin import handle_usage_history

        send_calls: list[dict[str, Any]] = []

        async def mock_send(msg: dict[str, Any]) -> None:
            send_calls.append(msg)

        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "query_string": b"provider=unknown&from=x&to=y",
            "headers": [(b"authorization", b"Bearer secret-token")],
        }

        await handle_usage_history(
            mock_send, scope, "secret-token", {}, None,
        )

        assert send_calls[0]["status"] == 404

    @pytest.mark.asyncio
    async def test_missing_from_to_params(self) -> None:
        from switchboard.admin import handle_usage_history
        from switchboard.providers import ProviderContext

        send_calls: list[dict[str, Any]] = []

        async def mock_send(msg: dict[str, Any]) -> None:
            send_calls.append(msg)

        ctx = MagicMock(spec=ProviderContext)
        ctx.usage_base_url = "https://api.example.com"
        ctx.usage_api_key = "test-key"
        ctx.usage_auth_header = "authorization"

        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "query_string": b"provider=umans",
            "headers": [(b"authorization", b"Bearer secret-token")],
        }

        await handle_usage_history(
            mock_send, scope, "secret-token", {"umans": ctx}, None,
        )

        assert send_calls[0]["status"] == 400

    @pytest.mark.asyncio
    async def test_proxies_to_upstream(self) -> None:
        from switchboard.admin import handle_usage_history
        from switchboard.providers import ProviderContext

        send_calls: list[dict[str, Any]] = []

        async def mock_send(msg: dict[str, Any]) -> None:
            send_calls.append(msg)

        ctx = MagicMock(spec=ProviderContext)
        ctx.usage_base_url = "https://api.example.com"
        ctx.usage_api_key = "test-key"
        ctx.usage_auth_header = "authorization"

        mock_response_data = {
            "buckets": [
                {"tokens_in": 100, "tokens_out": 200, "requests": 5}
            ]
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = mock_response_data
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            scope: dict[str, Any] = {
                "type": "http",
                "method": "GET",
                "query_string": b"provider=umans&from=2025-01-01T00:00:00Z&to=2025-01-02T00:00:00Z",
                "headers": [(b"authorization", b"Bearer secret-token")],
            }

            await handle_usage_history(
                mock_send, scope, "secret-token", {"umans": ctx}, None,
            )

        assert send_calls[0]["status"] == 200
        body = json.loads(send_calls[1]["body"].decode())
        assert body == mock_response_data

    @pytest.mark.asyncio
    async def test_handles_upstream_error(self) -> None:
        from switchboard.admin import handle_usage_history
        from switchboard.providers import ProviderContext

        send_calls: list[dict[str, Any]] = []

        async def mock_send(msg: dict[str, Any]) -> None:
            send_calls.append(msg)

        ctx = MagicMock(spec=ProviderContext)
        ctx.usage_base_url = "https://api.example.com"
        ctx.usage_api_key = "test-key"
        ctx.usage_auth_header = "authorization"

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            scope: dict[str, Any] = {
                "type": "http",
                "method": "GET",
                "query_string": b"provider=umans&from=2025-01-01T00:00:00Z&to=2025-01-02T00:00:00Z",
                "headers": [(b"authorization", b"Bearer secret-token")],
            }

            await handle_usage_history(
                mock_send, scope, "secret-token", {"umans": ctx}, None,
            )

        assert send_calls[0]["status"] == 502
