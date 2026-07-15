from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sluice.control import BreakerConfig, ControllerConfig
from sluice.gate import PermitGate
from sluice.providers import NullTruthSource
from sluice.reconcile import ReconciliationLoop

from switchboard.admin import (
    _build_status_payload,
    handle_config_get,
    handle_healthz,
    handle_provider_override,
    handle_readyz,
    handle_route_add,
    handle_route_delete,
    handle_route_list,
)
from switchboard.control import RoutingConfig
from switchboard.providers import ProviderContext
from switchboard.proxy import RoutingMetrics
from switchboard.route_table import RouteTableManager


def _make_provider_context(name: str = "test") -> ProviderContext:
    gate = PermitGate(initial_capacity=0)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        controller_config=ControllerConfig(target=1),
        breaker_config=BreakerConfig(),
    )
    return ProviderContext(
        name=name,
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=httpx.AsyncClient(),
    )


def _make_scope(
    method: str = "GET",
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }


def _make_receive(body: bytes = b""):
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def _make_send():
    messages: list[dict] = []

    async def send(msg: dict) -> None:
        messages.append(msg)

    return messages, send


def _parse_response(messages: list[dict]) -> tuple[int, bytes]:
    status = 0
    body = b""
    for msg in messages:
        if msg["type"] == "http.response.start":
            status = msg["status"]
        elif msg["type"] == "http.response.body":
            body += msg.get("body", b"")
    return status, body


@pytest.mark.asyncio
async def test_handle_healthz_returns_200() -> None:
    messages, send = _make_send()
    await handle_healthz(send)
    status, body = _parse_response(messages)
    assert status == 200
    assert b"ok" in body


@pytest.mark.asyncio
async def test_handle_readyz_returns_503_when_not_ready() -> None:
    providers = {"test": _make_provider_context()}
    messages, send = _make_send()
    await handle_readyz(send, providers)
    status, body = _parse_response(messages)
    assert status == 503
    assert b"not ready" in body


@pytest.mark.asyncio
async def test_handle_readyz_returns_200_when_all_ready() -> None:
    ctx = _make_provider_context()
    ctx.reconcile._first_poll_ok = True
    providers = {"test": ctx}
    messages, send = _make_send()
    await handle_readyz(send, providers)
    status, body = _parse_response(messages)
    assert status == 200
    assert b"ready" in body


@pytest.mark.asyncio
async def test_handle_readyz_empty_providers_returns_503() -> None:
    messages, send = _make_send()
    await handle_readyz(send, {})
    status, _ = _parse_response(messages)
    assert status == 503


@pytest.mark.asyncio
async def test_handle_route_list_returns_entries() -> None:
    mgr = RouteTableManager(default_providers=("umans", "ollama"))
    mgr.add_entry("key1", ["umans", "ollama"])
    messages, send = _make_send()
    await handle_route_list(send, mgr)
    status, body = _parse_response(messages)
    assert status == 200
    data = json.loads(body)
    assert "entries" in data
    assert len(data["entries"]) == 1
    assert data["entries"][0]["key"] == "key1"
    assert data["entries"][0]["providers"] == ["umans", "ollama"]
    assert data["default"] == ["umans", "ollama"]


@pytest.mark.asyncio
async def test_handle_route_add_requires_auth() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(
        method="POST",
        headers=[(b"content-type", b"application/json")],
    )
    body = json.dumps({"key": "sk-test", "providers": ["umans"]}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_route_add(send, receive, mgr, "admin-secret", scope)
    status, _ = _parse_response(messages)
    assert status == 403


@pytest.mark.asyncio
async def test_handle_route_add_no_admin_token_returns_405() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(method="POST")
    receive = _make_receive(b"{}")
    messages, send = _make_send()
    await handle_route_add(send, receive, mgr, None, scope)
    status, _ = _parse_response(messages)
    assert status == 405


@pytest.mark.asyncio
async def test_handle_route_add_hashes_key_and_persists() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(
        method="POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    body = json.dumps({"key": "sk-test-key", "providers": ["umans", "ollama"]}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_route_add(send, receive, mgr, "admin-secret", scope)
    status, resp_body = _parse_response(messages)
    assert status == 200
    data = json.loads(resp_body)
    assert data["key"] != "sk-test-key"
    assert len(data["key"]) == 64
    assert data["providers"] == ["umans", "ollama"]
    entries = mgr.list_entries()
    assert len(entries) == 1
    assert entries[0].key == data["key"]


@pytest.mark.asyncio
async def test_handle_route_add_invalid_json_returns_400() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(
        method="POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    receive = _make_receive(b"not json")
    messages, send = _make_send()
    await handle_route_add(send, receive, mgr, "admin-secret", scope)
    status, _ = _parse_response(messages)
    assert status == 400


@pytest.mark.asyncio
async def test_handle_route_add_missing_key_returns_400() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(
        method="POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    body = json.dumps({"providers": ["umans"]}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_route_add(send, receive, mgr, "admin-secret", scope)
    status, _ = _parse_response(messages)
    assert status == 400


@pytest.mark.asyncio
async def test_handle_route_add_missing_providers_returns_400() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(
        method="POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    body = json.dumps({"key": "sk-test"}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_route_add(send, receive, mgr, "admin-secret", scope)
    status, _ = _parse_response(messages)
    assert status == 400


@pytest.mark.asyncio
async def test_handle_route_add_wrong_content_type_returns_415() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(
        method="POST",
        headers=[
            (b"content-type", b"text/plain"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    receive = _make_receive(b"{}")
    messages, send = _make_send()
    await handle_route_add(send, receive, mgr, "admin-secret", scope)
    status, _ = _parse_response(messages)
    assert status == 415


@pytest.mark.asyncio
async def test_handle_route_delete_requires_auth() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    mgr.add_entry("key1", ["umans"])
    scope = _make_scope(method="DELETE")
    messages, send = _make_send()
    await handle_route_delete(send, mgr, "admin-secret", scope, "key1")
    status, _ = _parse_response(messages)
    assert status == 403


@pytest.mark.asyncio
async def test_handle_route_delete_no_admin_token_returns_405() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(method="DELETE")
    messages, send = _make_send()
    await handle_route_delete(send, mgr, None, scope, "key1")
    status, _ = _parse_response(messages)
    assert status == 405


@pytest.mark.asyncio
async def test_handle_route_delete_removes_entry() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    mgr.add_entry("key1", ["umans"])
    scope = _make_scope(
        method="DELETE",
        headers=[
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    messages, send = _make_send()
    await handle_route_delete(send, mgr, "admin-secret", scope, "key1")
    status, _ = _parse_response(messages)
    assert status == 200
    assert len(mgr.list_entries()) == 0


@pytest.mark.asyncio
async def test_handle_route_delete_unknown_key_returns_404() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(
        method="DELETE",
        headers=[
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    messages, send = _make_send()
    await handle_route_delete(send, mgr, "admin-secret", scope, "nonexistent")
    status, _ = _parse_response(messages)
    assert status == 404


@pytest.mark.asyncio
async def test_handle_config_get_returns_routing_config() -> None:
    config = RoutingConfig(failover_threshold_seconds=15, failover_margin=8)
    messages, send = _make_send()
    await handle_config_get(send, config)
    status, body = _parse_response(messages)
    assert status == 200
    data = json.loads(body)
    assert data["failover_threshold_seconds"] == 15
    assert data["failover_margin"] == 8


@pytest.mark.asyncio
async def test_handle_config_get_returns_defaults() -> None:
    config = RoutingConfig()
    messages, send = _make_send()
    await handle_config_get(send, config)
    status, body = _parse_response(messages)
    assert status == 200
    data = json.loads(body)
    assert data["failover_threshold_seconds"] == 10
    assert data["failover_margin"] == 5


def test_build_status_payload_structure() -> None:
    ctx = _make_provider_context()
    providers = {"test": ctx}
    mgr = RouteTableManager(default_providers=("test",))
    mgr.add_entry("key1", ["test"])
    metrics = RoutingMetrics()
    metrics.record_forwarded("test")
    metrics.record_decision("key1", "test", "test")
    payload = _build_status_payload(providers, mgr, metrics, build_sha="abc123")
    assert "providers" in payload
    assert "route_table" in payload
    assert "routing_metrics" in payload
    assert "version" in payload
    assert payload["build"] == "abc123"
    assert "test" in payload["providers"]
    assert payload["providers"]["test"]["gate_closed_reason"] == "saturated"
    assert payload["providers"]["test"]["upstream_url"] == "https://upstream.example.com"
    assert payload["route_table"]["key1"] == ["test"]
    assert payload["route_table"]["default"] == ["test"]
    assert payload["routing_metrics"]["failovers"] == 0
    assert payload["routing_metrics"]["routing_decisions"] == 1
    assert payload["routing_metrics"]["forwarded_per_provider"]["test"] == 1
    assert "recent_decisions" in payload["routing_metrics"]
    assert "evicted_decisions" in payload["routing_metrics"]


def test_build_status_payload_with_failover() -> None:
    ctx = _make_provider_context()
    providers = {"test": ctx}
    mgr = RouteTableManager(default_providers=("test",))
    metrics = RoutingMetrics()
    metrics.record_decision("key1", "fallback", "primary")
    payload = _build_status_payload(providers, mgr, metrics)
    assert payload["routing_metrics"]["failovers"] == 1


def test_build_status_payload_none_build_sha() -> None:
    ctx = _make_provider_context()
    providers = {"test": ctx}
    mgr = RouteTableManager(default_providers=("test",))
    metrics = RoutingMetrics()
    payload = _build_status_payload(providers, mgr, metrics)
    assert payload["build"] is None


# --- Provider override endpoint tests (Plan 012 WI-3) ---

_ADMIN_TOKEN = "test-admin-token"


def _make_override_scope(
    method: str = "POST",
    body: bytes = b"",
) -> dict[str, Any]:
    import hmac

    sig = hmac.new(
        _ADMIN_TOKEN.encode(), body, __import__("hashlib").sha256
    ).hexdigest()
    return {
        "type": "http",
        "method": method,
        "path": "/admin/providers/test/override",
        "raw_path": b"/admin/providers/test/override",
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {_ADMIN_TOKEN}".encode()),
            (b"content-type", b"application/json"),
            (b"x-csrf-token", sig.encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }


def _make_ready_provider(name: str = "test") -> ProviderContext:
    from sluice.usage import CachedReading

    ctx = _make_provider_context(name)
    ctx.reconcile._first_poll_ok = True
    ctx.reconcile._last_reading_cached = CachedReading(
        reading=__import__(
            "sluice.control", fromlist=["LimitState"]
        ).LimitState(provider="generic", age_seconds=0.0, limit=4, hard_cap=8),
        fetched_at_monotonic=0.0,
        ok=True,
    )
    return ctx


@pytest.mark.asyncio
async def test_override_apply_success() -> None:
    ctx = _make_ready_provider("test")
    providers = {"test": ctx}
    body = json.dumps({"target": 3}).encode()
    scope = _make_override_scope("POST", body)
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_provider_override(
        send, receive, providers, _ADMIN_TOKEN, scope,
        "test", "POST", None,
    )
    status, _ = _parse_response(messages)
    assert status == 200


@pytest.mark.asyncio
async def test_override_apply_rejects_zero() -> None:
    ctx = _make_ready_provider("test")
    providers = {"test": ctx}
    body = json.dumps({"target": 0}).encode()
    scope = _make_override_scope("POST", body)
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_provider_override(
        send, receive, providers, _ADMIN_TOKEN, scope,
        "test", "POST", None,
    )
    status, _ = _parse_response(messages)
    assert status == 400


@pytest.mark.asyncio
async def test_override_unknown_provider_404() -> None:
    ctx = _make_ready_provider("test")
    providers = {"test": ctx}
    body = json.dumps({"target": 3}).encode()
    scope = _make_override_scope("POST", body)
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_provider_override(
        send, receive, providers, _ADMIN_TOKEN, scope,
        "nonexistent", "POST", None,
    )
    status, _ = _parse_response(messages)
    assert status == 404


@pytest.mark.asyncio
async def test_override_no_admin_token_405() -> None:
    ctx = _make_ready_provider("test")
    providers = {"test": ctx}
    body = json.dumps({"target": 3}).encode()
    scope = _make_override_scope("POST", body)
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_provider_override(
        send, receive, providers, None, scope,
        "test", "POST", None,
    )
    status, _ = _parse_response(messages)
    assert status == 405


@pytest.mark.asyncio
async def test_override_delete_reverts() -> None:
    ctx = _make_ready_provider("test")
    providers = {"test": ctx}
    scope = _make_override_scope("DELETE")
    receive = _make_receive(b"")
    messages, send = _make_send()
    await handle_provider_override(
        send, receive, providers, _ADMIN_TOKEN, scope,
        "test", "DELETE", None,
    )
    status, _ = _parse_response(messages)
    assert status == 200
