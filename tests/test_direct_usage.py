"""Tests for direct usage solicitation (standalone mode, no usage-dashboard)."""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest

from switchboard.direct_usage import (
    DirectUsageParseError,
    DirectUsageTruthSource,
    OllamaCloudUsageFetcher,
    OpencodeGoUsageFetcher,
    ZaiUsageFetcher,
    make_direct_fetcher,
    supported_provider_types,
)
from switchboard.limit import LimitState

# ── make_direct_fetcher ─────────────────────────────────────────────────────


def test_supported_provider_types_includes_known_vendors() -> None:
    types = supported_provider_types()
    assert "zai" in types
    assert "zai-coding-plan" in types
    assert "opencode-go" in types
    assert "ollama-cloud" in types


def test_make_direct_fetcher_returns_none_for_unsupported() -> None:
    assert make_direct_fetcher("generic") is None
    assert make_direct_fetcher("deepseek") is None


def test_make_direct_fetcher_zai_requires_key() -> None:
    assert make_direct_fetcher("zai") is None
    f = make_direct_fetcher("zai", api_key="test-key")
    assert f is not None
    assert isinstance(f, ZaiUsageFetcher)


def test_make_direct_fetcher_opencode_requires_workspace_and_cookie() -> None:
    assert make_direct_fetcher("opencode-go") is None
    assert make_direct_fetcher("opencode-go", cookie="ck") is None
    assert make_direct_fetcher("opencode-go", workspace_id="ws") is None
    f = make_direct_fetcher("opencode-go", workspace_id="ws", cookie="ck")
    assert f is not None
    assert isinstance(f, OpencodeGoUsageFetcher)


def test_make_direct_fetcher_ollama_requires_cookie() -> None:
    assert make_direct_fetcher("ollama-cloud") is None
    f = make_direct_fetcher("ollama-cloud", cookie="test-cookie")
    assert f is not None
    assert isinstance(f, OllamaCloudUsageFetcher)


def test_api_key_is_never_used_as_a_cookie() -> None:
    """Plan 022 WI-2. The first cut fell back to `api_key` when no cookie was
    configured, which would have sent a provider's upstream bearer token to
    ollama.com / opencode.ai as a raw Cookie header — the wrong credential, to
    the wrong place, and silently, because the vendor would simply answer with
    its logged-out page. No cookie means no fetcher."""
    assert make_direct_fetcher("ollama-cloud", api_key="sk-secret") is None
    assert (
        make_direct_fetcher("opencode-go", workspace_id="ws", api_key="sk-secret")
        is None
    )


def test_bare_opencode_token_gets_the_auth_prefix() -> None:
    """A bare token is what copying the value out of a browser gives you.
    Accepting only `auth=<token>` would fail as a sign-in redirect much later
    instead of here."""
    f = make_direct_fetcher("opencode-go", workspace_id="ws", cookie="rawtoken")
    assert isinstance(f, OpencodeGoUsageFetcher)
    assert f._cookie == "auth=rawtoken"


def test_full_opencode_cookie_string_is_left_alone() -> None:
    f = make_direct_fetcher(
        "opencode-go", workspace_id="ws", cookie="auth=t; other=1"
    )
    assert isinstance(f, OpencodeGoUsageFetcher)
    assert f._cookie == "auth=t; other=1"


# ── ZaiUsageFetcher ─────────────────────────────────────────────────────────


def _zai_response() -> bytes:
    return json.dumps({
        "code": 200,
        "msg": "ok",
        "data": {
            "limits": [
                {
                    "type": "TOKENS_LIMIT",
                    "unit": 3,
                    "percentage": 40.0,
                    "nextResetTime": str(int((time.time() + 18000) * 1000)),
                },
                {
                    "type": "TOKENS_LIMIT",
                    "unit": 6,
                    "percentage": 60.0,
                    "nextResetTime": str(int((time.time() + 432000) * 1000)),
                },
            ]
        }
    }).encode()


def _mock_transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=30.0,
    )


@pytest.mark.asyncio
async def test_zai_fetcher_parses_session_and_weekly() -> None:
    fetcher = ZaiUsageFetcher("test-key", "zai-test")
    client = _mock_transport(lambda req: httpx.Response(200, content=_zai_response()))
    try:
        reading = await fetcher.fetch_usage(client)
    finally:
        await client.aclose()
    assert isinstance(reading, LimitState)
    assert reading.provider == "zai-test"
    # session 40% used → 60 remaining
    assert reading.requests_remaining == 60
    assert reading.requests_limit == 100
    # weekly 60% used → 40% remaining
    assert reading.weekly_remaining_fraction is not None
    assert reading.weekly_remaining_fraction == pytest.approx(0.40)
    assert reading.weekly_reset_epoch is not None
    assert reading.weekly_reset_epoch > time.time()


@pytest.mark.asyncio
async def test_zai_fetcher_missing_weekly_entry() -> None:
    """When the weekly entry is absent, weekly fields stay None."""
    payload = json.dumps({
        "data": {
            "limits": [
                {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 10.0},
            ]
        }
    }).encode()
    fetcher = ZaiUsageFetcher("test-key")
    client = _mock_transport(lambda req: httpx.Response(200, content=payload))
    try:
        reading = await fetcher.fetch_usage(client)
    finally:
        await client.aclose()
    assert reading.weekly_remaining_fraction is None
    assert reading.weekly_reset_epoch is None


# ── OpencodeGoUsageFetcher ──────────────────────────────────────────────────

_OPENCODE_HTML = """
<script>
rollingUsage:$R[36]={status:"ok",resetInSec:17223,usagePercent:0}
weeklyUsage:$R[37]={status:"ok",resetInSec:256748,usagePercent:13}
monthlyUsage:$R[38]={status:"ok",resetInSec:2306645,usagePercent:7}
</script>
"""


@pytest.mark.asyncio
async def test_opencode_fetcher_parses_hydration_blob() -> None:
    fetcher = OpencodeGoUsageFetcher("wrk_test", "auth-cookie", "ocg-test")
    client = _mock_transport(
        lambda req: httpx.Response(200, content=_OPENCODE_HTML.encode())
    )
    try:
        reading = await fetcher.fetch_usage(client)
    finally:
        await client.aclose()
    assert reading.provider == "ocg-test"
    # rolling 0% → 100 remaining
    assert reading.requests_remaining == 100
    # weekly 13% used → 87% remaining
    assert reading.weekly_remaining_fraction == pytest.approx(0.87)
    assert reading.weekly_reset_epoch is not None


@pytest.mark.asyncio
async def test_opencode_fetcher_redirect_to_auth_raises() -> None:
    fetcher = OpencodeGoUsageFetcher("wrk_test", "expired")

    # The real site 302s to auth.opencode.ai and serves a 200 on the final hop.
    # Simulate: first request gets a 302 redirect, second gets the auth page.
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                302,
                headers={"location": "https://auth.opencode.ai/authorize"},
            )
        return httpx.Response(200, content=b"<html>sign in</html>")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True,
    )
    try:
        with pytest.raises((httpx.HTTPError, httpx.HTTPStatusError)):
            await fetcher.fetch_usage(client)
    finally:
        await client.aclose()


# ── OllamaCloudUsageFetcher ────────────────────────────────────────────────

_OLLAMA_HTML = """
<section>
<h3>Session usage</h3>
<div style="width: 30%">30% used</div>
<span>Resets in 4 hours 47 minutes</span>
</section>
<section>
<h3>Weekly usage</h3>
<div style="width: 65%">65% used</div>
<span>Resets in 2 days 3 hours</span>
</section>
"""


@pytest.mark.asyncio
async def test_ollama_fetcher_parses_session_and_weekly() -> None:
    fetcher = OllamaCloudUsageFetcher("test-cookie", "ollama-test")
    client = _mock_transport(
        lambda req: httpx.Response(200, content=_OLLAMA_HTML.encode())
    )
    try:
        reading = await fetcher.fetch_usage(client)
    finally:
        await client.aclose()
    assert reading.provider == "ollama-test"
    # session 30% used → 70 remaining
    assert reading.requests_remaining == 70
    # weekly 65% used → 35% remaining
    assert reading.weekly_remaining_fraction == pytest.approx(0.35)
    assert reading.weekly_reset_epoch is not None


@pytest.mark.asyncio
async def test_ollama_fetcher_unparseable_page_raises() -> None:
    """A 200 with no usage bars means the page was restyled, not that usage is
    zero. Returning an empty reading would be indistinguishable from a provider
    with no weekly quota; raising makes the rot countable (Plan 022 WI-3)."""
    fetcher = OllamaCloudUsageFetcher("session=test-cookie")
    client = _mock_transport(
        lambda req: httpx.Response(200, content=b"<html>no data</html>")
    )
    try:
        with pytest.raises(DirectUsageParseError) as excinfo:
            await fetcher.fetch_usage(client)
    finally:
        await client.aclose()
    # The message has to name the provider and the surface, because the person
    # reading it is deciding which parser to go and fix.
    assert "ollama-cloud" in str(excinfo.value)
    assert "settings page" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ollama_fetcher_session_only_does_not_raise() -> None:
    """A page with a session bar but no weekly one is a provider without a
    weekly quota, not a broken parser. It must not be counted as rot."""
    html = b"<html>Session usage 40% used, resets in 2 hours</html>"
    fetcher = OllamaCloudUsageFetcher("session=test-cookie")
    client = _mock_transport(lambda req: httpx.Response(200, content=html))
    try:
        reading = await fetcher.fetch_usage(client)
    finally:
        await client.aclose()
    assert reading.weekly_remaining_fraction is None
    assert reading.requests_remaining == 60


# ── DirectUsageTruthSource ──────────────────────────────────────────────────


class _StubFetcher:
    """A fetcher that returns a fixed reading or raises."""

    def __init__(self, reading: LimitState | None = None, error: Exception | None = None) -> None:
        self._reading = reading or LimitState(provider="stub")
        self._error = error
        self.fetch_count = 0

    @property
    def provider_name(self) -> str:
        return "stub"

    async def fetch_usage(self, client: httpx.AsyncClient) -> LimitState:
        self.fetch_count += 1
        if self._error is not None:
            raise self._error
        return self._reading


@pytest.mark.asyncio
async def test_direct_usage_truth_source_fetch_success() -> None:
    reading = LimitState(
        provider="zai-test",
        requests_remaining=50,
        requests_limit=100,
        weekly_remaining_fraction=0.55,
        weekly_reset_epoch=time.time() + 86400,
    )
    fetcher = _StubFetcher(reading=reading)
    source = DirectUsageTruthSource(fetcher)
    try:
        result = await source.fetch(now_monotonic=100.0)
    finally:
        await source.close()
    assert result.ok is True
    assert result.reading.requests_remaining == 50
    assert result.reading.weekly_remaining_fraction == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_direct_usage_truth_source_fetch_failure_serves_lkg() -> None:
    reading = LimitState(provider="zai-test", requests_remaining=50)
    fetcher = _StubFetcher(
        reading=reading,
        error=httpx.ConnectError("connection refused"),
    )
    source = DirectUsageTruthSource(fetcher)
    try:
        # First fetch fails — serves fail-safe (no LKG yet)
        result = await source.fetch(now_monotonic=100.0)
        assert result.ok is False
        # Second fetch also fails — serves the fail-safe LKG from first
        result2 = await source.fetch(now_monotonic=110.0)
        assert result2.ok is False
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_direct_usage_truth_source_lkg_after_success() -> None:
    """A successful fetch sets the LKG; a subsequent failure serves it stale."""
    reading = LimitState(
        provider="zai-test",
        weekly_remaining_fraction=0.80,
        weekly_reset_epoch=time.time() + 432000,
    )
    good_fetcher = _StubFetcher(reading=reading)
    source = DirectUsageTruthSource(good_fetcher)
    try:
        # First fetch succeeds
        result = await source.fetch(now_monotonic=100.0)
        assert result.ok is True
        assert result.reading.weekly_remaining_fraction == pytest.approx(0.80)

        # Swap to a failing fetcher — serves LKG
        good_fetcher._error = httpx.ConnectError("down")
        result2 = await source.fetch(now_monotonic=110.0)
        assert result2.ok is False
        assert result2.reading.weekly_remaining_fraction == pytest.approx(0.80)
    finally:
        await source.close()


def test_direct_usage_truth_source_implements_protocol() -> None:
    """DirectUsageTruthSource has the TruthSource protocol methods."""
    fetcher = _StubFetcher()
    source = DirectUsageTruthSource(fetcher)
    assert hasattr(source, "fetch")
    assert hasattr(source, "last_cached")
    assert hasattr(source, "close")
    assert hasattr(source, "record_response_headers")


# ── parse failures are distinct from transport failures (Plan 022 WI-3) ─────


class _ParseFailingFetcher:
    """A fetcher whose vendor surface answered with something unreadable."""

    provider_name = "rotted"

    async def fetch_usage(self, client: httpx.AsyncClient) -> LimitState:
        raise DirectUsageParseError("rotted", "the settings page", "no bars")


class _TransportFailingFetcher:
    provider_name = "unreachable"

    async def fetch_usage(self, client: httpx.AsyncClient) -> LimitState:
        raise httpx.ConnectError("network down")


@pytest.mark.asyncio
async def test_parse_failures_are_counted_separately() -> None:
    """Routing degrades identically for both failures — the provider goes
    unscored — so the counter is the only thing that distinguishes "the page
    changed" from "the network blipped"."""
    source = DirectUsageTruthSource(_ParseFailingFetcher())
    for _ in range(3):
        await source.fetch(now_monotonic=100.0)
    assert source.parse_failures == 3
    assert source.transport_failures == 0


@pytest.mark.asyncio
async def test_transport_failures_are_not_counted_as_parse_failures() -> None:
    source = DirectUsageTruthSource(_TransportFailingFetcher())
    await source.fetch(now_monotonic=100.0)
    assert source.transport_failures == 1
    assert source.parse_failures == 0


@pytest.mark.asyncio
async def test_a_parse_failure_still_fails_safe() -> None:
    """The counter is an observability affordance, not a behaviour change: an
    unreadable response must still serve last-known-good with ok=False, so the
    pace strategy sees a stale provider and ranks it in table order."""
    source = DirectUsageTruthSource(_ParseFailingFetcher())
    cached = await source.fetch(now_monotonic=100.0)
    assert cached.ok is False


@pytest.mark.asyncio
async def test_the_rot_warning_is_logged_once_then_rearmed(caplog) -> None:
    """A poll loop firing every 30 s would turn a permanent condition into an
    unreadable log. Warn once, then re-arm on recovery so the next break is
    loud too."""
    fetcher = _ParseFailingFetcher()
    source = DirectUsageTruthSource(fetcher)
    with caplog.at_level(logging.WARNING, logger="switchboard.direct_usage"):
        for _ in range(5):
            await source.fetch(now_monotonic=100.0)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "rotted" in warnings[0].getMessage()

    # Recovery re-arms the warning.
    source._fetcher = _StubFetcher(LimitState(provider="rotted"))  # type: ignore[assignment]
    await source.fetch(now_monotonic=200.0)
    source._fetcher = fetcher  # type: ignore[assignment]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="switchboard.direct_usage"):
        await source.fetch(now_monotonic=300.0)
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1
