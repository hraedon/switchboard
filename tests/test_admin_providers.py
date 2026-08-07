"""Admin provider CRUD + test endpoint + effective config (Plan 020 WI-3/4).

The load-bearing behaviors: auth/CSRF identical to the route handlers,
dry-build-first so a bad section never poisons the store, PUT rollback on
build failure, D1 tombstones for TOML-declared providers, and — above all —
no serialization surface ever carries a credential (sentinel greps).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from switchboard.admin import (
    handle_config_effective,
    handle_provider_create,
    handle_provider_delete,
    handle_provider_test,
    handle_provider_update,
    handle_providers_list,
)
from switchboard.config_store import ConfigStoreManager
from switchboard.control import RoutingConfig
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.provider_manager import ProviderManager
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.session import SESSION_COOKIE, mint_session
from switchboard.truth import NullTruthSource

ADMIN_TOKEN = "admin-secret"

SENTINEL_STORE = "sk-store-SENTINEL-do-not-leak-1111"
SENTINEL_TOML = "sk-toml-SENTINEL-do-not-leak-2222"


def _make_ctx(
    name: str = "alpha",
    upstream: str | None = None,
    api_key: str = "",
) -> ProviderContext:
    gate = PermitGate(initial_capacity=1)
    truth = NullTruthSource(provider="generic")
    reconcile = ReconciliationLoop(
        truth_source=truth,
        gate=gate,
        max_concurrency=1,
        provider_type="generic",
        breaker_config=BreakerConfig(),
    )
    return ProviderContext(
        name=name,
        upstream_url=upstream or f"https://{name}.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=httpx.AsyncClient(),
        api_key=api_key,
    )


def _make_scope(
    method: str = "GET",
    path: str = "/admin/providers",
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


def _admin_headers() -> list[tuple[bytes, bytes]]:
    return [
        (b"content-type", b"application/json"),
        (b"authorization", f"Bearer {ADMIN_TOKEN}".encode()),
        (b"sec-fetch-site", b"same-origin"),
    ]


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


def _env_body(name: str | None = "beta", **overrides: Any) -> bytes:
    data: dict[str, Any] = {
        "upstream": "https://beta.example.com",
        "provider_type": "generic",
        "target": 2,
        "key_mode": "env",
        "api_key_env": "SB_TEST_BETA_KEY",
    }
    if name is not None:
        data["name"] = name
    data.update(overrides)
    return json.dumps(data).encode()


def _stored_fields(**overrides: Any) -> dict[str, object]:
    fields: dict[str, object] = {
        "upstream": "https://stored.example.com",
        "provider_type": "generic",
        "target": 1,
        "key_mode": "stored",
        "api_key_stored": SENTINEL_STORE,
    }
    fields.update(overrides)
    return fields


def _passthrough_fields(**overrides: Any) -> dict[str, object]:
    fields: dict[str, object] = {
        "upstream": "https://pass.example.com",
        "provider_type": "generic",
        "target": 1,
        "key_mode": "passthrough",
    }
    fields.update(overrides)
    return fields


async def _invoke(
    endpoint: str,
    *,
    admin_token: str | None,
    scope: dict[str, Any],
    mgr: ProviderManager,
    store: ConfigStoreManager,
    body: bytes = b"{}",
) -> tuple[int, bytes]:
    receive = _make_receive(body)
    messages, send = _make_send()
    if endpoint == "create":
        await handle_provider_create(
            send, receive, mgr, store, admin_token, scope,
        )
    elif endpoint == "update":
        await handle_provider_update(
            send, receive, mgr, store, admin_token, scope, "alpha",
        )
    elif endpoint == "delete":
        await handle_provider_delete(
            send, mgr, store, admin_token, scope, "alpha",
            frozenset(), {},
        )
    elif endpoint == "test":
        await handle_provider_test(
            send, mgr.providers, admin_token, scope, "alpha",
        )
    else:  # pragma: no cover - guard against typos in parametrize
        raise AssertionError(endpoint)
    return _parse_response(messages)


# ------------------------------------------------------------ auth matrix


_MUTATING = ("create", "update", "delete", "test")


@pytest.mark.parametrize("endpoint", _MUTATING)
@pytest.mark.asyncio
async def test_mutating_endpoint_405_without_admin_token(
    endpoint: str,
) -> None:
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    scope = _make_scope("POST", headers=_admin_headers())
    status, _ = await _invoke(
        endpoint, admin_token=None, scope=scope, mgr=mgr, store=store,
    )
    assert status == 405


@pytest.mark.parametrize("endpoint", _MUTATING)
@pytest.mark.asyncio
async def test_mutating_endpoint_403_with_bad_token(endpoint: str) -> None:
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    scope = _make_scope(
        "POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer wrong-token"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    status, body = await _invoke(
        endpoint, admin_token=ADMIN_TOKEN, scope=scope, mgr=mgr, store=store,
    )
    assert status == 403
    assert b"unauthorized" in body


@pytest.mark.parametrize("endpoint", _MUTATING)
@pytest.mark.asyncio
async def test_mutating_endpoint_403_cross_site_cookie(
    endpoint: str,
) -> None:
    """Cookie-authenticated cross-site requests must be CSRF-blocked."""
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    cookie = mint_session(ADMIN_TOKEN, time.time(), 3600)
    scope = _make_scope(
        "POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"cookie", f"{SESSION_COOKIE}={cookie}".encode()),
            (b"sec-fetch-site", b"cross-site"),
        ],
    )
    status, body = await _invoke(
        endpoint, admin_token=ADMIN_TOKEN, scope=scope, mgr=mgr, store=store,
    )
    assert status == 403
    assert b"cross-site" in body


# ------------------------------------------------------------------ create


@pytest.mark.asyncio
async def test_create_happy_path_masked_and_live(monkeypatch) -> None:
    monkeypatch.setenv("SB_TEST_BETA_KEY", "sk-beta-secret-9999")
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    scope = _make_scope("POST", headers=_admin_headers())
    status, body = await _invoke(
        "create", admin_token=ADMIN_TOKEN, scope=scope,
        mgr=mgr, store=store, body=_env_body(),
    )
    try:
        assert status == 200
        data = json.loads(body)
        assert data["name"] == "beta"
        assert data["key_mode"] == "env"
        assert data["enabled"] is True
        assert "api_key_stored" not in data
        assert "sk-beta-secret" not in body.decode()

        ctx = mgr.providers["beta"]
        assert ctx.upstream_url == "https://beta.example.com"
        # The construction path resolved the env credential.
        assert ctx.api_key == "sk-beta-secret-9999"
        await ctx.reconcile.tick()
        assert ctx.reconcile.ready
        assert store.get("beta") is not None
    finally:
        await mgr.remove("beta")
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_create_stored_key_masked_in_response() -> None:
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    scope = _make_scope("POST", headers=_admin_headers())
    body_in = json.dumps({
        "name": "gamma",
        "upstream": "https://gamma.example.com",
        "provider_type": "generic",
        "target": 1,
        "key_mode": "stored",
        "api_key_stored": SENTINEL_STORE,
    }).encode()
    status, body = await _invoke(
        "create", admin_token=ADMIN_TOKEN, scope=scope,
        mgr=mgr, store=store, body=body_in,
    )
    try:
        assert status == 200
        assert SENTINEL_STORE not in body.decode()
        data = json.loads(body)
        assert data["api_key_set"] is True
        assert data["api_key_hint"] == SENTINEL_STORE[-4:]
        assert mgr.providers["gamma"].api_key == SENTINEL_STORE
    finally:
        await mgr.remove("gamma")
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_create_unset_api_key_env_400_and_store_untouched(
    monkeypatch,
) -> None:
    """Dry-build-first: a section the build refuses never reaches the store."""
    monkeypatch.delenv("SB_TEST_BETA_KEY", raising=False)
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    scope = _make_scope("POST", headers=_admin_headers())
    status, body = await _invoke(
        "create", admin_token=ADMIN_TOKEN, scope=scope,
        mgr=mgr, store=store, body=_env_body(),
    )
    assert status == 400
    assert b"api_key_env" in body
    assert store.get("beta") is None
    assert "beta" not in mgr.providers


@pytest.mark.asyncio
async def test_create_missing_name_400() -> None:
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    scope = _make_scope("POST", headers=_admin_headers())
    status, body = await _invoke(
        "create", admin_token=ADMIN_TOKEN, scope=scope,
        mgr=mgr, store=store, body=_env_body(name=None),
    )
    assert status == 400
    assert b"name" in body


@pytest.mark.asyncio
async def test_create_duplicate_live_name_409() -> None:
    ctx = _make_ctx("beta")
    mgr = ProviderManager({"beta": ctx})
    store = ConfigStoreManager()
    scope = _make_scope("POST", headers=_admin_headers())
    try:
        status, body = await _invoke(
            "create", admin_token=ADMIN_TOKEN, scope=scope,
            mgr=mgr, store=store, body=_env_body(),
        )
        assert status == 409
        assert b"already exists" in body
        assert store.get("beta") is None
    finally:
        await mgr.remove("beta")
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_create_duplicate_store_row_409_even_when_disabled() -> None:
    """A disabled tombstone still owns its name — create must not revive it."""
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    store.upsert("beta", _passthrough_fields(enabled=0))
    scope = _make_scope("POST", headers=_admin_headers())
    status, _ = await _invoke(
        "create", admin_token=ADMIN_TOKEN, scope=scope,
        mgr=mgr, store=store, body=_env_body(),
    )
    assert status == 409
    assert "beta" not in mgr.providers


# ------------------------------------------------------------------ update


@pytest.mark.asyncio
async def test_update_swaps_live_context() -> None:
    old_ctx = _make_ctx("alpha", upstream="https://old.example.com")
    mgr = ProviderManager({"alpha": old_ctx})
    store = ConfigStoreManager()
    scope = _make_scope("PUT", headers=_admin_headers())
    body_in = json.dumps({
        "upstream": "https://new.example.com",
        "provider_type": "generic",
        "target": 1,
        "key_mode": "passthrough",
    }).encode()
    try:
        status, body = await _invoke(
            "update", admin_token=ADMIN_TOKEN, scope=scope,
            mgr=mgr, store=store, body=body_in,
        )
        assert status == 200
        data = json.loads(body)
        assert data["upstream"] == "https://new.example.com"
        # The live map now serves the NEW context.
        assert mgr.providers["alpha"].upstream_url == "https://new.example.com"
        assert store.get("alpha") is not None
        # Let the old context's background drain settle.
        await asyncio.sleep(0)
    finally:
        await mgr.remove("alpha")
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_update_rollback_restores_previous_row(monkeypatch) -> None:
    """A failed dry-build must leave the store exactly as it was."""
    monkeypatch.setenv("SB_TEST_GOOD_KEY", "sk-good-key-0001")
    monkeypatch.delenv("SB_TEST_MISSING_KEY", raising=False)
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    store.upsert("alpha", {
        "upstream": "https://original.example.com",
        "provider_type": "generic",
        "target": 3,
        "key_mode": "env",
        "api_key_env": "SB_TEST_GOOD_KEY",
        "account": "acct-1",
    })
    scope = _make_scope("PUT", headers=_admin_headers())
    body_in = json.dumps({
        "upstream": "https://broken.example.com",
        "provider_type": "generic",
        "target": 1,
        "key_mode": "env",
        "api_key_env": "SB_TEST_MISSING_KEY",
    }).encode()
    status, body = await _invoke(
        "update", admin_token=ADMIN_TOKEN, scope=scope,
        mgr=mgr, store=store, body=body_in,
    )
    assert status == 400
    assert b"SB_TEST_MISSING_KEY" in body
    restored = store.get("alpha")
    assert restored is not None
    assert restored["upstream"] == "https://original.example.com"
    assert restored["api_key_env"] == "SB_TEST_GOOD_KEY"
    assert restored["target"] == 3
    assert restored["account"] == "acct-1"
    assert "alpha" not in mgr.providers


@pytest.mark.asyncio
async def test_update_rollback_preserves_stored_credential(
    monkeypatch,
) -> None:
    """Rollback goes through to_provider_section, so the raw key survives."""
    monkeypatch.delenv("SB_TEST_MISSING_KEY", raising=False)
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    store.upsert("alpha", _stored_fields())
    scope = _make_scope("PUT", headers=_admin_headers())
    body_in = json.dumps({
        "upstream": "https://broken.example.com",
        "provider_type": "generic",
        "target": 1,
        "key_mode": "env",
        "api_key_env": "SB_TEST_MISSING_KEY",
    }).encode()
    status, _ = await _invoke(
        "update", admin_token=ADMIN_TOKEN, scope=scope,
        mgr=mgr, store=store, body=body_in,
    )
    assert status == 400
    section = store.to_provider_section("alpha")
    assert section["api_key"] == SENTINEL_STORE
    assert section["upstream"] == "https://stored.example.com"


@pytest.mark.asyncio
async def test_update_unknown_name_404() -> None:
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    scope = _make_scope("PUT", headers=_admin_headers())
    status, _ = await _invoke(
        "update", admin_token=ADMIN_TOKEN, scope=scope,
        mgr=mgr, store=store,
        body=json.dumps({
            "upstream": "https://x.example.com",
            "provider_type": "generic",
            "target": 1,
            "key_mode": "passthrough",
        }).encode(),
    )
    assert status == 404


# ------------------------------------------------------------------ delete


@pytest.mark.asyncio
async def test_delete_store_only_removes_row_and_context() -> None:
    ctx = _make_ctx("alpha")
    mgr = ProviderManager({"alpha": ctx})
    store = ConfigStoreManager()
    store.upsert("alpha", _passthrough_fields())
    scope = _make_scope("DELETE", headers=_admin_headers())
    messages, send = _make_send()
    await handle_provider_delete(
        send, mgr, store, ADMIN_TOKEN, scope, "alpha",
        frozenset(), {},
    )
    status, body = _parse_response(messages)
    await mgr.shutdown()
    assert status == 200
    data = json.loads(body)
    assert data == {"removed": True, "tombstoned": False}
    assert store.get("alpha") is None
    assert "alpha" not in mgr.providers


@pytest.mark.asyncio
async def test_delete_toml_declared_writes_tombstone() -> None:
    """Deleting a TOML provider tombstones it so the next boot honors it."""
    toml_section = {
        "upstream": "https://toml.example.com",
        "type": "generic",
        "target": 2,
        "api_key": SENTINEL_TOML,
    }
    ctx = _make_ctx("alpha", upstream="https://toml.example.com")
    mgr = ProviderManager({"alpha": ctx})
    store = ConfigStoreManager()
    scope = _make_scope("DELETE", headers=_admin_headers())
    messages, send = _make_send()
    await handle_provider_delete(
        send, mgr, store, ADMIN_TOKEN, scope, "alpha",
        frozenset({"alpha"}), {"alpha": toml_section},
    )
    status, body = _parse_response(messages)
    await mgr.shutdown()
    assert status == 200
    data = json.loads(body)
    assert data == {"removed": True, "tombstoned": True}
    assert "alpha" not in mgr.providers

    row = store.get("alpha")
    assert row is not None
    assert row["enabled"] is False
    assert row["upstream"] == "https://toml.example.com"
    assert row["provider_type"] == "generic"
    assert row["target"] == 2
    # The inline TOML key was copied into the (write-only) store row.
    assert row["api_key_set"] is True

    # Boot merge: the tombstone suppresses the TOML provider (D1).
    effective = store.effective_providers({"provider": {"alpha": toml_section}})
    assert "alpha" not in effective


@pytest.mark.asyncio
async def test_delete_toml_declared_with_store_row_flips_enabled() -> None:
    ctx = _make_ctx("alpha")
    mgr = ProviderManager({"alpha": ctx})
    store = ConfigStoreManager()
    store.upsert("alpha", _stored_fields())
    scope = _make_scope("DELETE", headers=_admin_headers())
    messages, send = _make_send()
    await handle_provider_delete(
        send, mgr, store, ADMIN_TOKEN, scope, "alpha",
        frozenset({"alpha"}), {"alpha": {"upstream": "https://t.example.com"}},
    )
    status, body = _parse_response(messages)
    await mgr.shutdown()
    assert status == 200
    assert json.loads(body)["tombstoned"] is True
    row = store.get("alpha")
    assert row is not None
    assert row["enabled"] is False
    # Write-only key semantics kept the stored credential intact.
    assert store.to_provider_section("alpha")["api_key"] == SENTINEL_STORE


@pytest.mark.asyncio
async def test_delete_unknown_name_404() -> None:
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    scope = _make_scope("DELETE", headers=_admin_headers())
    messages, send = _make_send()
    await handle_provider_delete(
        send, mgr, store, ADMIN_TOKEN, scope, "ghost",
        frozenset(), {},
    )
    status, _ = _parse_response(messages)
    assert status == 404


@pytest.mark.asyncio
async def test_delete_disabled_store_row_is_200_not_404() -> None:
    """A tombstone is not live, but deleting it must still succeed."""
    mgr = ProviderManager({})
    store = ConfigStoreManager()
    store.upsert("alpha", _passthrough_fields(enabled=0))
    scope = _make_scope("DELETE", headers=_admin_headers())
    messages, send = _make_send()
    await handle_provider_delete(
        send, mgr, store, ADMIN_TOKEN, scope, "alpha",
        frozenset(), {},
    )
    status, body = _parse_response(messages)
    assert status == 200
    assert json.loads(body)["removed"] is True
    assert store.get("alpha") is None


# -------------------------------------------------------------------- list


@pytest.mark.asyncio
async def test_list_joins_live_and_store_with_tombstones() -> None:
    ctx_toml = _make_ctx("from-toml")
    ctx_store = _make_ctx("from-store")
    providers = {"from-toml": ctx_toml, "from-store": ctx_store}
    store = ConfigStoreManager()
    store.upsert("from-store", _stored_fields())
    store.upsert("dead", _passthrough_fields(enabled=0))

    messages, send = _make_send()
    await handle_providers_list(send, providers, store)
    status, body = _parse_response(messages)
    assert status == 200
    assert SENTINEL_STORE not in body.decode()
    entries = {e["name"]: e for e in json.loads(body)["providers"]}

    assert entries["from-toml"]["source"] == "toml"
    assert entries["from-toml"]["live"]["upstream"] == (
        "https://from-toml.example.com"
    )
    assert entries["from-store"]["source"] == "store"
    assert entries["from-store"]["api_key_set"] is True
    assert isinstance(entries["from-store"]["live"], dict)
    assert entries["dead"]["live"] is False
    assert entries["dead"]["enabled"] is False


# ----------------------------------------------------------- test endpoint


def _mock_factory(handler):
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=5.0,
        )

    return factory


@pytest.mark.asyncio
async def test_probe_ok_shape_and_no_credential_leak() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"data": []})

    ctx = _make_ctx("alpha", api_key="sk-test-credential-7777")
    providers = {"alpha": ctx}
    scope = _make_scope("POST", headers=_admin_headers())
    messages, send = _make_send()
    await handle_provider_test(
        send, providers, ADMIN_TOKEN, scope, "alpha",
        client_factory=_mock_factory(handler),
    )
    status, body = _parse_response(messages)
    assert status == 200
    text = body.decode()
    assert "sk-test-credential" not in text
    data = json.loads(body)
    assert data["ok"] is True
    assert data["status"] == 200
    assert isinstance(data["latency_ms"], (int, float))
    assert data["latency_ms"] >= 0
    # The credential went to the UPSTREAM request, and only there.
    assert captured["auth"] == "Bearer sk-test-credential-7777"
    assert captured["url"].endswith("/models")


@pytest.mark.asyncio
async def test_probe_connection_error_reports_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    ctx = _make_ctx("alpha")
    scope = _make_scope("POST", headers=_admin_headers())
    messages, send = _make_send()
    await handle_provider_test(
        send, {"alpha": ctx}, ADMIN_TOKEN, scope, "alpha",
        client_factory=_mock_factory(handler),
    )
    status, body = _parse_response(messages)
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is False
    assert data["status"] is None
    assert data["detail"] == "ConnectError"


@pytest.mark.asyncio
async def test_probe_timeout_reports_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    ctx = _make_ctx("alpha")
    scope = _make_scope("POST", headers=_admin_headers())
    messages, send = _make_send()
    await handle_provider_test(
        send, {"alpha": ctx}, ADMIN_TOKEN, scope, "alpha",
        client_factory=_mock_factory(handler),
    )
    _, body = _parse_response(messages)
    data = json.loads(body)
    assert data["ok"] is False
    assert data["detail"] == "timeout"


@pytest.mark.asyncio
async def test_probe_unknown_provider_404() -> None:
    scope = _make_scope("POST", headers=_admin_headers())
    messages, send = _make_send()
    await handle_provider_test(send, {}, ADMIN_TOKEN, scope, "ghost")
    status, _ = _parse_response(messages)
    assert status == 404


# -------------------------------------------------------- effective config


@pytest.mark.asyncio
async def test_effective_config_masks_every_credential() -> None:
    """Sentinel grep: neither the store's nor the TOML's key may appear."""
    store = ConfigStoreManager()
    store.upsert("stored-prov", _stored_fields())
    store.upsert("dead-prov", _passthrough_fields(enabled=0))
    toml_sections = {
        "toml-prov": {
            "upstream": "https://toml.example.com",
            "type": "generic",
            "target": 1,
            "api_key": SENTINEL_TOML,
        },
        "dead-prov": {
            "upstream": "https://dead.example.com",
        },
    }
    messages, send = _make_send()
    await handle_config_effective(
        send, store, frozenset(toml_sections), toml_sections,
    )
    status, body = _parse_response(messages)
    assert status == 200
    text = body.decode()
    assert SENTINEL_STORE not in text
    assert SENTINEL_TOML not in text

    entries = {e["name"]: e for e in json.loads(body)["providers"]}
    assert entries["stored-prov"]["source"] == "store"
    assert entries["stored-prov"]["api_key_set"] is True
    assert entries["stored-prov"]["api_key_hint"] == SENTINEL_STORE[-4:]
    assert entries["toml-prov"]["source"] == "toml"
    assert entries["toml-prov"]["api_key_set"] is True
    assert entries["toml-prov"]["api_key_hint"] == SENTINEL_TOML[-4:]
    assert "api_key" not in entries["toml-prov"]
    # The tombstone shadows its TOML section and reads disabled (D1).
    assert entries["dead-prov"]["source"] == "store"
    assert entries["dead-prov"]["enabled"] is False


# ------------------------------------------------------- dispatch (proxy)


def _make_app(
    providers: dict[str, ProviderContext],
    store: ConfigStoreManager,
    toml_sections: dict[str, dict[str, Any]] | None = None,
) -> ProxyApp:
    route_table = RouteTableManager(
        default_providers=tuple(providers) or ("test",)
    )
    sections = toml_sections or {}
    return ProxyApp(
        providers=providers,
        route_table=route_table,
        routing_config=RoutingConfig(),
        admin_token=ADMIN_TOKEN,
        config_store=store,
        toml_provider_names=frozenset(sections),
        toml_provider_sections=sections,
    )


async def _dispatch(
    app: ProxyApp,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> tuple[int, bytes]:
    scope = _make_scope(method, path, headers)
    receive = _make_receive(body)
    messages, send = _make_send()
    await app(scope, receive, send)
    return _parse_response(messages)


@pytest.mark.asyncio
async def test_dispatch_providers_get_requires_auth() -> None:
    app = _make_app({"test": _make_ctx("test")}, ConfigStoreManager())
    try:
        status, _ = await _dispatch(app, "GET", "/admin/providers")
        assert status == 401
        status, body = await _dispatch(
            app, "GET", "/admin/providers",
            headers=[(b"authorization", f"Bearer {ADMIN_TOKEN}".encode())],
        )
        assert status == 200
        assert json.loads(body)["providers"][0]["name"] == "test"
    finally:
        await app.provider_manager.shutdown()


@pytest.mark.asyncio
async def test_dispatch_effective_config_requires_auth() -> None:
    app = _make_app({"test": _make_ctx("test")}, ConfigStoreManager())
    status, _ = await _dispatch(app, "GET", "/admin/config/effective")
    assert status == 401
    status, _ = await _dispatch(
        app, "GET", "/admin/config/effective",
        headers=[(b"authorization", f"Bearer {ADMIN_TOKEN}".encode())],
    )
    assert status == 200


@pytest.mark.asyncio
async def test_dispatch_full_crud_round_trip(monkeypatch) -> None:
    """POST → PUT → DELETE through the real dispatch, override untouched."""
    monkeypatch.setenv("SB_TEST_BETA_KEY", "sk-dispatch-key-3333")
    app = _make_app({"test": _make_ctx("test")}, ConfigStoreManager())
    mgr = app.provider_manager
    try:
        status, body = await _dispatch(
            app, "POST", "/admin/providers",
            headers=_admin_headers(), body=_env_body(),
        )
        assert status == 200
        assert "beta" in app._providers

        status, body = await _dispatch(
            app, "PUT", "/admin/providers/beta",
            headers=_admin_headers(),
            body=_env_body(name=None, upstream="https://beta2.example.com"),
        )
        assert status == 200
        assert app._providers["beta"].upstream_url == (
            "https://beta2.example.com"
        )

        status, body = await _dispatch(
            app, "DELETE", "/admin/providers/beta",
            headers=_admin_headers(),
        )
        assert status == 200
        assert json.loads(body) == {"removed": True, "tombstoned": False}
        assert "beta" not in app._providers

        # The pre-existing override endpoint still routes (Plan 012 WI-3):
        # a fresh reading, then a runtime target override.
        await app._providers["test"].reconcile.tick()
        status, body = await _dispatch(
            app, "POST", "/admin/providers/test/override",
            headers=_admin_headers(),
            body=json.dumps({"target": 2}).encode(),
        )
        assert status == 200
        assert json.loads(body)["applied"] is True
    finally:
        await mgr.remove("beta")
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_dispatch_probe_routes_to_test_handler() -> None:
    """The /test path reaches the probe (unreachable upstream → ok false)."""
    ctx = _make_ctx("test", upstream="http://127.0.0.1:1")
    app = _make_app({"test": ctx}, ConfigStoreManager())
    status, body = await _dispatch(
        app, "POST", "/admin/providers/test/test",
        headers=_admin_headers(),
    )
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is False
    assert data["status"] is None
