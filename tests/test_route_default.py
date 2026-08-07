"""Default-route persistence and runtime editing (Plan 020 WI-8).

Before this work the default route was boot-only: `set_default_providers`
was in-memory, and no admin endpoint reached it. That made GUI provider
management a dead end — the model map only *filters* a route's candidate
list, so a provider that no route names is unreachable however it was
created.

These tests pin the two halves: the store round-trip (including the D1
precedence rule against TOML) and the PUT endpoint's validation.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

import httpx
import pytest

from switchboard.admin import handle_route_default_set, handle_route_list
from switchboard.control import RoutingConfig
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.session import SESSION_COOKIE, mint_session
from switchboard.truth import NullTruthSource


def _make_provider_context(name: str = "test") -> ProviderContext:
    gate = PermitGate(initial_capacity=0)
    truth = NullTruthSource(provider="generic")
    return ProviderContext(
        name=name,
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=ReconciliationLoop(
            truth_source=truth,
            gate=gate,
            max_concurrency=1,
            provider_type="generic",
            breaker_config=BreakerConfig(),
        ),
        truth_source=truth,
        http_client=httpx.AsyncClient(),
    )


def _make_scope(
    method: str = "PUT",
    path: str = "/admin/routes/default",
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


def _authed_scope(method: str = "PUT") -> dict[str, Any]:
    return _make_scope(
        method=method,
        path="/admin/routes/default",
        headers=[
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )


async def _put_default(
    mgr: RouteTableManager,
    body: object,
    *,
    providers: dict[str, Any] | None = None,
    admin_token: str | None = "admin-secret",
    scope: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    receive = _make_receive(
        body if isinstance(body, bytes) else json.dumps(body).encode()
    )
    messages, send = _make_send()
    await handle_route_default_set(
        send,
        receive,
        mgr,
        admin_token,
        scope if scope is not None else _authed_scope(),
        None,
        providers,
    )
    status, raw = _parse_response(messages)
    return status, json.loads(raw) if raw else {}


# --- persistence -----------------------------------------------------------


def test_default_route_survives_a_restart(tmp_path) -> None:
    """The whole point of WI-8: a GUI-set default must outlive the process."""
    store = str(tmp_path / "routes.db")

    mgr = RouteTableManager(default_providers=("umans",), sqlite_path=store)
    mgr.set_default_providers(("ollama", "umans"), persist=True)
    mgr.close()

    reopened = RouteTableManager(default_providers=("umans",), sqlite_path=store)
    assert reopened.default_providers == ("ollama", "umans")
    assert reopened.default_from_store is True
    reopened.close()


def test_in_memory_set_is_not_persisted(tmp_path) -> None:
    """persist=False is the boot-merge path: conclusions must not stick.

    The boot merge filters tombstoned and unknown providers out of the
    default. Writing those filtered values back would freeze a transient
    condition — re-enable the provider and the narrowed default would still
    be on disk.
    """
    store = str(tmp_path / "routes.db")

    mgr = RouteTableManager(default_providers=("umans",), sqlite_path=store)
    mgr.set_default_providers(("ollama",))
    assert mgr.default_providers == ("ollama",)
    assert mgr.default_from_store is False
    mgr.close()

    reopened = RouteTableManager(default_providers=("umans",), sqlite_path=store)
    assert reopened.default_providers == ("umans",)
    assert reopened.default_from_store is False
    reopened.close()


def test_stored_default_outranks_toml(tmp_path) -> None:
    """D1: a store row wins wholesale, so a GUI edit is not undone by TOML."""
    store = str(tmp_path / "routes.db")
    config = {"route": {"default": {"providers": ["umans"]}}}

    mgr = RouteTableManager(sqlite_path=store)
    mgr.set_default_providers(("ollama",), persist=True)
    mgr.close()

    reopened = RouteTableManager(sqlite_path=store)
    reopened.load_from_config(config, overwrite=False)
    assert reopened.default_providers == ("ollama",)
    reopened.close()


def test_toml_wins_when_overwrite_is_set(tmp_path) -> None:
    """overwrite=True is the no-store boot path; TOML must still win there."""
    store = str(tmp_path / "routes.db")
    config = {"route": {"default": {"providers": ["umans"]}}}

    mgr = RouteTableManager(sqlite_path=store)
    mgr.set_default_providers(("ollama",), persist=True)
    mgr.close()

    reopened = RouteTableManager(sqlite_path=store)
    reopened.load_from_config(config, overwrite=True)
    assert reopened.default_providers == ("umans",)
    reopened.close()


def test_default_route_is_not_a_keyed_entry(tmp_path) -> None:
    """The default lives in its own table, so it never shows up as a route."""
    store = str(tmp_path / "routes.db")
    mgr = RouteTableManager(sqlite_path=store)
    mgr.add_entry("hashedkey", ["umans"])
    mgr.set_default_providers(("ollama",), persist=True)

    assert [e.key for e in mgr.list_entries()] == ["hashedkey"]
    assert mgr.get_route_table().default_providers == ("ollama",)
    mgr.close()


@pytest.mark.parametrize(
    "bad_value",
    [
        "not json at all",
        json.dumps({"providers": ["umans"]}),  # right idea, wrong shape
        json.dumps([1, 2, 3]),
        json.dumps([]),
    ],
)
def test_corrupt_stored_default_does_not_brick_boot(tmp_path, bad_value) -> None:
    """A bad row falls back to the declared default rather than crashing.

    The store is operator-writable state on a PVC; a malformed row must be
    survivable without hand-editing SQLite to get the process up.
    """
    store = str(tmp_path / "routes.db")
    RouteTableManager(sqlite_path=store).close()

    db = sqlite3.connect(store)
    db.execute(
        "INSERT OR REPLACE INTO route_default (id, providers, updated_at) "
        "VALUES (1, ?, 0.0)",
        (bad_value,),
    )
    db.commit()
    db.close()

    mgr = RouteTableManager(default_providers=("umans",), sqlite_path=store)
    assert mgr.default_providers == ("umans",)
    assert mgr.default_from_store is False
    mgr.close()


def test_memory_only_manager_accepts_persist(tmp_path) -> None:
    """No store configured: the write applies live and is simply not durable."""
    mgr = RouteTableManager(default_providers=("umans",))
    mgr.set_default_providers(("ollama",), persist=True)
    assert mgr.default_providers == ("ollama",)


# --- the PUT endpoint ------------------------------------------------------


@pytest.mark.asyncio
async def test_put_default_sets_and_reports_persistence(tmp_path) -> None:
    store = str(tmp_path / "routes.db")
    mgr = RouteTableManager(default_providers=("umans",), sqlite_path=store)
    providers = {"umans": _make_provider_context("umans"),
                 "ollama": _make_provider_context("ollama")}

    status, data = await _put_default(
        mgr, {"providers": ["ollama", "umans"]}, providers=providers
    )
    assert status == 200
    assert data["default"] == ["ollama", "umans"]
    assert data["persisted"] is True
    assert mgr.default_providers == ("ollama", "umans")
    mgr.close()


@pytest.mark.asyncio
async def test_put_default_is_visible_in_route_list() -> None:
    """The GUI reads the default back from GET /admin/routes."""
    mgr = RouteTableManager(default_providers=("umans",))
    providers = {"umans": _make_provider_context("umans"),
                 "ollama": _make_provider_context("ollama")}

    await _put_default(mgr, {"providers": ["ollama"]}, providers=providers)

    messages, send = _make_send()
    await handle_route_list(send, mgr)
    _, raw = _parse_response(messages)
    assert json.loads(raw)["default"] == ["ollama"]


@pytest.mark.asyncio
async def test_put_default_rejects_unknown_provider() -> None:
    """Reject on write — the operator gets told, rather than silently
    configuring a default that routes nowhere."""
    mgr = RouteTableManager(default_providers=("umans",))
    providers = {"umans": _make_provider_context("umans")}

    status, data = await _put_default(
        mgr, {"providers": ["umans", "nope"]}, providers=providers
    )
    assert status == 400
    assert "nope" in data["error"]
    assert mgr.default_providers == ("umans",)


@pytest.mark.asyncio
async def test_put_default_rejects_empty_list() -> None:
    """An empty default would 503 every unkeyed request."""
    mgr = RouteTableManager(default_providers=("umans",))
    status, _ = await _put_default(
        mgr, {"providers": []}, providers={"umans": _make_provider_context()}
    )
    assert status == 400
    assert mgr.default_providers == ("umans",)


@pytest.mark.asyncio
async def test_put_default_rejects_duplicates() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    providers = {"umans": _make_provider_context("umans"),
                 "ollama": _make_provider_context("ollama")}
    status, data = await _put_default(
        mgr, {"providers": ["umans", "ollama", "umans"]}, providers=providers
    )
    assert status == 400
    assert "umans" in data["error"]


@pytest.mark.asyncio
async def test_put_default_rejects_non_string_providers() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    status, _ = await _put_default(mgr, {"providers": ["umans", 7]})
    assert status == 400


@pytest.mark.asyncio
async def test_put_default_rejects_bad_json() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    status, _ = await _put_default(mgr, b"{not json")
    assert status == 400


@pytest.mark.asyncio
async def test_put_default_requires_json_content_type() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(
        method="PUT",
        path="/admin/routes/default",
        headers=[
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    status, _ = await _put_default(mgr, {"providers": ["umans"]}, scope=scope)
    assert status == 415


@pytest.mark.asyncio
async def test_put_default_requires_auth() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    scope = _make_scope(
        method="PUT",
        path="/admin/routes/default",
        headers=[(b"content-type", b"application/json")],
    )
    status, _ = await _put_default(mgr, {"providers": ["ollama"]}, scope=scope)
    assert status == 403
    assert mgr.default_providers == ("umans",)


@pytest.mark.asyncio
async def test_put_default_blocked_cross_site_cookie() -> None:
    """Cookie-authenticated cross-site requests must be CSRF-blocked.

    A bearer token deliberately short-circuits this check — a browser cannot
    attach one cross-site, so its presence disproves CSRF. The cookie is the
    ambient credential that needs the guard.
    """
    mgr = RouteTableManager(default_providers=("umans",))
    cookie = mint_session("admin-secret", time.time(), 3600)
    scope = _make_scope(
        method="PUT",
        path="/admin/routes/default",
        headers=[
            (b"content-type", b"application/json"),
            (b"cookie", f"{SESSION_COOKIE}={cookie}".encode()),
            (b"sec-fetch-site", b"cross-site"),
        ],
    )
    status, data = await _put_default(
        mgr, {"providers": ["ollama"]}, scope=scope
    )
    assert status == 403
    assert "cross-site" in data["error"]
    assert mgr.default_providers == ("umans",)


@pytest.mark.asyncio
async def test_put_default_allowed_same_origin_cookie() -> None:
    """The counterpart: the GUI's own same-origin fetch must succeed."""
    mgr = RouteTableManager(default_providers=("umans",))
    cookie = mint_session("admin-secret", time.time(), 3600)
    scope = _make_scope(
        method="PUT",
        path="/admin/routes/default",
        headers=[
            (b"content-type", b"application/json"),
            (b"cookie", f"{SESSION_COOKIE}={cookie}".encode()),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    status, _ = await _put_default(
        mgr,
        {"providers": ["ollama"]},
        providers={"ollama": _make_provider_context("ollama")},
        scope=scope,
    )
    assert status == 200
    assert mgr.default_providers == ("ollama",)


@pytest.mark.asyncio
async def test_put_default_disabled_without_admin_token() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    status, _ = await _put_default(
        mgr, {"providers": ["ollama"]}, admin_token=None
    )
    assert status == 405
    assert mgr.default_providers == ("umans",)


# --- dispatch --------------------------------------------------------------


def _make_app(mgr: RouteTableManager, providers: dict[str, Any]) -> ProxyApp:
    return ProxyApp(
        providers=providers,
        route_table=mgr,
        routing_config=RoutingConfig(),
        admin_token="admin-secret",
    )


async def _dispatch(
    app: ProxyApp, method: str, path: str, body: bytes = b""
) -> tuple[int, bytes]:
    scope = _make_scope(
        method=method,
        path=path,
        headers=[
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    )
    messages, send = _make_send()
    await app(scope, _make_receive(body), send)
    return _parse_response(messages)


@pytest.mark.asyncio
async def test_dispatch_put_default_reaches_the_handler() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    app = _make_app(mgr, {"umans": _make_provider_context("umans"),
                          "ollama": _make_provider_context("ollama")})
    status, _ = await _dispatch(
        app, "PUT", "/admin/routes/default",
        json.dumps({"providers": ["ollama"]}).encode(),
    )
    assert status == 200
    assert mgr.default_providers == ("ollama",)


@pytest.mark.asyncio
async def test_dispatch_delete_default_is_405_not_a_key_lookup() -> None:
    """The default branch must precede the generic /admin/routes/<key> DELETE.

    Without the ordering, DELETE /admin/routes/default falls through and is
    treated as a hashed key, answering 404 "route not found" — which reads as
    "your default route is missing" rather than "that is not deletable".
    """
    mgr = RouteTableManager(default_providers=("umans",))
    app = _make_app(mgr, {"umans": _make_provider_context("umans")})
    status, _ = await _dispatch(app, "DELETE", "/admin/routes/default")
    assert status == 405
    assert mgr.default_providers == ("umans",)


@pytest.mark.asyncio
async def test_dispatch_keyed_route_delete_still_works() -> None:
    """The guard above must not shadow ordinary keyed-route deletion."""
    mgr = RouteTableManager(default_providers=("umans",))
    mgr.add_entry("abc123", ["umans"])
    app = _make_app(mgr, {"umans": _make_provider_context("umans")})
    status, _ = await _dispatch(app, "DELETE", "/admin/routes/abc123")
    assert status == 200
    assert mgr.list_entries() == []
