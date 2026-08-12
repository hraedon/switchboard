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
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from switchboard.control import (
    DEFAULT_REROUTE_STATUSES,
    RoutingConfig,
    should_reroute,
)
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource

_BODY = b'{"model":"m","messages":[]}'


def _ctx(
    name: str, handler: Any, *, capacity: int = 4
) -> ProviderContext:
    gate = PermitGate(initial_capacity=capacity)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        max_concurrency=capacity,
        provider_type="generic",
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


class _Stream(httpx.AsyncByteStream):
    """Minimal async byte stream.

    ``httpx.Response(content=...)`` yields an already-consumed body, which the
    proxy's ``aiter_raw()`` loop cannot read — the response has to arrive as a
    stream to exercise the real forwarding path.
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._payload

    async def aclose(self) -> None:
        pass


def _responder(status: int, *, body: bytes = b'{"ok":true}', headers: dict | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            status, stream=_Stream(body), headers=headers or {}
        )

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


@pytest.mark.asyncio
class TestRerouteSafetyGaps:
    """The cases an adversarial review said the first cut could get wrong."""

    async def test_no_reroute_after_client_disconnect(self) -> None:
        """A client that has gone away must not cause a second upstream call.

        Opening a fresh request on behalf of a vanished client is the
        phantom-request failure the streaming core exists to prevent.
        """
        exhausted = _responder(429)
        healthy = _responder(200)
        a, b = _ctx("a", exhausted), _ctx("b", healthy)
        await _ready(a, b)

        async def receive() -> dict[str, Any]:
            # Body, then an immediate disconnect while the upstream is answering.
            if not getattr(receive, "sent", False):
                receive.sent = True  # type: ignore[attr-defined]
                return {"type": "http.request", "body": _BODY, "more_body": False}
            return {"type": "http.disconnect"}

        _msgs, send = _sender()
        await _app({"a": a, "b": b})(_scope(), receive, send)

        assert len(healthy.seen) == 0
        assert a.gate.held == 0
        assert b.gate.held == 0

    async def test_affinity_follows_the_provider_that_served(self) -> None:
        """Otherwise the conversation keeps paying for the exhausted provider."""
        a, b = _ctx("a", _responder(429)), _ctx("b", _responder(200))
        await _ready(a, b)
        app = _app({"a": a, "b": b})
        _msgs, send = _sender()

        await app(_scope(), _receive_body(), send)

        pinned = [entry.provider for entry in app._affinity.values()]
        assert pinned == ["b"]

    async def test_queue_only_candidate_counts_as_an_alternative(self) -> None:
        """A busy-but-alive provider is still somewhere to go.

        With b's only permit already held, b can only be reached through the
        queue; treating it as "no alternative" would strand the request on the
        exhausted provider.
        """
        exhausted = _responder(429)
        healthy = _responder(200)
        a = _ctx("a", exhausted)
        b = _ctx("b", healthy, capacity=1)
        await _ready(a, b)
        assert await b.gate.acquire(timeout=0.0)

        async def release_soon() -> None:
            await asyncio.sleep(0.05)
            await b.gate.release()

        releaser = asyncio.ensure_future(release_soon())
        msgs, send = _sender()
        await _app({"a": a, "b": b})(_scope(), _receive_body(), send)
        await releaser

        assert _statuses(msgs) == [200]
        assert len(healthy.seen) == 1

    async def test_terminal_attempt_passes_the_upstream_body_through(self) -> None:
        """When there is nowhere left to go, the client gets the real response.

        The upstream's own body is more useful than a switchboard-authored one,
        and keeping it means response bodies are never inspected or replaced on
        the ordinary path.
        """
        upstream_body = b'{"error":{"message":"quota exceeded","type":"insufficient_quota"}}'
        a = _ctx("a", _responder(429, body=upstream_body))
        await _ready(a)
        msgs, send = _sender()

        await _app({"a": a})(_scope(), _receive_body(), send)

        assert _statuses(msgs) == [429]
        body = b"".join(
            m.get("body", b"") for m in msgs if m["type"] == "http.response.body"
        )
        assert body == upstream_body


@pytest.mark.asyncio
class TestUsageGiveUpObservability:
    """WI-4: the estate-is-exhausted condition must be observable.

    When every eligible provider returns a usage error, the reroute has
    nowhere to go and switchboard surfaces the upstream's status.  That give-up
    is the one exhaustion condition routing cannot fix, so it is counted and
    logged at WARNING — and a successful reroute must not count.  Both give-up
    paths are covered: the terminal attempt whose probe was never armed (the
    upstream response passes through untouched) and the probe-that-fired with
    no further provider admitted (a synthesized error).
    """

    def _give_up_logs(
        self, caplog: pytest.LogCaptureFixture,
    ) -> list[logging.LogRecord]:
        return [
            r for r in caplog.records
            if r.getMessage().startswith("usage-error give-up:")
        ]

    async def test_terminal_attempt_counts_and_logs_once(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Give-up path 1: the terminal attempt's probe is never armed, so the
        upstream's response passes through untouched — still a give-up."""
        caplog.set_level(logging.WARNING, logger="switchboard.proxy")
        a, b = _ctx("a", _responder(429)), _ctx("b", _responder(429))
        await _ready(a, b)
        app = _app({"a": a, "b": b})
        msgs, send = _sender()

        await app(_scope(), _receive_body(), send)

        assert _statuses(msgs) == [429]
        assert app._metrics.usage_giveups_total == 1
        give_ups = self._give_up_logs(caplog)
        assert len(give_ups) == 1
        message = give_ups[0].getMessage()
        assert "a=429" in message
        assert "b=429" in message

    async def test_unadmittable_alternative_counts_and_logs_once(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Give-up path 2: the probe fired but no further provider could be
        admitted, so the error is synthesised under the upstream's status."""
        caplog.set_level(logging.WARNING, logger="switchboard.proxy")
        a = _ctx("a", _responder(429))
        b = _ctx("b", _responder(200), capacity=1)
        await _ready(a, b)
        # Occupy b's only permit: the plan keeps it as queue candidate, but
        # admission cannot reach it within the queue timeout.
        assert await b.gate.acquire(timeout=0.0)
        app = _app({"a": a, "b": b})
        msgs, send = _sender()

        await app(_scope(), _receive_body(), send)

        assert _statuses(msgs) == [429]
        assert app._metrics.usage_giveups_total == 1
        give_ups = self._give_up_logs(caplog)
        assert len(give_ups) == 1
        assert "a=429" in give_ups[0].getMessage()
        await b.gate.release()

    async def test_successful_reroute_does_not_count(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger="switchboard.proxy")
        a, b = _ctx("a", _responder(429)), _ctx("b", _responder(200))
        await _ready(a, b)
        app = _app({"a": a, "b": b})
        msgs, send = _sender()

        await app(_scope(), _receive_body(), send)

        assert _statuses(msgs) == [200]
        assert app._metrics.usage_giveups_total == 0
        assert self._give_up_logs(caplog) == []

    async def test_feature_disabled_does_not_count(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """With the reroute off, a 429 is plain pass-through behaviour, not a
        give-up: switchboard never attempted to route around it."""
        caplog.set_level(logging.WARNING, logger="switchboard.proxy")
        a = _ctx("a", _responder(429))
        await _ready(a)
        app = _app({"a": a}, attempts=0)
        msgs, send = _sender()

        await app(_scope(), _receive_body(), send)

        assert _statuses(msgs) == [429]
        assert app._metrics.usage_giveups_total == 0
        assert self._give_up_logs(caplog) == []


@pytest.mark.asyncio
class TestPerProviderCredentials:
    """Cross-vendor failover is only real if the new upstream gets its own key.

    Every provider issues its own credential, so forwarding the client's
    verbatim would turn "provider A is out of quota" into "provider B says
    401" — strictly worse than the problem rerouting exists to solve.
    """

    async def test_each_provider_receives_its_own_credential(self) -> None:
        exhausted = _responder(429)
        healthy = _responder(200)
        a, b = _ctx("a", exhausted), _ctx("b", healthy)
        a.api_key, b.api_key = "key-for-a", "key-for-b"
        await _ready(a, b)
        _msgs, send = _sender()

        scope = _scope()
        scope["headers"] = [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer client-supplied-key"),
        ]
        await _app({"a": a, "b": b})(scope, _receive_body(), send)

        assert exhausted.seen[0].headers["authorization"] == "Bearer key-for-a"
        assert healthy.seen[0].headers["authorization"] == "Bearer key-for-b"

    async def test_no_key_configured_passes_the_client_header_through(self) -> None:
        """Single-vendor deployments keep byte-identical egress."""
        healthy = _responder(200)
        a = _ctx("a", healthy)
        await _ready(a)
        _msgs, send = _sender()

        scope = _scope()
        scope["headers"] = [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer client-supplied-key"),
        ]
        await _app({"a": a})(scope, _receive_body(), send)

        assert (
            healthy.seen[0].headers["authorization"] == "Bearer client-supplied-key"
        )

    async def test_alternate_auth_header_and_prefix(self) -> None:
        healthy = _responder(200)
        a = _ctx("a", healthy)
        a.api_key, a.auth_header, a.auth_prefix = "raw-key", "x-api-key", ""
        await _ready(a)
        _msgs, send = _sender()

        await _app({"a": a})(_scope(), _receive_body(), send)

        assert healthy.seen[0].headers["x-api-key"] == "raw-key"

    async def test_client_credential_never_survives_a_header_style_change(self) -> None:
        """The leak the credential feature exists to prevent, in its subtlest form.

        Stripping only the provider's own auth header lets a client's
        `Authorization` ride along to an upstream that reads `x-api-key` —
        handing one vendor's key to another while appearing to work.
        """
        healthy = _responder(200)
        a = _ctx("a", healthy)
        a.api_key, a.auth_header, a.auth_prefix = "provider-key", "x-api-key", ""
        await _ready(a)
        _msgs, send = _sender()

        scope = _scope()
        scope["headers"] = [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer OTHER-VENDOR-KEY"),
        ]
        await _app({"a": a})(scope, _receive_body(), send)

        sent = healthy.seen[0].headers
        assert sent["x-api-key"] == "provider-key"
        assert "authorization" not in sent

    async def test_reverse_header_style_also_strips(self) -> None:
        healthy = _responder(200)
        a = _ctx("a", healthy)
        a.api_key = "provider-key"  # authorization/Bearer defaults
        await _ready(a)
        _msgs, send = _sender()

        scope = _scope()
        scope["headers"] = [
            (b"content-type", b"application/json"),
            (b"x-api-key", b"OTHER-VENDOR-KEY"),
        ]
        await _app({"a": a})(scope, _receive_body(), send)

        sent = healthy.seen[0].headers
        assert sent["authorization"] == "Bearer provider-key"
        assert "x-api-key" not in sent


@pytest.mark.asyncio
class TestPermitOwnership:
    """`held == 0` at the end cannot see a permit released twice.

    A queue acquisition handed to the caller and then released by the helper
    leaves the caller forwarding without capacity; its own later release then
    decrements a DIFFERENT request's permit. The gate looks balanced the whole
    time, which is exactly why these need asserting mid-flight.
    """

    async def test_queued_acquisition_is_not_released_by_the_helper(self) -> None:
        healthy = _responder(200)
        a = _ctx("a", healthy, capacity=1)
        await _ready(a)
        # Occupy the only permit so admission must go through the queue path.
        assert await a.gate.acquire(timeout=0.0)

        async def free_it() -> None:
            await asyncio.sleep(0.05)
            await a.gate.release()

        releaser = asyncio.ensure_future(free_it())
        _msgs, send = _sender()
        await _app({"a": a})(_scope(), _receive_body(), send)
        await releaser

        # If the helper had released the transferred permit, the proxy's own
        # release would have driven this negative — sluice clamps at zero and
        # logs, so the visible symptom is a gate that has silently gained
        # capacity. Balanced here means ownership transferred exactly once.
        assert a.gate.held == 0

    async def test_reroute_leaves_every_gate_balanced(self) -> None:
        a, b = _ctx("a", _responder(429)), _ctx("b", _responder(200))
        await _ready(a, b)
        _msgs, send = _sender()

        await _app({"a": a, "b": b})(_scope(), _receive_body(), send)

        assert a.gate.held == 0
        assert b.gate.held == 0

    async def test_affinity_is_not_pinned_when_the_reroute_also_fails(self) -> None:
        """A pin must record who served, not who was merely selected."""
        a, b = _ctx("a", _responder(429)), _ctx("b", _responder(429))
        await _ready(a, b)
        app = _app({"a": a, "b": b})
        _msgs, send = _sender()

        await app(_scope(), _receive_body(), send)

        assert list(app._affinity.values()) == []


def _mutable_responder(status: int):
    """Like _responder, but the status can be flipped mid-test."""
    state = {"status": status}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            state["status"], stream=_Stream(b'{"ok":true}'), headers={}
        )

    handler.state = state  # type: ignore[attr-defined]
    handler.seen = seen  # type: ignore[attr-defined]
    return handler


@pytest.mark.asyncio
async def test_reroute_to_effective_primary_retires_the_stale_pin() -> None:
    """Plan 026 review, blocking B1. With the model map excluding the
    configured primary (alpha), beta is the EFFECTIVE primary. A pinned
    fallback (gamma) that 429s and reroutes onto beta must not keep its
    pin — pre-fix the re-pin guard (`rerouted_to != primary`) skipped the
    effective-primary case, so the dwell pin kept fronting the exhausted
    fallback and every request paid its failed round trip."""
    from switchboard.model_map import ModelMapManager

    alpha_handler = _responder(500)  # excluded by the model map; never called
    beta = _mutable_responder(429)   # effective primary; exhausted at first
    gamma = _mutable_responder(200)  # healthy fallback at first

    a = _ctx("alpha", alpha_handler)
    b = _ctx("beta", beta)
    g = _ctx("gamma", gamma)
    await _ready(a, b, g)

    mm = ModelMapManager()
    mm.set_model("m", {"beta": "m", "gamma": "m"})
    app = ProxyApp(
        providers={"alpha": a, "beta": b, "gamma": g},
        route_table=RouteTableManager(
            default_providers=("alpha", "beta", "gamma"),
        ),
        routing_config=RoutingConfig(),
        model_map_mgr=mm,
        queue_timeout=1.0,
        reroute_max_attempts=1,
    )

    async def send_one() -> None:
        msgs, send = _sender()
        await app(_scope(), _receive_body(), send)
        assert _statuses(msgs)[-0:] is not None  # response completed

    # Request 1: beta 429s, gamma serves via reroute → pin lands on gamma.
    await send_one()
    assert [af.provider for af in app._affinity.values()] == ["gamma"]

    # gamma is now exhausted; beta recovered.
    gamma.state["status"] = 429
    beta.state["status"] = 200

    # Request 2: the pin fronts gamma, gamma 429s, the reroute lands on the
    # EFFECTIVE primary (beta). The stale gamma pin must be retired.
    await send_one()
    assert all(af.provider != "gamma" for af in app._affinity.values())

    # Request 3: straight to beta — gamma pays no further failed round trip.
    gamma_calls = len(gamma.seen)
    await send_one()
    assert len(gamma.seen) == gamma_calls
    assert len(alpha_handler.seen) == 0

    for ctx in (a, b, g):
        await ctx.http_client.aclose()
