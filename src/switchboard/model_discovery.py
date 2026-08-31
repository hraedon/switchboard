"""Upstream model-listing discovery — the shared probe (Plan 027 W1).

Every surface that asks an upstream "what models do you serve" (the GUI's
"show models" / auto-match handlers, the reachability test, the periodic
discovery loop, the manual refresh endpoint) goes through
:func:`fetch_model_listing`. The probe must hit the byte-identical URL and
present the byte-identical credential the live forwarding path would use for
a client ``GET /v1/models``:

* the URL is composed with :func:`switchboard.control.compose_upstream_path`
  (the version-aware composition Plan 021 established), not concatenated;
* the credential is built with :func:`switchboard.control.credential_value`
  (the same prefix normalization the egress choke point applies).

The body is read under a hard byte cap — an unbounded ``/models`` response
(or a hostile one) must not be buffered whole before a parse can reject it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from switchboard.control import compose_upstream_path, credential_value
from switchboard.model_capabilities import ModelObservation, parse_model_listing

if TYPE_CHECKING:
    from switchboard.capability_store import CapabilityStore
    from switchboard.providers import ProviderContext

log = logging.getLogger("switchboard.model_discovery")


#: Hard cap on a ``/models`` response body the probe will buffer. 4 MiB
#: clears the largest live OpenAI-compatible listings by an order of
#: magnitude; beyond that the probe reports "response too large" instead.
MODELS_RESPONSE_MAX_BYTES = 4 * 1024 * 1024

#: Cap on a non-2xx error body retained for the operator's ``detail``.
ERROR_DETAIL_MAX_BYTES = 16 * 1024

#: Probe timeout, per request.
PROBE_TIMEOUT = 5.0


@dataclass(frozen=True)
class ListingResult:
    """The outcome of one provider's model-listing probe."""

    provider: str
    ok: bool
    status: int | None = None
    latency_ms: float = 0.0
    detail: str = ""
    models: tuple[ModelObservation, ...] = ()
    #: The fingerprint recorded under, "" on failure.
    fingerprint: str = ""


def models_probe_url(ctx: ProviderContext) -> str:
    """The upstream URL a discovery probe must GET.

    The canonical client shape is ``/v1/models`` (every OpenAI-compatible
    ``baseURL`` convention is built for it), and composing it against the
    provider's base gives the exact URL the live proxy would egress a client
    ``GET /v1/models`` to — so "the provider proxies fine" and "the probe
    answers 200" stop being different statements.
    """
    return compose_upstream_path(ctx.upstream_url, "/v1/models")


def models_probe_headers(ctx: ProviderContext) -> dict[str, str]:
    """The credential headers a probe presents — the egress value, verbatim.

    Empty when the provider configures no key (then the live path sends the
    client's headers untouched, and the probe — which has no client — sends
    none).
    """
    if not ctx.api_key:
        return {}
    return {ctx.auth_header: credential_value(ctx.auth_prefix, ctx.api_key)}


def config_fingerprint(ctx: ProviderContext) -> str:
    """A digest of the provider's effective egress-relevant config.

    Recorded with every observation so that a later config change (base URL,
    credential header, prefix, key) makes the stored evidence visibly stale.
    The key itself is digested (not stored) — the same SQLite file already
    holds the plaintext key in stored mode, so this adds no exposure, but it
    means the fingerprint can round-trip through a log line safely.
    """
    import hashlib

    parts = (
        ctx.name,
        ctx.upstream_url,
        ctx.auth_header,
        ctx.auth_prefix,
        "" if not ctx.api_key else hashlib.sha256(ctx.api_key.encode()).hexdigest()[:16],
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


async def _bounded_detail(
    response: httpx.Response, status_code: int
) -> str:
    """A short failure reason from a non-2xx error body, under the cap."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        if total > ERROR_DETAIL_MAX_BYTES:
            break
        chunks.append(chunk)
        total += len(chunk)
    # The first chunk is always kept (a body that arrives as one chunk
    # larger than the cap would otherwise yield an empty detail); the join
    # is hard-truncated so retention stays bounded.
    raw = b"".join(chunks)[:ERROR_DETAIL_MAX_BYTES]
    text = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(text) if text.strip() else None
    except (ValueError, RecursionError):
        # RecursionError: a crafted deeply-nested error body (valid syntax)
        # overflows the parser; treat it as unparseable like any junk body.
        payload = None
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val[:200]
            if isinstance(val, dict):
                msg = val.get("message") or val.get("reason")
                if isinstance(msg, str) and msg:
                    return msg[:200]
    if text.strip():
        first = text.strip().splitlines()[0][:200]
        return first
    return f"HTTP {status_code}"


async def _probe(
    url: str,
    headers: dict[str, str],
    *,
    timeout: float,
    client_factory: Callable[[], httpx.AsyncClient],
) -> tuple[int, bytes, str]:
    """GET ``url`` and return ``(status, body, error_detail)``.

    The body is streamed under :data:`MODELS_RESPONSE_MAX_BYTES`; a body that
    would exceed it is abandoned, not buffered. On a non-2xx, at most
    :data:`ERROR_DETAIL_MAX_BYTES` of the error body is retained and a short
    reason is extracted; on a 2xx, ``error_detail`` is "".
    """
    try:
        async with client_factory() as client, client.stream(
            "GET", url, headers=headers
        ) as response:
            status = response.status_code
            if not (200 <= status < 300):
                detail = await _bounded_detail(response, status)
                with contextlib.suppress(Exception):
                    await response.aclose()
                return status, b"", detail
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MODELS_RESPONSE_MAX_BYTES:
                    with contextlib.suppress(Exception):
                        await response.aclose()
                    return status, b"", "response too large"
                chunks.append(chunk)
            return status, b"".join(chunks), ""
    except httpx.TimeoutException:
        return -1, b"", "timeout"
    except httpx.HTTPError as exc:
        return -1, b"", type(exc).__name__


async def probe_model_listing(
    ctx: ProviderContext,
    *,
    observed_at: float | None = None,
    timeout: float = PROBE_TIMEOUT,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> ListingResult:
    """Probe one provider's live model listing (parity URL + credential).

    ``observed_at`` is the wall-clock stamp stored with the observations
    (defaults to ``time.time()``); the pure core never reads a clock, so
    callers may pass a fixed instant in tests.
    """
    url = models_probe_url(ctx)
    headers = models_probe_headers(ctx)
    factory = client_factory or (
        lambda: httpx.AsyncClient(timeout=httpx.Timeout(timeout))
    )
    start = time.monotonic()
    status, body, error_detail = await _probe(
        url, headers, timeout=timeout, client_factory=factory
    )
    latency_ms = round((time.monotonic() - start) * 1000.0, 1)
    at = observed_at if observed_at is not None else time.time()

    models: tuple[ModelObservation, ...] = ()
    detail = error_detail
    # An empty body with a 2xx means the probe abandoned an over-size
    # listing; keep its detail (the parse below would clobber it with
    # "non-JSON response" for the empty buffer).
    payload: Any = None
    if 200 <= (status or 0) < 300 and body:
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            # RecursionError: a deeply-nested listing (valid syntax, up to the
            # byte cap) overflows the parser; report it like a non-JSON body
            # instead of letting it abort the probe (and the gather wave).
            payload = None
            detail = "non-JSON response"
        if detail == "" and isinstance(payload, (dict, list)):
            parsed = parse_model_listing(
                payload,
                provider=ctx.name,
                observed_at=at,
                config_fingerprint=config_fingerprint(ctx),
            )
            models = tuple(parsed)
            if not models:
                items = (
                    payload.get("data")
                    if isinstance(payload, dict)
                    and isinstance(payload.get("data"), list)
                    else payload if isinstance(payload, list) else []
                )
                # Items present but none parsed as a model still needs a
                # reason: without one the probe returns ok=False with an empty
                # detail, and the admin matrix shows a failed provider with a
                # blank explanation. Reached by a listing whose entries are the
                # wrong shape, and by deeply-nested hostile JSON on an
                # interpreter with stack enough to parse it (3.14 raises
                # RecursionError or not depending on stack available at
                # runtime, not on the input).
                detail = (
                    "no models in response"
                    if not items
                    else "no parsable models in response"
                )
        elif detail == "" and payload is None:
            detail = "non-JSON response"

    ok = 200 <= (status or 0) < 300 and bool(models)
    return ListingResult(
        provider=ctx.name,
        ok=ok,
        status=status if status >= 0 else None,
        latency_ms=latency_ms,
        detail=detail,
        models=models,
        fingerprint=config_fingerprint(ctx) if ok else "",
    )


class ModelDiscovery:
    """The periodic discovery loop's runner (Plan 027 W3).

    Advisory by design: discovery feeds the client catalog and the admin
    matrix — it never influences a routing decision, and a failed probe
    degrades the *guarantee level* of the affected contracts (fields fall
    back to observed/unknown), it never 503s traffic.

    ``interval_s`` is the minimum spacing between runs; ``0`` disables the
    periodic run (manual refresh still works). The runner is
    re-entrancy-guarded: a slow estate probe must not stack a second wave
    of upstream requests on top of the first.
    """

    def __init__(
        self,
        store: CapabilityStore,
        interval_s: float = 21600.0,
    ) -> None:
        self._store = store
        self._interval = max(0.0, float(interval_s))
        self._last_run: float | None = None
        self._in_flight = False

    @property
    def interval(self) -> float:
        return self._interval

    def is_due(self, now: float) -> bool:
        if self._interval <= 0.0:
            return False
        if self._last_run is None:
            return True
        return (now - self._last_run) >= self._interval

    @property
    def last_run(self) -> float | None:
        return self._last_run

    async def refresh(
        self,
        providers: Mapping[str, ProviderContext],
        *,
        force: bool = False,
    ) -> tuple[ListingResult, ...]:
        """Probe every live provider (bounded, in parallel) and persist the
        successful listings. Returns the per-provider results, in provider
        order. A provider whose probe fails keeps its previous observations
        (they will age out of ``max_age`` on their own).

        ``force`` bypasses the interval (the manual refresh endpoint); the
        in-flight guard still applies.
        """
        now = time.time()
        if not force and not self.is_due(now):
            return ()
        if self._in_flight:
            return ()
        self._in_flight = True
        try:
            results: list[ListingResult] = list(
                await asyncio.gather(
                    *(probe_model_listing(ctx) for ctx in providers.values())
                )
            )
            for result in results:
                if result.ok:
                    try:
                        self._store.upsert(result.provider, list(result.models))
                    except sqlite3.Error:
                        log.warning(
                            "capability store write failed for %s; "
                            "observations keep their previous state",
                            result.provider,
                            exc_info=True,
                        )
            return tuple(results)
        finally:
            self._last_run = time.time()
            self._in_flight = False


async def probe_status_only(
    ctx: ProviderContext,
    *,
    timeout: float = PROBE_TIMEOUT,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> tuple[int | None, float, str]:
    """The reachability probe: parity URL + credential, status only.

    Kept as its own entry point (not just a ``models`` probe that discards
    the parse) because the test endpoint promises status + latency without
    implying anything about the listing's shape.
    """
    url = models_probe_url(ctx)
    headers = models_probe_headers(ctx)
    factory = client_factory or (
        lambda: httpx.AsyncClient(timeout=httpx.Timeout(timeout))
    )
    start = time.monotonic()
    status, _body, detail = await _probe(
        url, headers, timeout=timeout, client_factory=factory
    )
    latency_ms = round((time.monotonic() - start) * 1000.0, 1)
    return (status if status >= 0 else None), latency_ms, detail
