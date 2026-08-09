#!/usr/bin/env python3
"""Soak/chaos harness for switchboard (Plan 017 WI-10).

Stands up fake upstreams in-process (no real network) and drives switchboard
through the failure modes that matter, asserting properties not timings:

  1. all-providers-exhausted — every upstream 429s; the estate gives up and
     surfaces an error, no leaked permits.
  2. 429-then-recover        — a provider 429s then recovers; traffic reroutes
     away and fails back.
  3. disconnect-mid-stream    — an upstream dies mid-SSE-stream; the client
     gets a clean close, no leaked permits.
  4. client-disconnect        — the client vanishes mid-request; the upstream
     is cancelled and the permit released.
  5. sustained-load           — concurrent load against the gates; no hung
     requests, no leaked permits at the end.

Run with:  uv run python scripts/chaos_harness.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

# Allow running directly (python scripts/chaos_harness.py) without `uv run`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from switchboard.control import DEFAULT_REROUTE_STATUSES, RoutingConfig
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource

_BODY = b'{"model":"m","messages":[{"role":"user","content":"hi"}]}'


# ---------------------------------------------------------------------------
# Streaming byte streams for fake upstream responses.
# ---------------------------------------------------------------------------


class _Stream(httpx.AsyncByteStream):
    """Yield fixed chunks; optionally block on a gate before chunk i>0."""

    def __init__(self, chunks: list[bytes], gate: asyncio.Event | None = None) -> None:
        self._chunks = list(chunks)
        self._gate = gate

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i, chunk in enumerate(self._chunks):
            if i > 0 and self._gate is not None:
                await self._gate.wait()
            yield chunk

    async def aclose(self) -> None:
        pass


class _BlockingStream(httpx.AsyncByteStream):
    """Return response headers immediately, then block forever on the body.

    Used so a client disconnect can be detected while the upstream is "slow".
    """

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await self._gate.wait()  # never set -> body never completes
        yield b'{"ok": true}'  # unreachable

    async def aclose(self) -> None:
        pass


class _FailingStream(httpx.AsyncByteStream):
    """Yield `fail_after` chunks then raise, simulating an upstream that dies
    mid-stream. The proxy surfaces this as a clean close (it catches
    httpx.RequestError) and must still release its permit."""

    def __init__(
        self, chunks: list[bytes], fail_after: int = 1,
        exc: Exception | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._fail_after = max(0, fail_after)
        self._exc = exc or httpx.ReadError("simulated upstream disconnect")

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i, chunk in enumerate(self._chunks):
            if i >= self._fail_after:
                raise self._exc
            yield chunk
        raise self._exc

    async def aclose(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fake upstream: an httpx.MockTransport handler with call tracking.
# ---------------------------------------------------------------------------


class FakeUpstream:
    """A callable MockTransport handler. Tracks how many times it was hit and
    builds a response from either a constant (status/body) or a per-call
    callable `fn(call_number, request)`."""

    def __init__(
        self,
        fn_or_status: int | Callable[[int, httpx.Request], httpx.Response],
        *,
        body: bytes | None = None,
        stream: bool = False,
        gate: asyncio.Event | None = None,
        fail_after: int | None = None,
        exc: Exception | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.calls = 0
        self.extra_headers = extra_headers
        if callable(fn_or_status):
            self._fn = fn_or_status
        else:
            status = fn_or_status

            def _const(calls: int, request: httpx.Request) -> httpx.Response:
                return self._build(
                    status, body or b'{"ok": true}', stream, gate, fail_after, exc
                )

            self._fn = _const

    def _build(
        self, status: int, body: bytes, stream: bool,
        gate: asyncio.Event | None, fail_after: int | None,
        exc: Exception | None,
    ) -> httpx.Response:
        ct = "text/event-stream" if stream else "application/json"
        headers = {"content-type": ct}
        if self.extra_headers:
            headers.update(self.extra_headers)
        if fail_after is not None:
            return httpx.Response(
                status,
                stream=_FailingStream([body], fail_after=fail_after, exc=exc),
                headers=headers,
            )
        if gate is not None:
            return httpx.Response(status, stream=_BlockingStream(gate), headers=headers)
        return httpx.Response(status, stream=_Stream([body]), headers=headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self._fn(self.calls, request)


# ---------------------------------------------------------------------------
# App construction + ASGI driving helpers (mirror tests/ patterns).
# ---------------------------------------------------------------------------


def _make_provider(name: str, upstream: FakeUpstream, *, capacity: int) -> ProviderContext:
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
        upstream_url=f"https://{name}.fake",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(upstream), timeout=None,
        ),
    )


def _make_app(
    providers: list[tuple[str, FakeUpstream, int]],
    *,
    reroute: int = 0,
    routing_config: RoutingConfig | None = None,
    queue_timeout: float = 2.0,
) -> tuple[ProxyApp, dict[str, ProviderContext]]:
    prov_dict = {
        name: _make_provider(name, upstream, capacity=cap)
        for name, upstream, cap in providers
    }
    app = ProxyApp(
        providers=prov_dict,
        route_table=RouteTableManager(default_providers=tuple(prov_dict)),
        routing_config=routing_config or RoutingConfig(),
        queue_timeout=queue_timeout,
        reroute_max_attempts=reroute,
        reroute_statuses=DEFAULT_REROUTE_STATUSES if reroute else None,
    )
    return app, prov_dict


async def _ready(*ctxs: ProviderContext) -> None:
    """Make each provider "ready" (first poll ok) without starting the
    background reconcile loop — deterministic, matches tests/."""
    for c in ctxs:
        await c.reconcile.tick()


def _scope(method: str = "POST", path: str = "/v1/chat/completions") -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }


async def _call(
    app: ProxyApp,
    method: str,
    path: str,
    body: bytes = b"",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, bytes, list[dict[str, Any]]]:
    """Drive one ASGI request through the app; return (status, body, messages)."""
    scope = _scope(method, path)
    if extra_headers is not None:
        scope["headers"] = extra_headers
    events = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict[str, Any]:
        if events:
            return events.pop(0)
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    messages: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        messages.append(msg)

    await app(scope, receive, send)

    status = 0
    resp_body = b""
    for m in messages:
        if m["type"] == "http.response.start":
            status = m["status"]
        elif m["type"] == "http.response.body":
            resp_body += m.get("body", b"")
    return status, resp_body, messages


def _response_starts(messages: list[dict[str, Any]]) -> int:
    return sum(1 for m in messages if m["type"] == "http.response.start")


async def _status_check(
    app: ProxyApp, prov_dict: dict[str, ProviderContext],
) -> tuple[bool, dict[str, Any]]:
    """Hit /status.json and verify every provider's in_flight (held) is 0.
    Also asserts the raw gate counters, which is the ground truth."""
    for name, ctx in prov_dict.items():
        if ctx.gate.held != 0:
            return False, {"error": f"gate leak: {name}.held={ctx.gate.held}"}
    status, body, _ = await _call(app, "GET", "/status.json")
    if status != 200:
        return False, {"error": f"/status.json returned {status}"}
    try:
        payload = json.loads(body)
    except Exception as exc:
        return False, {"error": f"/status.json invalid: {exc}"}
    for name, p in payload.get("providers", {}).items():
        if p.get("in_flight", 0) != 0:
            return False, {"error": f"/status.json leak: {name}.in_flight={p['in_flight']}"}
    return True, payload


# ---------------------------------------------------------------------------
# Scenarios.
# ---------------------------------------------------------------------------


async def scenario_all_exhausted() -> tuple[str, bool, str]:
    """Every upstream returns 429. With reroute enabled the estate tries each
    provider then gives up, surfacing the upstream error. No permits leak and
    the give-up counter increments (the estate-is-exhausted signal)."""
    a = FakeUpstream(429, body=b'{"error":"rate limited"}',
                     extra_headers={"retry-after": "2"})
    b = FakeUpstream(429, body=b'{"error":"rate limited"}',
                     extra_headers={"retry-after": "2"})
    app, provs = _make_app([("a", a, 3), ("b", b, 3)], reroute=1)
    await _ready(*provs.values())

    status, _, msgs = await _call(app, "POST", "/v1/chat/completions", _BODY)

    # The terminal attempt passes the last provider's 429 through untouched.
    ok_status = status == 429
    one_response = _response_starts(msgs) == 1

    held_ok, detail = await _status_check(app, provs)
    giveups = detail.get("routing_metrics", {}).get("usage_giveups_total", 0)
    ok_giveup = giveups >= 1

    passed = ok_status and one_response and held_ok and ok_giveup
    return (
        "all-providers-exhausted",
        passed,
        f"status={status} (expect 429), giveups={giveups}, "
        f"response_starts={_response_starts(msgs)}, held_ok={held_ok}",
    )


async def scenario_429_then_recover() -> tuple[str, bool, str]:
    """Primary 429s for its first two calls then recovers. Traffic reroutes to
    the healthy fallback, then fails back to the primary once it recovers and
    the dwell interval passes."""
    # Primary: 429 on calls 1 and 2, then 200. Track calls so the scenario can
    # assert the primary actually served once it recovered.
    primary_calls = {"n": 0}

    def primary(calls: int, request: httpx.Request) -> httpx.Response:
        primary_calls["n"] = calls
        if calls <= 2:
            return httpx.Response(
                429, stream=_Stream([b'{"error":"rate limited"}']),
                headers={"content-type": "application/json", "retry-after": "1"},
            )
        return httpx.Response(
            200, stream=_Stream([b'{"served_by":"primary"}']),
            headers={"content-type": "application/json"},
        )

    fallback = FakeUpstream(200, body=b'{"served_by":"fallback"}')

    # Short dwell so failback happens within the scenario. The inter-request
    # sleep (0.5s) comfortably exceeds this to avoid timing flakiness.
    rc = RoutingConfig(dwell_interval=0.2)
    # Wrap the callable in a FakeUpstream so the MockTransport handler tracks
    # calls and dispatches with the (calls, request) signature.
    primary_upstream = FakeUpstream(primary)
    app, provs = _make_app(
        [("primary", primary_upstream, 3), ("fallback", fallback, 3)],
        reroute=1, routing_config=rc,
    )
    await _ready(*provs.values())

    results: list[int] = []

    # Request 1: primary 429s -> reroute to fallback.
    s, _, _ = await _call(app, "POST", "/v1/chat/completions", _BODY)
    results.append(s)
    await asyncio.sleep(0.5)  # exceed dwell_interval

    # Request 2: primary still 429s (call 2) -> reroute to fallback.
    s, _, _ = await _call(app, "POST", "/v1/chat/completions", _BODY)
    results.append(s)
    await asyncio.sleep(0.5)

    # Primary has now recovered (call 3). Failback routes to primary.
    s, _, _ = await _call(app, "POST", "/v1/chat/completions", _BODY)
    results.append(s)

    all_ok = all(s == 200 for s in results)
    held_ok, detail = await _status_check(app, provs)
    reroutes = detail.get("routing_metrics", {}).get("usage_reroutes_total", 0)

    # The primary must have been hit at least 3 times (twice 429, once 200),
    # proving traffic failed back to it after recovery.
    primary_recovered = primary_calls["n"] >= 3
    passed = all_ok and held_ok and reroutes >= 2 and primary_recovered
    return (
        "429-then-recover",
        passed,
        f"responses={results} (expect all 200), reroutes={reroutes}, "
        f"primary_calls={primary_calls['n']} (>=3 means failback), "
        f"fallback_calls={fallback.calls}, held_ok={held_ok}",
    )


async def scenario_disconnect_mid_stream() -> tuple[str, bool, str]:
    """An upstream returns 200 + SSE headers, yields one chunk, then dies. The
    proxy must close the response cleanly and release its permit."""
    upstream = FakeUpstream(
        200, body=b"data: hello\n\n", stream=True, fail_after=1,
        exc=httpx.ReadError("simulated upstream disconnect"),
    )
    app, provs = _make_app([("a", upstream, 3)])
    await _ready(*provs.values())

    status, body, msgs = await _call(app, "POST", "/v1/chat/completions", _BODY)

    # Response started (200) and was closed; client got the partial chunk.
    started = status == 200
    one_response = _response_starts(msgs) == 1
    got_partial = b"hello" in body
    held_ok, _ = await _status_check(app, provs)

    passed = started and one_response and held_ok
    return (
        "disconnect-mid-stream",
        passed,
        f"status={status}, partial_chunk={got_partial}, "
        f"response_starts={_response_starts(msgs)}, held_ok={held_ok}",
    )


async def scenario_client_disconnect() -> tuple[str, bool, str]:
    """The client sends its body then immediately disconnects while the
    upstream is slow. The proxy detects the disconnect, cancels the upstream,
    and releases the permit."""
    gate = asyncio.Event()  # never set -> upstream blocks forever
    upstream = FakeUpstream(200, body=b'{"ok": true}', gate=gate)
    app, provs = _make_app([("a", upstream, 3)])
    await _ready(*provs.values())

    scope = _scope("POST", "/v1/chat/completions")
    sent_body = False

    async def receive() -> dict[str, Any]:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": _BODY, "more_body": False}
        # Client vanishes.
        return {"type": "http.disconnect"}

    messages: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        messages.append(msg)

    # The request must complete (not hang) despite the blocked upstream.
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=5.0)
        completed = True
    except TimeoutError:
        completed = False

    held_ok, _ = await _status_check(app, provs)
    passed = completed and held_ok
    return (
        "client-disconnect",
        passed,
        f"request_completed={completed}, held_ok={held_ok}",
    )


async def scenario_sustained_load() -> tuple[str, bool, str]:
    """Fire many concurrent requests at two healthy providers. Every request
    must succeed and all permits must be released when the dust settles."""
    a = FakeUpstream(200, body=b'{"ok":"a"}')
    b = FakeUpstream(200, body=b'{"ok":"b"}')
    app, provs = _make_app([("a", a, 4), ("b", b, 4)])
    await _ready(*provs.values())

    CONCURRENT = 24

    async def one() -> int:
        s, _, _ = await _call(app, "POST", "/v1/chat/completions", _BODY)
        return s

    results = await asyncio.gather(*[one() for _ in range(CONCURRENT)])
    all_ok = all(s == 200 for s in results)

    # Let any in-flight settling complete, then check permits.
    await asyncio.sleep(0.2)
    held_ok, detail = await _status_check(app, provs)
    forwarded = detail.get("routing_metrics", {}).get("forwarded_per_provider", {})
    total_forwarded = sum(forwarded.values())

    passed = all_ok and held_ok and total_forwarded == CONCURRENT
    return (
        "sustained-load",
        passed,
        f"{sum(1 for s in results if s == 200)}/{CONCURRENT} succeeded, "
        f"forwarded={forwarded}, held_ok={held_ok}",
    )


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------


SCENARIOS = [
    scenario_all_exhausted,
    scenario_429_then_recover,
    scenario_disconnect_mid_stream,
    scenario_client_disconnect,
    scenario_sustained_load,
]


async def _run(scenario) -> tuple[str, bool, str]:
    name = scenario.__name__.replace("scenario_", "").replace("_", "-")
    try:
        passed, detail = True, ""
        _, passed, detail = await asyncio.wait_for(scenario(), timeout=20.0)
        return name, passed, detail
    except TimeoutError:
        return name, False, "TIMEOUT (request hung)"
    except Exception as exc:
        return name, False, f"{type(exc).__name__}: {exc}"


async def main() -> int:
    print("switchboard chaos harness (Plan 017 WI-10)")
    print("=" * 60)
    results = []
    for scenario in SCENARIOS:
        name, passed, detail = await _run(scenario)
        results.append((name, passed, detail))
        flag = "PASS" if passed else "FAIL"
        print(f"[{flag}] {name}: {detail}")

    print("=" * 60)
    passed_count = sum(1 for _, p, _ in results if p)
    total = len(results)
    print(f"{passed_count}/{total} scenarios passed")
    return 0 if passed_count == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
