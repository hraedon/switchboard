"""Usage-error reroute — Plan 010 reactive half.

A provider that answers "I am out of quota" (429/402/503/529) should not end
the request: switchboard holds the client's first byte until an upstream has
actually accepted the work, so it can hand the request to somebody else. These
tests pin the safety properties, not just the happy path — the dangerous
outcomes are a retry that duplicates a partially-sent response, a retry that
replays a body it no longer has, and a reroute that fires for errors which are
the client's fault rather than the provider's.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from sluice.control import BreakerConfig, ControllerConfig
from sluice.gate import PermitGate
from sluice.providers import NullTruthSource
from sluice.reconcile import ReconciliationLoop

from switchboard.control import (
    DEFAULT_REROUTE_STATUSES,
    RoutingConfig,
    should_reroute,
)
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.route_table import RouteTableManager

_BODY = b'{"model":"m","messages":[]}'


def _ctx(
    name: str, handler: Any, *, capacity: int = 4
) -> ProviderContext:
    gate = PermitGate(initial_capacity=capacity)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        controller_config=ControllerConfig(target=capacity),
        breaker_config=BreakerConfig(),
    )
    return ProviderContext(
        name=name,
        upstream_url=f"https://{name}.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=None
        ),
    )


def _responder(status: int, *, body: bytes = b'{"ok":true}', headers: dict | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=body, headers=headers or {})

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def _scope() -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }


def _receive_body(body: bytes = _BODY):
    """ASGI receive that yields the body once, then simply never returns.

    Returning ``http.disconnect`` straight after the body would trip the
    proxy's disconnect watcher and abort the response before it is sent —
    the client in these tests is still waiting.
    """
    events = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict[str, Any]:
        if events:
            return events.pop(0)
        await asyncio.sleep(30)
        return {"type": "http.disconnect"}

    return receive


def _sender() -> tuple[list[dict[str, Any]], Any]:
    msgs: list[dict[str, Any]] = []

    async def send(m: dict[str, Any]) -> None:
        msgs.append(m)

    return msgs, send


def _app(providers: dict[str, ProviderContext], *, attempts: int = 1) -> ProxyApp:
    return ProxyApp(
        providers=providers,
        route_table=RouteTableManager(default_providers=tuple(providers)),
        routing_config=RoutingConfig(),
        queue_timeout=1.0,
        reroute_max_attempts=attempts,
    )


async def _ready(*ctxs: ProviderContext) -> None:
    for c in ctxs:
        await c.reconcile.tick()


def _statuses(msgs: list[dict[str, Any]]) -> list[int]:
    return [m["status"] for m in msgs if m["type"] == "http.response.start"]


# ── the pure rule ────────────────────────────────────────────────────────


class TestShouldReroute:
    def _base(self, **over: Any) -> dict[str, Any]:
        base = dict(
            status=429,
            reroute_statuses=DEFAULT_REROUTE_STATUSES,
            reroutes_done=0,
            max_attempts=1,
            body_replayable=True,
            response_started=False,
            alternatives_remain=True,
        )
        base.update(over)
        return base

    def test_usage_statuses_reroute(self) -> None:
        for status in (402, 429, 503, 529):
            assert should_reroute(**self._base(status=status))

    def test_client_and_server_faults_do_not(self) -> None:
        # 400/401/404 are the caller's problem; 500/502 are a broken upstream.
        # Spraying either across the estate would multiply the failure.
        for status in (200, 400, 401, 404, 500, 502):
            assert not should_reroute(**self._base(status=status))

    def test_never_after_the_first_byte(self) -> None:
        assert not should_reroute(**self._base(response_started=True))

    def test_never_without_a_replayable_body(self) -> None:
        assert not should_reroute(**self._base(body_replayable=False))

    def test_never_without_an_alternative(self) -> None:
        assert not should_reroute(**self._base(alternatives_remain=False))

    def test_bounded_by_attempts(self) -> None:
        assert not should_reroute(**self._base(reroutes_done=1, max_attempts=1))


# ── end to end through the proxy ─────────────────────────────────────────


@pytest.mark.asyncio
class TestRerouteThroughProxy:
    async def test_quota_error_is_served_by_another_provider(self) -> None:
        exhausted = _responder(429)
        healthy = _responder(200)
        a, b = _ctx("a", exhausted), _ctx("b", healthy)
        await _ready(a, b)
        app = _app({"a": a, "b": b})
        msgs, send = _sender()

        await app(_scope(), _receive_body(), send)

        # The client never learns the first provider was exhausted.
        assert _statuses(msgs) == [200]
        assert len(exhausted.seen) == 1
        assert len(healthy.seen) == 1
        assert app._metrics.usage_reroutes_total == 1
        assert app._metrics.usage_reroutes_from["a"] == 1

    async def test_replayed_body_is_intact(self) -> None:
        exhausted = _responder(429)
        healthy = _responder(200)
        a, b = _ctx("a", exhausted), _ctx("b", healthy)
        await _ready(a, b)
        _msgs, send = _sender()

        await _app({"a": a, "b": b})(_scope(), _receive_body(), send)

        assert healthy.seen[0].content == _BODY

    async def test_exhausted_estate_surfaces_the_upstream_status(self) -> None:
        a, b = _ctx("a", _responder(429)), _ctx("b", _responder(429))
        await _ready(a, b)
        msgs, send = _sender()

        await _app({"a": a, "b": b})(_scope(), _receive_body(), send)

        # Status is preserved so the client's own backoff still sees the truth.
        assert _statuses(msgs) == [429]

    async def test_retry_after_is_preserved_when_giving_up(self) -> None:
        a = _ctx("a", _responder(429, headers={"retry-after": "30"}))
        b = _ctx("b", _responder(429, headers={"retry-after": "30"}))
        await _ready(a, b)
        msgs, send = _sender()

        await _app({"a": a, "b": b})(_scope(), _receive_body(), send)

        start = next(m for m in msgs if m["type"] == "http.response.start")
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        assert headers.get("retry-after") == "30"

    async def test_disabled_by_default_passes_the_error_through(self) -> None:
        exhausted = _responder(429)
        healthy = _responder(200)
        a, b = _ctx("a", exhausted), _ctx("b", healthy)
        await _ready(a, b)
        msgs, send = _sender()

        # attempts=0 — the shipped default.
        await _app({"a": a, "b": b}, attempts=0)(_scope(), _receive_body(), send)

        assert _statuses(msgs) == [429]
        assert len(healthy.seen) == 0

    async def test_server_error_is_not_rerouted(self) -> None:
        broken = _responder(500)
        healthy = _responder(200)
        a, b = _ctx("a", broken), _ctx("b", healthy)
        await _ready(a, b)
        msgs, send = _sender()

        await _app({"a": a, "b": b})(_scope(), _receive_body(), send)

        assert _statuses(msgs) == [500]
        assert len(healthy.seen) == 0

    async def test_attempts_are_bounded(self) -> None:
        a, b, c = (
            _ctx("a", _responder(429)),
            _ctx("b", _responder(429)),
            _ctx("c", _responder(200)),
        )
        await _ready(a, b, c)
        msgs, send = _sender()

        # One retry only: a → b, then give up even though c would serve.
        await _app({"a": a, "b": b, "c": c}, attempts=1)(
            _scope(), _receive_body(), send
        )

        assert _statuses(msgs) == [429]

    async def test_two_attempts_reach_the_third_provider(self) -> None:
        a, b = _ctx("a", _responder(429)), _ctx("b", _responder(429))
        healthy = _responder(200)
        c = _ctx("c", healthy)
        await _ready(a, b, c)
        msgs, send = _sender()

        await _app({"a": a, "b": b, "c": c}, attempts=2)(
            _scope(), _receive_body(), send
        )

        assert _statuses(msgs) == [200]
        assert len(healthy.seen) == 1

    async def test_permits_are_released_on_every_attempt(self) -> None:
        a, b = _ctx("a", _responder(429)), _ctx("b", _responder(200))
        await _ready(a, b)
        _msgs, send = _sender()

        await _app({"a": a, "b": b})(_scope(), _receive_body(), send)

        # A reroute must not strand the failed provider's permit, or the
        # estate leaks capacity every time a provider runs dry.
        assert a.gate.held == 0
        assert b.gate.held == 0
