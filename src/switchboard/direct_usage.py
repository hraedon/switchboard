"""Direct usage solicitation — standalone truth source without usage-dashboard.

An optional truth source that fetches usage data directly from each provider's
own usage endpoint or settings page, eliminating the need for a separate
usage-dashboard deployment.  Each provider-type fetcher knows its vendor's
specific API or page layout and normalizes into a :class:`~switchboard.limit.LimitState`
with the weekly-window fields (``weekly_remaining_fraction``,
``weekly_reset_epoch``) populated for pace routing.

This module is a SHELL module (not pure-core): it does I/O via httpx.  It
implements the :class:`~switchboard.truth.TruthSource` protocol so it slots
into the existing reconcile loop alongside :class:`~switchboard.dashboard.DashboardTruthSource`
and :class:`~switchboard.truth.PolledTruthSource`.

Supported providers:

* **zai-coding-plan / zai** — JSON API at ``/api/monitor/usage/quota/limit``.
  Returns session + weekly ``TOKENS_LIMIT`` entries with percentage and
  ``nextResetTime`` (epoch ms).
* **opencode-go** — SolidJS hydration blob scrape of the workspace dashboard
  page.  Parses ``rollingUsage``/``weeklyUsage``/``monthlyUsage`` from the SSR
  hydration data.
* **ollama-cloud** — HTML settings page scrape.  Parses session + weekly
  usage bars and reset countdowns.

Unsupported providers fall back to the existing truth source (NullTruthSource,
HeaderTruthSource, PolledTruthSource) — direct usage is opt-in per provider.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from typing import Any, Protocol

import httpx

from switchboard.limit import CachedReading, LimitState

log = logging.getLogger("switchboard.direct_usage")

_HTTP_TIMEOUT = 30.0
_FAIL_SAFE_AGE = 99999.0


class DirectUsageParseError(Exception):
    """A vendor surface answered, but not with anything we could read.

    Distinct from a transport failure on purpose (Plan 022 WI-3). Two of the
    three fetchers parse markup no vendor has promised to keep stable, so
    "HTTP 200 and nothing extractable" is the expected way this feature dies:
    quietly, on someone else's release schedule. Routing degrades safely — an
    unscored provider ranks in table order — which is exactly why the failure
    has to be counted and logged rather than left to be inferred from the
    absence of a signal.
    """

    def __init__(self, provider: str, surface: str, detail: str = "") -> None:
        self.provider = provider
        self.surface = surface
        message = f"could not parse {surface} for '{provider}'"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _epoch_ms_to_epoch(value: Any) -> float | None:
    """Convert epoch-milliseconds (z.ai's format) to epoch-seconds."""
    if value is None:
        return None
    try:
        return int(str(value)) / 1000.0
    except (ValueError, TypeError):
        return None


def _pct_to_remaining(pct: float | None) -> float | None:
    """Convert a used-percentage to a remaining fraction (0-1, clamped)."""
    if pct is None:
        return None
    return max(0.0, min(1.0, 1.0 - pct / 100.0))


# ── Per-provider fetcher protocol ───────────────────────────────────────────


class UsageFetcher(Protocol):
    """Fetches usage data directly from a provider's own endpoint."""

    async def fetch_usage(self, client: httpx.AsyncClient) -> LimitState: ...

    @property
    def provider_name(self) -> str: ...


# ── z.ai fetcher ────────────────────────────────────────────────────────────

_ZAI_USAGE_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
_ZAI_SESSION_UNIT = 3
_ZAI_WEEKLY_UNIT = 6


class ZaiUsageFetcher:
    """Fetches z.ai usage from the quota/limit JSON API.

    The API returns a ``{"code","msg","data": {"limits": [...]}}`` envelope.
    Session = ``TOKENS_LIMIT`` unit 3 (resets every ~5h), weekly = unit 6
    (resets weekly).  Each entry carries ``percentage`` (used) and
    ``nextResetTime`` (epoch ms).
    """

    def __init__(self, api_key: str, name: str = "zai") -> None:
        self._api_key = api_key
        self._name = name

    @property
    def provider_name(self) -> str:
        return self._name

    async def fetch_usage(self, client: httpx.AsyncClient) -> LimitState:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        response = await client.get(_ZAI_USAGE_URL, headers=headers)
        response.raise_for_status()
        data = response.json()

        payload = data["data"] if isinstance(data.get("data"), dict) else data
        try:
            limits: list[dict[str, object]] = payload["limits"]
        except (KeyError, TypeError) as exc:
            raise DirectUsageParseError(
                self._name, "the quota/limit API", "no 'limits' in the response",
            ) from exc
        session_entry: dict[str, object] | None = None
        weekly_entry: dict[str, object] | None = None
        for entry in limits:
            if entry.get("type") == "TOKENS_LIMIT":
                if entry.get("unit") == _ZAI_SESSION_UNIT:
                    session_entry = entry
                elif entry.get("unit") == _ZAI_WEEKLY_UNIT:
                    weekly_entry = entry

        if session_entry is None and weekly_entry is None:
            raise DirectUsageParseError(
                self._name,
                "the quota/limit API",
                "no TOKENS_LIMIT entry for the session or weekly unit",
            )

        session_pct: float | None = None
        session_reset: float | None = None
        if session_entry is not None:
            session_pct = float(str(session_entry["percentage"]))
            session_reset = _epoch_ms_to_epoch(
                session_entry.get("nextResetTime")
            )

        weekly_pct: float | None = None
        weekly_reset: float | None = None
        if weekly_entry is not None:
            weekly_pct = float(str(weekly_entry["percentage"]))
            weekly_reset = _epoch_ms_to_epoch(
                weekly_entry.get("nextResetTime")
            )

        return LimitState(
            concurrent_sessions=0,
            limit=1,
            hard_cap=2,
            requests_remaining=(
                round(100 - session_pct) if session_pct is not None else None
            ),
            requests_limit=100 if session_pct is not None else None,
            bucket_reset_epoch=session_reset,
            provider=self._name,
            age_seconds=0.0,
            weekly_remaining_fraction=_pct_to_remaining(weekly_pct),
            weekly_reset_epoch=weekly_reset,
        )


# ── opencode-go fetcher ─────────────────────────────────────────────────────

_OPENCODE_DASHBOARD_URL = "https://opencode.ai/workspace/{workspace_id}/go"
_OPENCODE_AUTH_HOST_SUFFIX = "auth.opencode.ai"
_OPENCODE_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_OPENCODE_WINDOW_KEYS = ("rolling", "weekly", "monthly")
_OPENCODE_SSR_WINDOW_RES = {
    key: re.compile(rf"{key}Usage:\$R\[\d+\]=\{{([^{{}}]*)\}}")
    for key in _OPENCODE_WINDOW_KEYS
}
_OPENCODE_PERCENT_RE = re.compile(r"usagePercent:(-?\d+(?:\.\d+)?)")
_OPENCODE_RESET_RE = re.compile(r"resetInSec:(-?\d+(?:\.\d+)?)")


class OpencodeGoUsageFetcher:
    """Fetches opencode-go usage from the workspace dashboard page.

    Parses the SolidJS hydration blob (exact values).  Rolling → session,
    weekly → weekly, monthly → ignored (not a LimitState field).
    """

    def __init__(self, workspace_id: str, cookie: str, name: str = "opencode-go") -> None:
        self._workspace_id = workspace_id
        # Accept either a full cookie string ("auth=xyz; other=1") or the bare
        # token, because the bare token is what copying the value out of a
        # browser's storage inspector actually gives you. Guessing wrong here
        # produces a sign-in redirect forty minutes later rather than an
        # error, so normalise at construction where it is visible.
        self._cookie = cookie if "=" in cookie else f"auth={cookie}"
        self._name = name

    @property
    def provider_name(self) -> str:
        return self._name

    async def fetch_usage(self, client: httpx.AsyncClient) -> LimitState:
        from urllib.parse import quote

        url = _OPENCODE_DASHBOARD_URL.format(
            workspace_id=quote(self._workspace_id, safe="")
        )
        headers = {
            "Cookie": self._cookie,
            "User-Agent": _OPENCODE_USER_AGENT,
            "Accept": "text/html",
        }
        response = await client.get(
            url, headers=headers, follow_redirects=True
        )
        if response.status_code in (401, 403):
            raise httpx.HTTPStatusError(
                f"OpenCode Go session rejected: HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
        if response.url.host.endswith(_OPENCODE_AUTH_HOST_SUFFIX):
            raise httpx.HTTPError(
                "OpenCode Go redirected to sign-in — the auth cookie has "
                "expired or the workspace id is wrong"
            )
        response.raise_for_status()
        html = response.text

        windows: dict[str, dict[str, float]] = {}
        for key, pattern in _OPENCODE_SSR_WINDOW_RES.items():
            match = pattern.search(html)
            if match is None:
                continue
            body = match.group(1)
            pct_m = _OPENCODE_PERCENT_RE.search(body)
            reset_m = _OPENCODE_RESET_RE.search(body)
            if pct_m is None:
                continue
            windows[key] = {
                "percent": max(0.0, float(pct_m.group(1))),
                "reset_in_seconds": (
                    max(0.0, float(reset_m.group(1)))
                    if reset_m else 0.0
                ),
            }

        if not windows:
            raise DirectUsageParseError(
                self._name,
                "the workspace page's hydration blob",
                "no usage window matched; the page layout has probably changed",
            )

        rolling = windows.get("rolling")
        weekly = windows.get("weekly")
        now_epoch = time.time()

        session_pct = rolling["percent"] if rolling else None
        session_reset = (
            now_epoch + rolling["reset_in_seconds"] if rolling else None
        )
        weekly_pct = weekly["percent"] if weekly else None
        weekly_reset = (
            now_epoch + weekly["reset_in_seconds"] if weekly else None
        )

        return LimitState(
            concurrent_sessions=0,
            limit=1,
            hard_cap=2,
            requests_remaining=(
                round(100 - session_pct) if session_pct is not None else None
            ),
            requests_limit=100 if session_pct is not None else None,
            bucket_reset_epoch=session_reset,
            provider=self._name,
            age_seconds=0.0,
            weekly_remaining_fraction=_pct_to_remaining(weekly_pct),
            weekly_reset_epoch=weekly_reset,
        )


# ── ollama-cloud fetcher ────────────────────────────────────────────────────

_OLLAMA_SETTINGS_URL = "https://ollama.com/settings"
_OLLAMA_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_OLLAMA_SESSION_LABELS = ("Session usage", "Hourly usage")
_OLLAMA_WEEKLY_LABEL = "Weekly usage"
_OLLAMA_BLOCK_END_TAG = "</section>"
_OLLAMA_PERCENT_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*%\s*used", re.IGNORECASE
)
_OLLAMA_BAR_WIDTH_RE = re.compile(
    r"width:\s*([0-9]+(?:\.[0-9]+)?)%", re.IGNORECASE
)
_OLLAMA_RESET_IN_RE = re.compile(
    r"resets?\s+in\s+(.{0,40}?)(?:<|\bago\b|$)", re.IGNORECASE | re.DOTALL
)
_OLLAMA_DURATION_TOKEN_RE = re.compile(
    r"(\d+)\s*(week|day|hour|hr|minute|min)s?\b", re.IGNORECASE
)
_OLLAMA_DURATION_UNITS = {
    "week": "weeks",
    "day": "days",
    "hour": "hours",
    "hr": "hours",
    "minute": "minutes",
    "min": "minutes",
}


def _ollama_block_after(label: str, html: str) -> str | None:
    start = html.find(label)
    if start == -1:
        return None
    tail = html[start + len(label):]
    boundaries: list[int] = []
    for other in (*_OLLAMA_SESSION_LABELS, _OLLAMA_WEEKLY_LABEL):
        if other != label:
            pos = tail.find(other)
            if pos != -1:
                boundaries.append(pos)
    if boundaries:
        end = min(boundaries)
    else:
        section_end = tail.find(_OLLAMA_BLOCK_END_TAG)
        end = section_end if section_end != -1 else len(tail)
    return tail[:end]


def _ollama_parse_percent(block: str) -> float | None:
    match = _OLLAMA_PERCENT_RE.search(block) or _OLLAMA_BAR_WIDTH_RE.search(block)
    if match:
        return float(match.group(1))
    return None


def _ollama_parse_reset(block: str, now_epoch: float) -> float | None:
    phrase = _OLLAMA_RESET_IN_RE.search(block)
    if phrase is None:
        return None
    tokens = _OLLAMA_DURATION_TOKEN_RE.findall(phrase.group(1))
    if not tokens:
        return None
    delta: dict[str, int] = {}
    for amount, unit in tokens:
        key = _OLLAMA_DURATION_UNITS[unit.lower()]
        delta[key] = delta.get(key, 0) + int(amount)
    return now_epoch + timedelta(**delta).total_seconds()


def _ollama_parse_usage_block(
    labels: tuple[str, ...], html: str, now_epoch: float,
) -> tuple[float, float | None] | None:
    for label in labels:
        block = _ollama_block_after(label, html)
        if block is None:
            continue
        pct = _ollama_parse_percent(block)
        if pct is not None:
            return pct, _ollama_parse_reset(block, now_epoch)
    return None


class OllamaCloudUsageFetcher:
    """Fetches ollama-cloud usage from the settings page (HTML scrape).

    No usage API exists; the settings page is scraped with a browser session
    cookie.  Parses session + weekly usage bars and reset countdowns.
    """

    def __init__(self, cookie: str, name: str = "ollama-cloud") -> None:
        self._cookie = cookie
        self._name = name

    @property
    def provider_name(self) -> str:
        return self._name

    async def fetch_usage(self, client: httpx.AsyncClient) -> LimitState:
        headers = {
            "Cookie": self._cookie,
            "User-Agent": _OLLAMA_USER_AGENT,
            "Accept": "text/html",
        }
        response = await client.get(
            _OLLAMA_SETTINGS_URL, headers=headers, follow_redirects=True
        )
        response.raise_for_status()
        html = response.text
        now_epoch = time.time()

        session = _ollama_parse_usage_block(
            _OLLAMA_SESSION_LABELS, html, now_epoch,
        )
        weekly = _ollama_parse_usage_block(
            (_OLLAMA_WEEKLY_LABEL,), html, now_epoch,
        )

        if session is None and weekly is None:
            raise DirectUsageParseError(
                self._name,
                "the settings page",
                "no usage bar matched; the page has probably been restyled",
            )

        session_pct = session[0] if session else None
        session_reset = session[1] if session else None
        weekly_pct = weekly[0] if weekly else None
        weekly_reset = weekly[1] if weekly else None

        return LimitState(
            concurrent_sessions=0,
            limit=1,
            hard_cap=2,
            requests_remaining=(
                round(100 - session_pct) if session_pct is not None else None
            ),
            requests_limit=100 if session_pct is not None else None,
            bucket_reset_epoch=session_reset,
            provider=self._name,
            age_seconds=0.0,
            weekly_remaining_fraction=_pct_to_remaining(weekly_pct),
            weekly_reset_epoch=weekly_reset,
        )


# ── DirectUsageTruthSource ─────────────────────────────────────────────────


_FETCHERS: dict[str, type[UsageFetcher]] = {
    "zai": ZaiUsageFetcher,
    "zai-coding-plan": ZaiUsageFetcher,
    "opencode-go": OpencodeGoUsageFetcher,
    "ollama-cloud": OllamaCloudUsageFetcher,
}


def supported_provider_types() -> list[str]:
    """Provider types that have a direct usage fetcher."""
    return sorted(_FETCHERS.keys())


def make_direct_fetcher(
    provider_type: str,
    *,
    api_key: str = "",
    usage_key: str = "",
    workspace_id: str = "",
    cookie: str = "",
    name: str = "",
) -> UsageFetcher | None:
    """Construct a direct usage fetcher for a provider type, or None.

    Returns None when the provider type has no direct fetcher, or when its
    credentials are not configured — in both cases the caller falls back to
    the existing truth source, so a half-configured provider loses its weekly
    signal rather than failing the boot.

    Credentials are sourced per-provider-type:

    * zai/zai-coding-plan: ``api_key`` or ``usage_key`` (a real bearer token)
    * opencode-go: ``workspace_id`` + ``cookie``
    * ollama-cloud: ``cookie``

    A cookie is never substituted from ``api_key`` (Plan 022 WI-2). They are
    different credentials for different surfaces: an API key sent as a raw
    ``Cookie:`` header would not authenticate anything, and it would put a
    bearer token somewhere it was never meant to go — silently, since the
    vendor would simply answer with its logged-out page. If the cookie is not
    configured, there is no fetcher.
    """
    cls = _FETCHERS.get(provider_type)
    if cls is None:
        return None
    effective_name = name or provider_type
    if cls is ZaiUsageFetcher:
        key = api_key or usage_key
        if not key:
            log.warning(
                "direct usage is enabled for '%s' but no api_key/usage_key is "
                "configured; falling back to its default truth source",
                effective_name,
            )
            return None
        return ZaiUsageFetcher(key, effective_name)
    if cls is OpencodeGoUsageFetcher:
        if not workspace_id or not cookie:
            log.warning(
                "direct usage is enabled for '%s' but %s not configured "
                "(direct_usage_workspace_id / direct_usage_cookie_env); "
                "falling back to its default truth source",
                effective_name,
                "workspace id and cookie are"
                if not workspace_id and not cookie
                else ("the workspace id is" if not workspace_id else "the cookie is"),
            )
            return None
        return OpencodeGoUsageFetcher(workspace_id, cookie, effective_name)
    if cls is OllamaCloudUsageFetcher:
        if not cookie:
            log.warning(
                "direct usage is enabled for '%s' but no cookie is configured "
                "(direct_usage_cookie_env); falling back to its default "
                "truth source",
                effective_name,
            )
            return None
        return OllamaCloudUsageFetcher(cookie, effective_name)
    return None


class DirectUsageTruthSource:
    """TruthSource that fetches usage directly from the provider.

    Implements the same :class:`~switchboard.truth.TruthSource` protocol as
    :class:`~switchboard.dashboard.DashboardTruthSource`, but eliminates the
    middleman: switchboard polls the provider's own usage endpoint/page
    directly and normalizes into a :class:`~switchboard.limit.LimitState`.

    Staleness is determined by the fetch result — a failed fetch serves the
    last-known-good with ``ok=False`` (same fail-safe as the dashboard source).
    """

    # Advisory: a usage scrape optimises routing; a broken scrape must not
    # close the gate (Plan 022 containment — see ReconciliationLoop).
    advisory = True

    def __init__(
        self,
        fetcher: UsageFetcher,
        *,
        stale_ttl: float = 900.0,
        poll_interval: float = 30.0,
    ) -> None:
        self._fetcher = fetcher
        self._stale_ttl = stale_ttl
        self._poll_interval = poll_interval
        self._lkg: CachedReading | None = None
        self._client: httpx.AsyncClient | None = None
        self._parse_failures = 0
        self._transport_failures = 0
        self._warned_about_parsing = False

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        return self._client

    def _serve_lkg_or_failsafe(self, now_monotonic: float) -> CachedReading:
        if self._lkg is not None:
            return CachedReading(
                reading=self._lkg.reading,
                fetched_at_monotonic=self._lkg.fetched_at_monotonic,
                ok=False,
            )
        reading = LimitState(
            concurrent_sessions=2,
            limit=1,
            hard_cap=2,
            provider=self._fetcher.provider_name,
            age_seconds=0.0,
        )
        cached = CachedReading(
            reading=reading,
            fetched_at_monotonic=now_monotonic - _FAIL_SAFE_AGE,
            ok=False,
        )
        self._lkg = cached
        return cached

    async def fetch(self, *, now_monotonic: float) -> CachedReading:
        """Fetch usage directly from the provider's own endpoint."""
        try:
            client = await self._ensure_client()
            reading = await self._fetcher.fetch_usage(client)
        except DirectUsageParseError as exc:
            # The vendor answered; we could not read it. Almost always means
            # the page changed under us (Plan 022 WI-3). Counted and reported
            # separately from transport failures because the remedy is
            # different — nobody is going to fix this by retrying.
            self._parse_failures += 1
            if not self._warned_about_parsing:
                self._warned_about_parsing = True
                log.warning(
                    "direct usage: %s — the vendor surface responded but its "
                    "shape has changed, so this provider now has no weekly "
                    "signal and will rank in table order under the pace "
                    "strategy. This message is logged once; see the "
                    "parse_failures counter in /status.json.",
                    exc,
                )
            else:
                log.debug("direct usage parse failure: %s", exc)
            return self._serve_lkg_or_failsafe(now_monotonic)
        except Exception as exc:
            self._transport_failures += 1
            log.warning(
                "direct usage fetch failed for '%s': %s: %s",
                self._fetcher.provider_name,
                type(exc).__name__,
                exc,
            )
            return self._serve_lkg_or_failsafe(now_monotonic)

        cached = CachedReading(
            reading=reading,
            fetched_at_monotonic=now_monotonic,
            ok=True,
        )
        self._lkg = cached
        # A parser that starts working again has been fixed (or the vendor
        # reverted); re-arm the one-shot warning so the next break is loud too.
        self._warned_about_parsing = False
        return cached

    @property
    def parse_failures(self) -> int:
        """Responses received but not understood (Plan 022 WI-3).

        Rising = the vendor surface changed and this provider's weekly signal
        is gone. Distinct from ``transport_failures``: routing degrades
        identically, but only one of them is fixed by waiting.
        """
        return self._parse_failures

    @property
    def transport_failures(self) -> int:
        """Fetches that never got a usable response (network, auth, timeout)."""
        return self._transport_failures

    @property
    def last_cached(self) -> CachedReading | None:
        return self._lkg

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def record_response_headers(
        self, headers: dict[str, str], status: int, *, now_monotonic: float
    ) -> None:
        pass
