from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from switchboard.control import RoutingConfig
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource


def _make_provider_context(
    name: str = "test",
    upstream_url: str = "https://upstream.example.com",
    capacity: int = 1,
    http_client: httpx.AsyncClient | None = None,
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
        upstream_url=upstream_url,
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=http_client or httpx.AsyncClient(),
    )


async def _ready(ctx: ProviderContext) -> None:
    await ctx.reconcile.tick()


def _make_scope(
    method: str = "POST",
    path: str = "/v1/chat/completions",
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": headers or [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }


def _make_send() -> tuple[list[dict[str, Any]], Any]:
    messages: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        messages.append(msg)

    return messages, send


def _make_app(
    providers: dict[str, ProviderContext],
    *,
    max_request_body_bytes: int | None = None,
    upstream_idle_timeout: float | None = None,
    queue_timeout: float = 30.0,
) -> ProxyApp:
    route_table = RouteTableManager(default_providers=tuple(providers.keys()))
    return ProxyApp(
        providers=providers,
        route_table=route_table,
        routing_config=RoutingConfig(),
        queue_timeout=queue_timeout,
        max_request_body_bytes=max_request_body_bytes,
        upstream_idle_timeout=upstream_idle_timeout,
    )


def _mock_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=None,
    )


class _ScriptedReceive:
    """ASGI receive that replays scripted events, then optionally delays
    before returning http.disconnect."""

    def __init__(
        self, events: list[dict[str, Any]], *, delay: float = 0.0
    ) -> None:
        self._events = list(events)
        self._index = 0
        self._delay = delay

    async def __call__(self) -> dict[str, Any]:
        if self._index < len(self._events):
            event = self._events[self._index]
            self._index += 1
            return event
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return {"type": "http.disconnect"}


class _SlowStream(httpx.AsyncByteStream):
    """AsyncByteStream that yields chunks with a fixed delay between each."""

    def __init__(self, chunks: list[bytes], delay: float) -> None:
        self._chunks = chunks
        self._delay = delay

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            await asyncio.sleep(self._delay)
            yield chunk

    async def aclose(self) -> None:
        pass


class _BlockingStream(httpx.AsyncByteStream):
    """Yields one chunk then blocks forever."""

    def __init__(self, first_chunk: bytes = b"data") -> None:
        self._first_chunk = first_chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first_chunk
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_queue_timeout_no_permits_no_upstream_call() -> None:
    """When no permits are available, queue wait times out without calling upstream."""
    upstream_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_called
        upstream_called = True
        return httpx.Response(200)

    ctx = _make_provider_context(capacity=1, http_client=_mock_client(handler))
    await _ready(ctx)
    await ctx.gate.acquire(timeout=0.0)

    app = _make_app({"test": ctx}, queue_timeout=0.2)
    scope = _make_scope()
    receive = _ScriptedReceive([{"type": "http.disconnect"}])
    _, send = _make_send()

    await app(scope, receive, send)

    assert ctx.gate.held == 1
    assert not upstream_called

    await ctx.gate.release()
    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_disconnect_during_body_upload() -> None:
    """Client disconnects during body upload; upstream cancelled, permit released."""
    handler_started = False
    handler_returned = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal handler_started, handler_returned
        handler_started = True
        await asyncio.Event().wait()
        handler_returned = True
        return httpx.Response(200)

    ctx = _make_provider_context(capacity=1, http_client=_mock_client(handler))
    await _ready(ctx)

    app = _make_app({"test": ctx})
    scope = _make_scope()
    receive = _ScriptedReceive([
        {"type": "http.request", "body": b"chunk1", "more_body": True},
        {"type": "http.disconnect"},
    ])
    _, send = _make_send()

    await app(scope, receive, send)

    assert ctx.gate.held == 0
    assert handler_started
    assert not handler_returned

    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_disconnect_while_awaiting_headers() -> None:
    """Client disconnects after upload, before headers; upstream cancelled."""
    handler_started = False
    handler_returned = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal handler_started, handler_returned
        handler_started = True
        await asyncio.Event().wait()
        handler_returned = True
        return httpx.Response(200)

    ctx = _make_provider_context(capacity=1, http_client=_mock_client(handler))
    await _ready(ctx)

    app = _make_app({"test": ctx})
    scope = _make_scope()
    receive = _ScriptedReceive(
        [{"type": "http.request", "body": b'{"model":"test"}', "more_body": False}],
        delay=0.1,
    )
    _, send = _make_send()

    await app(scope, receive, send)

    assert ctx.gate.held == 0
    assert handler_started
    assert not handler_returned

    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_disconnect_during_response_streaming() -> None:
    """Client disconnects mid-stream; upstream stream cancelled, permit released."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_SlowStream([b"chunk1", b"chunk2", b"chunk3"], 0.1),
        )

    ctx = _make_provider_context(capacity=1, http_client=_mock_client(handler))
    await _ready(ctx)

    app = _make_app({"test": ctx})
    scope = _make_scope()
    receive = _ScriptedReceive(
        [{"type": "http.request", "body": b'{"model":"test"}', "more_body": False}],
        delay=0.2,
    )
    messages, send = _make_send()

    await app(scope, receive, send)

    assert ctx.gate.held == 0
    statuses = [m["status"] for m in messages if m["type"] == "http.response.start"]
    assert statuses == [200]
    body_msgs = [m for m in messages if m["type"] == "http.response.body"]
    assert len(body_msgs) >= 1
    assert all(m.get("more_body") is True for m in body_msgs)

    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_body_size_limit_enforcement() -> None:
    """Body exceeding max_request_body_bytes triggers 413; permit released."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    ctx = _make_provider_context(capacity=1, http_client=_mock_client(handler))
    await _ready(ctx)

    app = _make_app({"test": ctx}, max_request_body_bytes=10)
    scope = _make_scope()
    receive = _ScriptedReceive([
        {"type": "http.request", "body": b"x" * 5, "more_body": True},
        {"type": "http.request", "body": b"y" * 10, "more_body": True},
    ])
    messages, send = _make_send()

    await app(scope, receive, send)

    assert ctx.gate.held == 0
    statuses = [m["status"] for m in messages if m["type"] == "http.response.start"]
    assert statuses == [413]

    await ctx.http_client.aclose()


@pytest.mark.asyncio
async def test_upstream_idle_timeout() -> None:
    """Upstream stops sending data; idle timeout fires; permit released."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_BlockingStream(b"first_chunk"))

    ctx = _make_provider_context(capacity=1, http_client=_mock_client(handler))
    await _ready(ctx)

    app = _make_app({"test": ctx}, upstream_idle_timeout=0.2)
    scope = _make_scope()
    receive = _ScriptedReceive(
        [{"type": "http.request", "body": b'{"model":"test"}', "more_body": False}],
        delay=5.0,
    )
    messages, send = _make_send()

    await app(scope, receive, send)

    assert ctx.gate.held == 0
    statuses = [m["status"] for m in messages if m["type"] == "http.response.start"]
    assert statuses == [200]
    body_msgs = [m for m in messages if m["type"] == "http.response.body"]
    assert len(body_msgs) >= 1

    await ctx.http_client.aclose()
