"""Plan 016 (opportunistic quota-burn) — RETIRED by Plan 026 W2.2.

This file used to prove the mechanism worked end to end. It now proves it is
gone, and gone *safely*. The name is deliberately unchanged: the history is the
point, and a reader who greps for the feature should land on its retirement
rather than on an absence.

Why the mechanism went (Plan 026 §4 W2.2): pace supersedes it — Plan 016's
use-it-or-lose-it philosophy promoted from an opportunistic exception to a
primary ordering — and its reset-window heuristic actively favoured the
*expensive* provider, because a ~5 h session reset always sits inside the 6 h
burn window it treated as "spend this now".

Retirement is not deletion. Three things are pinned here, and each is a way an
operator can still be carrying Plan 016's configuration:

1. A TOML config with ``opportunistic_enabled = true`` boots, says so exactly
   once, and routes by pure strategy ordering — no fronting.
2. ``PUT /admin/config/routing`` refuses to write an opportunistic field, with
   a message naming the retirement and the replacement.
3. A stored routing overlay written back when the fields were mutable loads
   without crashing and stays inert.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from typing import Any

import httpx
import pytest

from switchboard.admin import handle_routing_config_update
from switchboard.cli import _build_serve_app
from switchboard.config_store import ConfigStoreManager
from switchboard.control import (
    RETIRED_ROUTING_FIELDS,
    RoutingConfig,
    RoutingStrategy,
)
from switchboard.gate import PermitGate
from switchboard.limit import BreakerConfig, CachedReading, LimitState
from switchboard.providers import ProviderContext
from switchboard.proxy import ProxyApp
from switchboard.reconcile import ReconciliationLoop
from switchboard.route_table import RouteTableManager
from switchboard.truth import NullTruthSource


def _make_scope(body: bytes = b"") -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer test-key")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8801),
        "scheme": "http",
    }


class _MockReceive:
    def __init__(self, body: bytes = b"") -> None:
        self._body = body
        self._sent = False

    async def __call__(self) -> dict[str, Any]:
        if not self._sent:
            self._sent = True
            return {
                "type": "http.request",
                "body": self._body,
                "more_body": False,
            }
        await asyncio.Future()
        return {"type": "http.disconnect"}


def _make_send() -> tuple[list[dict], Any]:
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


def _sse_response() -> httpx.Response:
    chunks = [
        b'data: {"id":"1","choices":[{"delta":{"content":"Hi"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    class _SSEStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            for c in chunks:
                yield c

        async def aclose(self) -> None:
            pass

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_SSEStream(),
    )


def _make_mocked_ctx(
    name: str,
    handler: Any,
    *,
    capacity: int = 3,
    requests_remaining: int | None = None,
    requests_limit: int | None = None,
    bucket_reset_epoch: float | None = None,
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
    reconcile._first_poll_ok = True
    reconcile._last_reading_cached = CachedReading(
        reading=LimitState(
            provider="generic",
            age_seconds=0.0,
            requests_remaining=requests_remaining,
            requests_limit=requests_limit,
            bucket_reset_epoch=bucket_reset_epoch,
        ),
        fetched_at_monotonic=0.0,
        ok=True,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ProviderContext(
        name=name,
        upstream_url="https://upstream.example.com",
        gate=gate,
        reconcile=reconcile,
        truth_source=truth,
        http_client=client,
    )


async def _send_request(app: ProxyApp, body: bytes) -> tuple[int, bytes]:
    scope = _make_scope(body=body)
    receive = _MockReceive(body=body)
    messages, send = _make_send()
    await app(scope, receive, send)
    return _parse_response(messages)


def _qualifying_estate() -> tuple[ProxyApp, dict[str, list[int]]]:
    """The estate that used to trigger opportunism, wired for either verdict.

    zai clears every one of Plan 016's conditions: non-primary, healthy, 70%
    session headroom (above the 0.5 floor), and a quota reset 3 h out (inside
    the 6 h window). It is the sole qualifier, so it did not even need to clear
    the margin. Under the retirement the primary serves.
    """
    calls: dict[str, list[int]] = {"umans": [], "zai": []}

    def primary_handler(request: httpx.Request) -> httpx.Response:
        calls["umans"].append(1)
        return _sse_response()

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        calls["zai"].append(1)
        return _sse_response()

    providers = {
        "umans": _make_mocked_ctx("umans", primary_handler, capacity=3),
        "zai": _make_mocked_ctx(
            "zai",
            fallback_handler,
            requests_remaining=70,
            requests_limit=100,
            bucket_reset_epoch=time.time() + 10800.0,
        ),
    }
    app = ProxyApp(
        providers=providers,
        route_table=RouteTableManager(default_providers=("umans", "zai")),
        routing_config=RoutingConfig(
            opportunistic_enabled=True,
            opportunistic_min_headroom=0.5,
            opportunistic_reset_window=21600.0,
            opportunistic_margin=0.10,
        ),
    )
    return app, calls


_BODY = json.dumps({"model": "test", "messages": []}).encode()


# ── 1. the routing half: the fields are inert ──────────────────────────────


@pytest.mark.asyncio
async def test_enabled_opportunistic_config_routes_by_strategy_order() -> None:
    """The estate that used to divert now serves from the primary.

    This is the assertion that changed sign, and it is the retirement itself:
    before Plan 026 this exact configuration sent live traffic to zai.
    """
    app, calls = _qualifying_estate()
    status, _ = await _send_request(app, _BODY)
    assert status == 200
    assert len(calls["umans"]) == 1
    assert len(calls["zai"]) == 0


@pytest.mark.asyncio
async def test_enabled_opportunistic_config_leaves_no_pin_behind() -> None:
    """A diversion used to create an affinity pin (Plan 016 pinned its
    selection so conversations did not flap). With no diversion there is
    nothing to pin, so the conversation is not quietly migrated either."""
    app, _calls = _qualifying_estate()
    status, _ = await _send_request(app, _BODY)
    assert status == 200
    assert list(app._affinity.values()) == []
    assert app.metrics.affinity_pins_total == 0
    assert app.metrics.failovers == 0


# ── 2. the boot half: it loads, and it says so once ────────────────────────


_SERVE_PROVIDER = (
    "[provider.umans]\n"
    'upstream = "https://api.example.com"\n'
    "target = 1\n"
)


def _boot(
    tmp_path: Any, toml_body: str, overlay: dict[str, Any] | None = None
) -> Any:
    """Boot a serve app the way ``switchboard serve`` does."""
    store_path = str(tmp_path / "store.sqlite3")
    if overlay is not None:
        seed = ConfigStoreManager(sqlite_path=store_path)
        seed.set_routing_overlay(overlay)
        seed.close()

    cfg = tmp_path / "config.toml"
    cfg.write_text(toml_body + _SERVE_PROVIDER)
    args = argparse.Namespace(
        command="serve",
        listen=None,
        config=str(cfg),
        admin_token=None,
        no_admin_token=True,
        log_level=None,
        queue_timeout=None,
        drain_timeout=None,
        route_table_store=store_path,
        max_request_body_bytes=None,
    )
    return _build_serve_app(args)[0]


def _retirement_warnings(records: list[logging.LogRecord]) -> list[str]:
    return [
        r.getMessage()
        for r in records
        if r.levelno >= logging.WARNING and "RETIRED" in r.getMessage()
    ]


def test_toml_with_opportunistic_enabled_boots_and_warns_once(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A file written for Plan 016 must not cost a boot — and must not go
    unmentioned either, or the operator keeps believing it works."""
    with caplog.at_level(logging.INFO, logger="switchboard.cli"):
        app = _boot(
            tmp_path,
            "[routing]\n"
            "opportunistic_enabled = true\n"
            "opportunistic_min_headroom = 0.6\n"
            "opportunistic_reset_window = 18000.0\n"
            "opportunistic_margin = 0.15\n",
        )

    # Parsed faithfully onto RoutingConfig (nothing reads it).
    assert app.routing_config.opportunistic_enabled is True
    assert app.routing_config.opportunistic_min_headroom == 0.6
    # And the decision is untouched: pure ordered strategy.
    assert app.routing_config.strategy is RoutingStrategy.ORDERED

    warnings = _retirement_warnings(caplog.records)
    assert len(warnings) == 1, warnings
    assert "Plan 026" in warnings[0]
    assert "pace" in warnings[0]


def test_default_config_says_nothing_about_the_retirement(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A warning that fires for every estate is a warning nobody reads. Only a
    config that actually enabled the mechanism hears about it."""
    with caplog.at_level(logging.INFO, logger="switchboard.cli"):
        app = _boot(tmp_path, "[routing]\ndwell_interval = 15.0\n")
    assert app.routing_config.opportunistic_enabled is False
    assert _retirement_warnings(caplog.records) == []


# ── 3. the persistence half: an old overlay row is harmless ────────────────


def test_stored_overlay_with_opportunistic_keys_boots_inert(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The row an admin PUT wrote while the fields were still mutable.

    It outlives the mechanism, so it must load rather than crash the boot or be
    silently dropped in a way that makes the warning depend on which door the
    value came through. It is loaded, announced once, and does nothing.
    """
    with caplog.at_level(logging.INFO, logger="switchboard.cli"):
        app = _boot(
            tmp_path,
            "",
            {
                "opportunistic_enabled": True,
                "opportunistic_min_headroom": 0.75,
                "strategy": "pace",
            },
        )
    assert app.routing_config.opportunistic_enabled is True
    assert app.routing_config.opportunistic_min_headroom == 0.75
    # The live knob in the same overlay still applies — retirement is per
    # field, not a poison pill for the whole row.
    assert app.routing_config.strategy is RoutingStrategy.PACE
    assert len(_retirement_warnings(caplog.records)) == 1


def test_stored_overlay_of_only_retired_keys_still_warns(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The regression this shape invites: an overlay holding *nothing but*
    retired keys must still be loaded, or the boot warning goes quiet for
    exactly the operator who most needs it."""
    with caplog.at_level(logging.INFO, logger="switchboard.cli"):
        app = _boot(tmp_path, "", {"opportunistic_enabled": True})
    assert app.routing_config.opportunistic_enabled is True
    assert len(_retirement_warnings(caplog.records)) == 1


# ── 4. the write surface: refused, with a reason ───────────────────────────


class _FakeProxyApp:
    def __init__(self, config: RoutingConfig) -> None:
        self._routing_config = config

    @property
    def routing_config(self) -> RoutingConfig:
        return self._routing_config

    def update_routing_config(self, config: RoutingConfig) -> None:
        self._routing_config = config


async def _put(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    app = _FakeProxyApp(RoutingConfig())
    messages, send = _make_send()

    async def receive() -> dict[str, Any]:
        return {
            "type": "http.request",
            "body": json.dumps(payload).encode(),
            "more_body": False,
        }

    await handle_routing_config_update(
        send,
        receive,
        app,
        "admin-secret",
        {
            "type": "http",
            "method": "PUT",
            "path": "/admin/config/routing",
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", b"Bearer admin-secret"),
                (b"sec-fetch-site", b"same-origin"),
            ],
            "query_string": b"",
        },
    )
    status, body = _parse_response(messages)
    return status, json.loads(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", RETIRED_ROUTING_FIELDS)
async def test_admin_put_refuses_a_retired_field(field: str) -> None:
    """Writing is a human deciding to rely on the mechanism *now*, so unlike a
    config file it gets an error rather than a warning — and one that names the
    retirement and the replacement instead of the generic immutable-field
    message, which would tell them to restart into something that is gone."""
    value: object = True if field == "opportunistic_enabled" else 0.5
    status, body = await _put({field: value})
    assert status == 400
    assert "retired" in body["error"]
    assert "Plan 026" in body["error"]
    assert 'strategy = "pace"' in body["error"]


@pytest.mark.asyncio
async def test_admin_put_refusal_does_not_apply_the_rest_of_the_body() -> None:
    """A body mixing a retired field with a live one is rejected whole. Partial
    application would leave the operator's intent half-expressed with a 400 to
    read it by."""
    status, body = await _put(
        {"opportunistic_enabled": True, "dwell_interval": 99.0}
    )
    assert status == 400
    assert "retired" in body["error"]


@pytest.mark.asyncio
async def test_admin_get_no_longer_advertises_the_retired_fields() -> None:
    """The reporting surfaces are derived from MUTABLE_ROUTING_FIELDS, so a
    retired field stops being offered as something to set."""
    from switchboard.admin import routing_config_payload

    payload = routing_config_payload(RoutingConfig())
    for field in RETIRED_ROUTING_FIELDS:
        assert field not in payload
