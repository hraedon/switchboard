from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest

from switchboard.admin import (
    _build_status_payload,
    handle_config_get,
    handle_healthz,
    handle_model_map_delete,
    handle_model_map_list,
    handle_model_map_set,
    handle_provider_override,
    handle_readyz,
    handle_route_add,
    handle_route_delete,
    handle_route_list,
    handle_routing_config_update,
)
from switchboard.control import RoutingConfig, RoutingStrategy
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.model_map import ModelMapManager
from switchboard.providers import ProviderContext
from switchboard.proxy import RoutingMetrics
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource


def _make_provider_context(
    name: str = "test", provider_type: str = "generic"
) -> ProviderContext:
    gate = PermitGate(initial_capacity=0)
    truth = NullTruthSource(provider=provider_type)
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        max_concurrency=1,
        provider_type=provider_type,
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
async def test_handle_route_add_with_secret_stores_hmac_hash() -> None:
    """When a route_key_secret is provided, the stored digest is the
    HMAC-SHA-256 of the key (not the plain digest), so the proxy's dual-read
    lookup matches it back and a leaked store resists rainbow-table matching."""
    from switchboard.control import hash_route_key

    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(
        method="POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    body = json.dumps({"key": "sk-test-key", "providers": ["umans"]}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_route_add(
        send, receive, mgr, "admin-secret", scope,
        route_key_secret="route-hmac-secret",
    )
    status, resp_body = _parse_response(messages)
    assert status == 200
    data = json.loads(resp_body)
    expected = hash_route_key("sk-test-key", "route-hmac-secret")
    assert data["key"] == expected
    assert data["key"] != hash_route_key("sk-test-key")  # not the plain digest
    assert mgr.lookup(expected) == ("umans",)


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


# --- Model-map endpoint tests (WI-017/012) ---


def _authed_scope(method: str = "POST") -> dict[str, Any]:
    return _make_scope(
        method=method,
        headers=[
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )


@pytest.mark.asyncio
async def test_handle_model_map_list_returns_models() -> None:
    mgr = ModelMapManager()
    mgr.set_model(
        "kimi", {"umans": "umans-kimi", "ollama-cloud": "kimi-ollama"}
    )
    providers = {"umans": _make_provider_context("umans"),
                 "ollama-cloud": _make_provider_context("ollama-cloud")}
    messages, send = _make_send()
    await handle_model_map_list(send, mgr, None, providers)
    status, body = _parse_response(messages)
    assert status == 200
    data = json.loads(body)
    assert len(data["models"]) == 1
    entry = data["models"][0]
    assert entry["model"] == "kimi"
    assert entry["aliases"] == {"umans": "umans-kimi", "ollama-cloud": "kimi-ollama"}
    assert entry["servable_providers"] == ["ollama-cloud", "umans"]
    assert data["configured_providers"] == ["ollama-cloud", "umans"]


@pytest.mark.asyncio
async def test_handle_model_map_set_persists_and_responds() -> None:
    mgr = ModelMapManager()
    providers = {"umans": _make_provider_context("umans")}
    scope = _authed_scope()
    body = json.dumps(
        {"model": "kimi", "aliases": {"umans": "umans-kimi"}}
    ).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_model_map_set(
        send, receive, mgr, "admin-secret", scope, None, providers,
        max_request_body_bytes=1048576,
    )
    status, resp_body = _parse_response(messages)
    assert status == 200
    data = json.loads(resp_body)
    assert data["model"] == "kimi"
    assert data["aliases"] == {"umans": "umans-kimi"}
    assert mgr.get_model_map().alias_for("kimi", "umans") == "umans-kimi"


@pytest.mark.asyncio
async def test_handle_model_map_set_rejects_empty_alias() -> None:
    """An empty alias string would rewrite the upstream model to ''."""
    mgr = ModelMapManager()
    providers = {"umans": _make_provider_context("umans")}
    scope = _authed_scope()
    body = json.dumps(
        {"model": "kimi", "aliases": {"umans": ""}}
    ).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_model_map_set(
        send, receive, mgr, "admin-secret", scope, None, providers,
        max_request_body_bytes=1048576,
    )
    status, resp_body = _parse_response(messages)
    assert status == 400
    assert b"non-empty" in resp_body


@pytest.mark.asyncio
async def test_handle_model_map_set_requires_auth() -> None:
    mgr = ModelMapManager()
    scope = _make_scope(
        method="POST",
        headers=[(b"content-type", b"application/json")],
    )
    receive = _make_receive(b"{}")
    messages, send = _make_send()
    await handle_model_map_set(
        send, receive, mgr, "admin-secret", scope
    )
    status, _ = _parse_response(messages)
    assert status == 403


@pytest.mark.asyncio
async def test_handle_model_map_set_no_admin_token_returns_405() -> None:
    mgr = ModelMapManager()
    scope = _make_scope(method="POST")
    receive = _make_receive(b"{}")
    messages, send = _make_send()
    await handle_model_map_set(send, receive, mgr, None, scope)
    status, _ = _parse_response(messages)
    assert status == 405


@pytest.mark.asyncio
async def test_handle_model_map_set_rejects_unknown_provider() -> None:
    mgr = ModelMapManager()
    providers = {"umans": _make_provider_context("umans")}
    scope = _authed_scope()
    body = json.dumps(
        {"model": "kimi", "aliases": {"umans": "ok", "typo-prov": "bad"}}
    ).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_model_map_set(
        send, receive, mgr, "admin-secret", scope, None, providers,
        max_request_body_bytes=1048576,
    )
    status, resp_body = _parse_response(messages)
    assert status == 400
    data = json.loads(resp_body)
    assert "typo-prov" in data["error"]
    # Nothing was written.
    assert "kimi" not in mgr.get_model_map()


@pytest.mark.asyncio
async def test_handle_model_map_set_invalid_json_returns_400() -> None:
    mgr = ModelMapManager()
    scope = _authed_scope()
    receive = _make_receive(b"not json")
    messages, send = _make_send()
    await handle_model_map_set(
        send, receive, mgr, "admin-secret", scope,
        max_request_body_bytes=1048576,
    )
    status, _ = _parse_response(messages)
    assert status == 400


@pytest.mark.asyncio
async def test_handle_model_map_set_missing_model_returns_400() -> None:
    mgr = ModelMapManager()
    scope = _authed_scope()
    body = json.dumps({"aliases": {"umans": "u"}}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_model_map_set(
        send, receive, mgr, "admin-secret", scope,
        max_request_body_bytes=1048576,
    )
    status, _ = _parse_response(messages)
    assert status == 400


@pytest.mark.asyncio
async def test_handle_model_map_set_missing_aliases_returns_400() -> None:
    mgr = ModelMapManager()
    scope = _authed_scope()
    body = json.dumps({"model": "kimi"}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_model_map_set(
        send, receive, mgr, "admin-secret", scope,
        max_request_body_bytes=1048576,
    )
    status, _ = _parse_response(messages)
    assert status == 400


@pytest.mark.asyncio
async def test_handle_model_map_set_wrong_content_type_returns_415() -> None:
    mgr = ModelMapManager()
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
    await handle_model_map_set(
        send, receive, mgr, "admin-secret", scope,
        max_request_body_bytes=1048576,
    )
    status, _ = _parse_response(messages)
    assert status == 415


@pytest.mark.asyncio
async def test_handle_model_map_set_store_failure_returns_500_json() -> None:
    """A store write failure must surface as a JSON 500, never as success
    (WI-12b: the old memory-first order made the handler report a save a
    restart would revert)."""
    db = sqlite3.connect(":memory:")
    mgr = ModelMapManager(db=db)
    db.close()  # subsequent writes raise sqlite3.ProgrammingError
    providers = {"umans": _make_provider_context("umans")}
    scope = _authed_scope()
    body = json.dumps(
        {"model": "kimi", "aliases": {"umans": "umans-kimi"}}
    ).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_model_map_set(
        send, receive, mgr, "admin-secret", scope, None, providers,
        max_request_body_bytes=1048576,
    )
    status, resp_body = _parse_response(messages)
    assert status == 500
    data = json.loads(resp_body)
    assert "error" in data
    # Memory was not mutated: the handler's failure report is honest.
    assert "kimi" not in mgr.get_model_map()


@pytest.mark.asyncio
async def test_handle_model_map_set_no_body_limit_returns_409() -> None:
    """Without max_request_body_bytes, a model map would force unbounded
    buffering — reject the addition at the API."""
    mgr = ModelMapManager()
    providers = {"umans": _make_provider_context("umans")}
    scope = _authed_scope()
    body = json.dumps(
        {"model": "kimi", "aliases": {"umans": "umans-kimi"}}
    ).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_model_map_set(
        send, receive, mgr, "admin-secret", scope, None, providers,
        max_request_body_bytes=None,
    )
    status, _ = _parse_response(messages)
    assert status == 409
    assert "kimi" not in mgr.get_model_map()


@pytest.mark.asyncio
async def test_handle_model_map_delete_store_failure_returns_500_json() -> None:
    db = sqlite3.connect(":memory:")
    mgr = ModelMapManager(db=db)
    mgr.set_model("kimi", {"umans": "umans-kimi"})
    db.close()
    scope = _authed_scope(method="DELETE")
    messages, send = _make_send()
    await handle_model_map_delete(
        send, mgr, "admin-secret", scope, "kimi"
    )
    status, resp_body = _parse_response(messages)
    assert status == 500
    data = json.loads(resp_body)
    assert "error" in data
    # The entry survives — memory still agrees with the (unreachable) store.
    assert mgr.get_model_map().alias_for("kimi", "umans") == "umans-kimi"


@pytest.mark.asyncio
async def test_handle_model_map_delete_removes_entry() -> None:
    mgr = ModelMapManager()
    mgr.set_model("kimi", {"umans": "u"})
    scope = _make_scope(
        method="DELETE",
        headers=[
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    messages, send = _make_send()
    await handle_model_map_delete(
        send, mgr, "admin-secret", scope, "kimi"
    )
    status, _ = _parse_response(messages)
    assert status == 200
    assert "kimi" not in mgr.get_model_map()


@pytest.mark.asyncio
async def test_handle_model_map_delete_requires_auth() -> None:
    mgr = ModelMapManager()
    mgr.set_model("kimi", {"umans": "u"})
    scope = _make_scope(method="DELETE")
    messages, send = _make_send()
    await handle_model_map_delete(
        send, mgr, "admin-secret", scope, "kimi"
    )
    status, _ = _parse_response(messages)
    assert status == 403


@pytest.mark.asyncio
async def test_handle_model_map_delete_no_admin_token_returns_405() -> None:
    mgr = ModelMapManager()
    scope = _make_scope(method="DELETE")
    messages, send = _make_send()
    await handle_model_map_delete(send, mgr, None, scope, "kimi")
    status, _ = _parse_response(messages)
    assert status == 405


@pytest.mark.asyncio
async def test_handle_model_map_delete_unknown_model_returns_404() -> None:
    mgr = ModelMapManager()
    scope = _make_scope(
        method="DELETE",
        headers=[
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    messages, send = _make_send()
    await handle_model_map_delete(
        send, mgr, "admin-secret", scope, "nonexistent"
    )
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
    assert payload["routing_metrics"]["affinity_pins_total"] == 0
    assert payload["routing_metrics"]["affinity_failbacks_total"] == 0


def test_build_status_payload_with_failover() -> None:
    ctx = _make_provider_context()
    providers = {"test": ctx}
    mgr = RouteTableManager(default_providers=("test",))
    metrics = RoutingMetrics()
    metrics.record_decision("key1", "fallback", "primary")
    payload = _build_status_payload(providers, mgr, metrics)
    assert payload["routing_metrics"]["failovers"] == 1


def test_build_status_payload_includes_model_map() -> None:
    ctx = _make_provider_context()
    providers = {"test": ctx}
    mgr = RouteTableManager(default_providers=("test",))
    mmgr = ModelMapManager()
    mmgr.set_model("kimi", {"test": "test-kimi"})
    metrics = RoutingMetrics()
    payload = _build_status_payload(
        providers, mgr, metrics, model_map_mgr=mmgr
    )
    assert payload["model_map"] == {"kimi": {"test": "test-kimi"}}


def test_build_status_payload_omits_model_map_when_none() -> None:
    ctx = _make_provider_context()
    providers = {"test": ctx}
    mgr = RouteTableManager(default_providers=("test",))
    metrics = RoutingMetrics()
    payload = _build_status_payload(providers, mgr, metrics)
    assert "model_map" not in payload


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


def _make_ready_provider(
    name: str = "test", provider_type: str = "generic"
) -> ProviderContext:
    from switchboard.limit import CachedReading, LimitState

    ctx = _make_provider_context(name, provider_type=provider_type)
    ctx.reconcile._first_poll_ok = True
    ctx.reconcile._last_reading_cached = CachedReading(
        reading=LimitState(
            provider=provider_type, age_seconds=0.0, limit=4, hard_cap=8
        ),
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
async def test_override_generic_accepts_above_placeholder_caps() -> None:
    """Non-umans readings carry placeholder limit/hard_cap defaults the
    runtime never enforces — the override endpoint must not bound against
    them (drop-sluice review, blocking finding 1)."""
    ctx = _make_ready_provider("test", provider_type="generic")
    providers = {"test": ctx}
    body = json.dumps({"target": 12}).encode()
    scope = _make_override_scope("POST", body)
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_provider_override(
        send, receive, providers, _ADMIN_TOKEN, scope,
        "test", "POST", None,
    )
    status, resp_body = _parse_response(messages)
    assert status == 200
    data = json.loads(resp_body)
    assert data["applied"] is True
    assert "warning" not in data
    assert ctx.reconcile.max_concurrency == 12


@pytest.mark.asyncio
async def test_override_umans_rejects_above_hard_cap() -> None:
    """On the polled umans path limit/hard_cap are real provider limits,
    so the Plan 011 §4 bound applies: above hard_cap is a 400."""
    ctx = _make_ready_provider("test", provider_type="umans")
    providers = {"test": ctx}
    body = json.dumps({"target": 10}).encode()
    scope = _make_override_scope("POST", body)
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_provider_override(
        send, receive, providers, _ADMIN_TOKEN, scope,
        "test", "POST", None,
    )
    status, resp_body = _parse_response(messages)
    assert status == 400
    assert b"hard_cap" in resp_body


@pytest.mark.asyncio
async def test_override_umans_warns_between_limit_and_hard_cap() -> None:
    """limit < target <= hard_cap is accept-with-warning on umans:
    requests above the limit run at low priority."""
    ctx = _make_ready_provider("test", provider_type="umans")
    providers = {"test": ctx}
    body = json.dumps({"target": 6}).encode()
    scope = _make_override_scope("POST", body)
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_provider_override(
        send, receive, providers, _ADMIN_TOKEN, scope,
        "test", "POST", None,
    )
    status, resp_body = _parse_response(messages)
    assert status == 200
    data = json.loads(resp_body)
    assert data["applied"] is True
    assert "above limit" in data["warning"]
    assert ctx.reconcile.max_concurrency == 6


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


@pytest.mark.asyncio
async def test_override_umans_at_hard_cap_boundary_accepted() -> None:
    """target == hard_cap is the boundary: accepted (with the above-limit
    warning), only strictly-above is rejected."""
    ctx = _make_ready_provider("test", provider_type="umans")
    providers = {"test": ctx}
    body = json.dumps({"target": 8}).encode()
    scope = _make_override_scope("POST", body)
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_provider_override(
        send, receive, providers, _ADMIN_TOKEN, scope,
        "test", "POST", None,
    )
    status, resp_body = _parse_response(messages)
    assert status == 200
    data = json.loads(resp_body)
    assert data["applied"] is True
    assert "above limit" in data["warning"]
    assert ctx.reconcile.max_concurrency == 8


# --- Routing config runtime swap (Plan 020 WI-14) ---


class _FakeProxyApp:
    """Minimal proxy stand-in for routing config swap tests."""

    def __init__(self, config: RoutingConfig) -> None:
        self._routing_config = config

    @property
    def routing_config(self) -> RoutingConfig:
        return self._routing_config

    def update_routing_config(self, config: RoutingConfig) -> None:
        self._routing_config = config


def _routing_authed_scope() -> dict[str, Any]:
    return _make_scope(
        method="PUT",
        headers=[
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )


@pytest.mark.asyncio
async def test_routing_config_update_changes_strategy() -> None:
    app = _FakeProxyApp(RoutingConfig())
    scope = _routing_authed_scope()
    body = json.dumps({"strategy": "pace"}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_routing_config_update(
        send, receive, app, "admin-secret", scope,
    )
    status, resp_body = _parse_response(messages)
    assert status == 200
    data = json.loads(resp_body)
    assert data["strategy"] == "pace"
    assert app.routing_config.strategy == RoutingStrategy.PACE


@pytest.mark.asyncio
async def test_routing_config_update_preserves_unchanged_fields() -> None:
    original = RoutingConfig(
        strategy=RoutingStrategy.ORDERED,
        dwell_interval=60.0,
        failback_delay=30.0,
        pace_burn_rate_per_day=0.20,
    )
    app = _FakeProxyApp(original)
    scope = _routing_authed_scope()
    body = json.dumps({"strategy": "pace"}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_routing_config_update(
        send, receive, app, "admin-secret", scope,
    )
    status, _ = _parse_response(messages)
    assert status == 200
    # Unchanged fields preserved
    assert app.routing_config.dwell_interval == 60.0
    assert app.routing_config.failback_delay == 30.0
    assert app.routing_config.pace_burn_rate_per_day == 0.20
    # Changed field applied
    assert app.routing_config.strategy == RoutingStrategy.PACE


@pytest.mark.asyncio
async def test_routing_config_update_pace_knobs() -> None:
    app = _FakeProxyApp(RoutingConfig())
    scope = _routing_authed_scope()
    body = json.dumps({
        "pace_burn_rate_per_day": 0.30,
        "pace_flap_margin": 0.10,
    }).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_routing_config_update(
        send, receive, app, "admin-secret", scope,
    )
    status, _ = _parse_response(messages)
    assert status == 200
    assert app.routing_config.pace_burn_rate_per_day == 0.30
    assert app.routing_config.pace_flap_margin == 0.10


@pytest.mark.asyncio
async def test_routing_config_update_invalid_strategy() -> None:
    app = _FakeProxyApp(RoutingConfig())
    scope = _routing_authed_scope()
    body = json.dumps({"strategy": "invalid"}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_routing_config_update(
        send, receive, app, "admin-secret", scope,
    )
    status, resp_body = _parse_response(messages)
    assert status == 400
    assert b"strategy" in resp_body


@pytest.mark.asyncio
async def test_routing_config_update_rejects_immutable_field() -> None:
    app = _FakeProxyApp(RoutingConfig())
    scope = _routing_authed_scope()
    body = json.dumps({"pin_conversations": True}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_routing_config_update(
        send, receive, app, "admin-secret", scope,
    )
    status, resp_body = _parse_response(messages)
    assert status == 400
    assert b"pin_conversations" in resp_body


@pytest.mark.asyncio
async def test_routing_config_update_requires_auth() -> None:
    app = _FakeProxyApp(RoutingConfig())
    scope = _routing_authed_scope()
    body = json.dumps({"strategy": "pace"}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_routing_config_update(
        send, receive, app, "wrong-token", scope,
    )
    status, _ = _parse_response(messages)
    assert status == 403


@pytest.mark.asyncio
async def test_routing_config_update_no_admin_token_405() -> None:
    app = _FakeProxyApp(RoutingConfig())
    scope = _routing_authed_scope()
    body = json.dumps({"strategy": "pace"}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_routing_config_update(
        send, receive, app, None, scope,
    )
    status, _ = _parse_response(messages)
    assert status == 405


@pytest.mark.asyncio
async def test_routing_config_update_invalid_json() -> None:
    app = _FakeProxyApp(RoutingConfig())
    scope = _routing_authed_scope()
    receive = _make_receive(b"not json")
    messages, send = _make_send()
    await handle_routing_config_update(
        send, receive, app, "admin-secret", scope,
    )
    status, _ = _parse_response(messages)
    assert status == 400


@pytest.mark.asyncio
async def test_routing_config_update_wrong_content_type() -> None:
    app = _FakeProxyApp(RoutingConfig())
    scope = _make_scope(
        method="PUT",
        headers=[
            (b"content-type", b"text/plain"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    receive = _make_receive(b"{}")
    messages, send = _make_send()
    await handle_routing_config_update(
        send, receive, app, "admin-secret", scope,
    )
    status, _ = _parse_response(messages)
    assert status == 400


@pytest.mark.asyncio
async def test_routing_config_update_out_of_bounds() -> None:
    app = _FakeProxyApp(RoutingConfig())
    scope = _routing_authed_scope()
    body = json.dumps({"pace_burn_rate_per_day": 1.5}).encode()
    receive = _make_receive(body)
    messages, send = _make_send()
    await handle_routing_config_update(
        send, receive, app, "admin-secret", scope,
    )
    status, _ = _parse_response(messages)
    assert status == 400
