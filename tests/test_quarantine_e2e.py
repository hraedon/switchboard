"""Quarantine through the real proxy, not just the tracker (Plan 023 WI-3/WI-4).

The unit tests prove the counting and the attribution. These prove the wiring:
that a provider-attributable failure repeated five times actually removes the
pair from service, that a caller-attributable one never does, that the provider
keeps serving its other models, and that an operator can see and release it.

The caller-attributable case is the one that matters most — it replays the
incident that prompted the feature (a Cloudflare 1010 block driven by the
client's own User-Agent) and asserts the estate stays up.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from switchboard.control import RoutingConfig
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.model_map import ModelMapManager
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.quarantine import QuarantineTracker
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource

_BODY = json.dumps(
    {"model": "shared-model", "messages": [{"role": "user", "content": "hi"}]}
).encode()


def _ctx(name: str, handler) -> ProviderContext:
    gate = PermitGate(initial_capacity=4)
    truth = NullTruthSource(provider="generic")
    return ProviderContext(
        name=name,
        upstream_url="https://upstream.invalid/v1",
        gate=gate,
        reconcile=ReconciliationLoop(
            truth_source=truth,
            gate=gate,
            max_concurrency=4,
            poll_interval=60.0,
            breaker_config=BreakerConfig(),
            provider_type="generic",
        ),
        truth_source=truth,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(5.0),
        ),
        api_key="k",
    )


class _Stream(httpx.AsyncByteStream):
    """A fresh, single-use byte stream per mocked response.

    Building responses with ``json=``/``text=`` materialises the content, and
    the proxy streams it — the second read raises ``StreamConsumed``. The
    existing integration tests use an explicit stream for the same reason.
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self) -> Any:
        yield self._payload

    async def aclose(self) -> None:
        pass


def _resp(status: int, payload: bytes, headers: dict[str, str]) -> httpx.Response:
    return httpx.Response(status, headers=headers, stream=_Stream(payload))


def _ok(request: httpx.Request) -> httpx.Response:
    return _resp(
        200,
        b'{"choices":[{"message":{"content":"OK"}}]}',
        {"content-type": "application/json"},
    )


def _api_500(request: httpx.Request) -> httpx.Response:
    return _resp(
        500, b'{"error":"boom"}', {"content-type": "application/json"}
    )


def _cloudflare_1010(request: httpx.Request) -> httpx.Response:
    """The real shape: HTML body, cf-ray, server: cloudflare."""
    return _resp(
        403,
        b"<!doctype html><html><body>The owner of this website has banned "
        b"your access based on your browser's signature.</body></html>",
        {
            "content-type": "text/html; charset=UTF-8",
            "server": "cloudflare",
            "cf-ray": "a285460cd8eb8b77",
        },
    )


async def _build(primary_handler, *, models: dict[str, dict[str, str]] | None = None):
    tracker = QuarantineTracker(threshold=5, clock=lambda: 1000.0)
    providers = {
        "alpha": _ctx("alpha", primary_handler),
        "beta": _ctx("beta", _ok),
    }
    route_table = RouteTableManager()
    route_table.set_default_providers(("alpha", "beta"))
    model_map = ModelMapManager(valid_providers=frozenset(providers))
    model_map.load_from_config(
        {
            "model": models
            or {
                "shared-model": {"alpha": "shared-model", "beta": "shared-model"},
                "other-model": {"alpha": "other-model"},
            }
        }
    )
    for ctx in providers.values():
        # Without a tick the reconcile loop has never polled, every provider is
        # UNKNOWN, and routing refuses to admit anything.
        await ctx.reconcile.tick()
    app = ProxyApp(
        providers=providers,
        route_table=route_table,
        routing_config=RoutingConfig(),
        model_map_mgr=model_map,
        quarantine=tracker,
        admin_token="admin-secret",
    )
    return app, tracker


async def _post(app: ProxyApp, model: str = "shared-model") -> tuple[int, bytes]:
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    ).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer client-key"),
        ],
        "query_string": b"",
        "client": ("10.0.0.1", 1234),
    }
    sent: list[dict[str, Any]] = []
    delivered = {"body": False}

    async def receive() -> dict[str, Any]:
        if not delivered["body"]:
            delivered["body"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        # Park, as a real server does. Replaying the body instead makes the
        # proxy's disconnect watcher spin forever.
        await asyncio.Future()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return start["status"], payload


@pytest.mark.asyncio
async def test_five_provider_failures_take_the_pair_out_of_service() -> None:
    """alpha 500s; after five, it stops being a candidate for that model and
    beta serves."""
    app, tracker = await _build(_api_500)
    for _ in range(5):
        await _post(app)
    assert tracker.is_quarantined("alpha", "shared-model") is True

    status, body = await _post(app)
    assert status == 200
    assert b"OK" in body
    # alpha took no part in that last request.
    assert app.metrics.forwarded_per_provider.get("beta", 0) >= 1


@pytest.mark.asyncio
async def test_the_cloudflare_incident_never_quarantines_anything() -> None:
    """The failure that prompted this feature. Twenty client-caused 403s must
    leave both providers in service — quarantining alpha would have sent the
    retry to beta, whose edge would reject the same header, until the model
    was unservable."""
    app, tracker = await _build(_cloudflare_1010)
    for _ in range(20):
        status, _ = await _post(app)
        assert status == 403
    assert tracker.entries() == [], "a caller-caused failure was quarantined"
    assert tracker.is_quarantined("alpha", "shared-model") is False


@pytest.mark.asyncio
async def test_quarantine_is_scoped_to_the_model() -> None:
    """alpha is broken for shared-model but still serves other-model."""
    app, tracker = await _build(_api_500)
    for _ in range(5):
        await _post(app, "shared-model")
    assert tracker.is_quarantined("alpha", "shared-model") is True
    assert tracker.is_quarantined("alpha", "other-model") is False


@pytest.mark.asyncio
async def test_all_providers_quarantined_says_so_explicitly() -> None:
    """Not a bare 503 that reads like quota exhaustion — the operator needs to
    know a human decision is what unblocks it."""
    app, tracker = await _build(_api_500)
    tracker.record  # noqa: B018 - readability
    for provider in ("alpha", "beta"):
        for _ in range(5):
            from switchboard.control import FailureAttribution

            tracker.record(
                provider, "shared-model", FailureAttribution.PROVIDER, status=500
            )
    status, body = await _post(app)
    assert status == 503
    payload = json.loads(body)
    assert payload["reason"] == "quarantined"
    assert sorted(payload["quarantined"]) == [
        "alpha/shared-model",
        "beta/shared-model",
    ]
    assert "DELETE /admin/quarantine" in payload["release_with"]


@pytest.mark.asyncio
async def test_a_success_before_the_fifth_failure_resets() -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # fail, fail, fail, fail, succeed, then fail forever
        return _ok(request) if calls["n"] == 5 else _api_500(request)

    app, tracker = await _build(flaky)
    for _ in range(8):
        await _post(app)
    # 4 failures, a success (reset), then 3 failures = never five in a row.
    assert tracker.is_quarantined("alpha", "shared-model") is False


@pytest.mark.asyncio
async def test_admin_can_see_and_release() -> None:
    app, tracker = await _build(_api_500)
    for _ in range(5):
        await _post(app)

    async def admin(method: str, path: str) -> tuple[int, dict[str, Any]]:
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [
                (b"authorization", b"Bearer admin-secret"),
                (b"sec-fetch-site", b"same-origin"),
            ],
            "query_string": b"",
            "client": ("10.0.0.1", 1234),
        }
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            await asyncio.Future()
            return {"type": "http.disconnect"}

        async def send(m: dict[str, Any]) -> None:
            sent.append(m)

        await app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(
            m.get("body", b"") for m in sent if m["type"] == "http.response.body"
        )
        return start["status"], (json.loads(body) if body else {})

    status, listing = await admin("GET", "/admin/quarantine")
    assert status == 200
    assert listing["entries"][0]["provider"] == "alpha"
    assert listing["entries"][0]["last_status"] == 500

    status, released = await admin(
        "DELETE", "/admin/quarantine/alpha/shared-model"
    )
    assert status == 200
    assert released["released"]["model"] == "shared-model"
    assert tracker.is_quarantined("alpha", "shared-model") is False

    # Releasing something that is not quarantined is a 404, not a silent 200.
    status, _ = await admin("DELETE", "/admin/quarantine/alpha/shared-model")
    assert status == 404


@pytest.mark.asyncio
async def test_changing_the_threshold_at_runtime_changes_the_behaviour() -> None:
    """`quarantine_threshold` is advertised as runtime-mutable. The tracker is
    built once at boot, so the PUT used to update the config object and nothing
    else: accepted, persisted, and inert until a restart replayed the overlay —
    the change appearing later, with no visible cause.

    This drives the real PUT and then counts real failures.
    """
    app, tracker = await _build(_api_500)

    body = json.dumps({"quarantine_threshold": 2}).encode()
    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/admin/config/routing",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
        "query_string": b"",
        "client": ("10.0.0.1", 1234),
    }
    sent: list[dict[str, Any]] = []
    delivered = {"body": False}

    async def receive() -> dict[str, Any]:
        if not delivered["body"]:
            delivered["body"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()
        return {"type": "http.disconnect"}

    async def send(m: dict[str, Any]) -> None:
        sent.append(m)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    assert tracker.threshold == 2

    # Two failures now suffice; under the boot-time 5 this would still be in
    # service and the assertion below would fail.
    await _post(app)
    assert tracker.is_quarantined("alpha", "shared-model") is False
    await _post(app)
    assert tracker.is_quarantined("alpha", "shared-model") is True


@pytest.mark.asyncio
async def test_release_requires_auth() -> None:
    app, tracker = await _build(_api_500)
    for _ in range(5):
        await _post(app)
    scope = {
        "type": "http",
        "method": "DELETE",
        "path": "/admin/quarantine/alpha/shared-model",
        "headers": [(b"sec-fetch-site", b"same-origin")],
        "query_string": b"",
        "client": ("10.0.0.1", 1234),
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        await asyncio.Future()
        return {"type": "http.disconnect"}

    async def send(m: dict[str, Any]) -> None:
        sent.append(m)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 403
    assert tracker.is_quarantined("alpha", "shared-model") is True


# ----------------------------------------------------------- WI-007 evidence
# A quarantine entry from a transport failure must carry evidence — the
# error type as detail — not null status and empty string. The original
# code's `detail = "forward failed"` branch was dead for the entire
# httpx.RequestError class because _forward caught it without re-raising.


def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


@pytest.mark.asyncio
async def test_transport_failure_carries_evidence() -> None:
    """WI-007: a transport failure (ConnectError) produces a quarantine entry
    with a non-empty detail naming the error type, not null/empty."""
    app, tracker = await _build(_connect_error)
    for _ in range(5):
        await _post(app)
    assert tracker.is_quarantined("alpha", "shared-model") is True
    entries = tracker.entries()
    alpha_entry = next(
        e for e in entries if e.provider == "alpha"
        and e.model == "shared-model"
    )
    # The evidence: detail must name the transport error, not be empty.
    assert alpha_entry.last_detail, (
        f"transport failure has empty detail: {alpha_entry!r}"
    )
    assert "ConnectError" in alpha_entry.last_detail, (
        f"detail does not name the error type: {alpha_entry.last_detail!r}"
    )
