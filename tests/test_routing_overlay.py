"""A routing change made through the admin API must survive a restart.

Plan 020 WI-8a made the default route persistent precisely so operator intent
is not silently undone by the next pod restart. The routing strategy is the
same kind of decision: an operator who selects `pace` in the GUI, sees traffic
move, and then watches a rollout put them back on `ordered` has been lied to.

These tests cover the round trip — admin write, process restart, boot merge —
and the fail-safe behaviours around it: an unreadable or invalid overlay must
degrade to TOML rather than take routing down.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from switchboard.admin import handle_routing_config_update
from switchboard.cli import _build_serve_app
from switchboard.config_store import ConfigStoreManager
from switchboard.control import RoutingConfig, RoutingStrategy


class _FakeProxyApp:
    def __init__(self, config: RoutingConfig, store: ConfigStoreManager) -> None:
        self._routing_config = config
        self._store = store

    @property
    def routing_config(self) -> RoutingConfig:
        return self._routing_config

    @property
    def config_store(self) -> ConfigStoreManager:
        return self._store

    def update_routing_config(self, config: RoutingConfig) -> None:
        self._routing_config = config


def _make_receive(body: bytes):
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _make_send():
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    return messages, send


def _authed_scope() -> dict[str, Any]:
    return {
        "type": "http",
        "method": "PUT",
        "path": "/admin/config/routing",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ],
        "query_string": b"",
    }


async def _put(store: ConfigStoreManager, body: dict[str, Any]) -> dict[str, Any]:
    app = _FakeProxyApp(RoutingConfig(), store)
    messages, send = _make_send()
    await handle_routing_config_update(
        send,
        _make_receive(json.dumps(body).encode()),
        app,
        "admin-secret",
        _authed_scope(),
    )
    start = next(m for m in messages if m["type"] == "http.response.start")
    payload = next(m for m in messages if m["type"] == "http.response.body")
    return {
        "status": start["status"],
        "body": json.loads(payload["body"]),
        "app": app,
    }


@pytest.mark.asyncio
async def test_strategy_survives_a_restart(tmp_path) -> None:
    """The whole point: select pace, restart, still pace."""
    db_path = str(tmp_path / "store.sqlite3")
    store = ConfigStoreManager(sqlite_path=db_path)
    result = await _put(store, {"strategy": "pace"})
    assert result["status"] == 200
    assert result["body"]["persisted"] is True
    store.close()

    # Restart: a brand-new manager over the same file, as boot would build it.
    reopened = ConfigStoreManager(sqlite_path=db_path)
    assert reopened.get_routing_overlay() == {"strategy": "pace"}


@pytest.mark.asyncio
async def test_overlay_accumulates_across_requests(tmp_path) -> None:
    """Each PUT merges into the stored overlay instead of replacing it, so
    setting the burn rate does not silently discard the strategy set a minute
    earlier."""
    db_path = str(tmp_path / "store.sqlite3")
    store = ConfigStoreManager(sqlite_path=db_path)
    await _put(store, {"strategy": "pace"})
    await _put(store, {"pace_burn_rate_per_day": 0.2})
    assert store.get_routing_overlay() == {
        "strategy": "pace",
        "pace_burn_rate_per_day": 0.2,
    }


@pytest.mark.asyncio
async def test_untouched_fields_are_not_frozen(tmp_path) -> None:
    """Only fields the operator actually set are persisted. Storing a whole
    RoutingConfig would freeze every default at the value it had the day they
    first opened the GUI, so a later TOML change would be silently ignored."""
    db_path = str(tmp_path / "store.sqlite3")
    store = ConfigStoreManager(sqlite_path=db_path)
    await _put(store, {"strategy": "pace"})
    overlay = store.get_routing_overlay()
    assert set(overlay) == {"strategy"}
    assert "dwell_interval" not in overlay
    assert "headroom_threshold" not in overlay


@pytest.mark.asyncio
async def test_rejected_request_persists_nothing(tmp_path) -> None:
    """A 400 must not leave a partial overlay behind."""
    db_path = str(tmp_path / "store.sqlite3")
    store = ConfigStoreManager(sqlite_path=db_path)
    result = await _put(store, {"strategy": "sideways"})
    assert result["status"] == 400
    assert store.get_routing_overlay() == {}


@pytest.mark.asyncio
async def test_without_persistence_the_response_says_so() -> None:
    """No store file: the swap is live but will not survive a restart, and the
    response reports that rather than letting the GUI imply durability."""
    store = ConfigStoreManager()  # in-memory, no sqlite
    result = await _put(store, {"strategy": "pace"})
    assert result["status"] == 200
    assert result["body"]["persisted"] is False
    assert result["app"].routing_config.strategy == RoutingStrategy.PACE


def test_unreadable_overlay_degrades_to_toml(tmp_path) -> None:
    """A corrupt overlay must not take routing down — it is a preference, and
    falling back to the TOML section is always safe."""
    db_path = str(tmp_path / "store.sqlite3")
    store = ConfigStoreManager(sqlite_path=db_path)
    assert store.db is not None
    store.db.execute(
        "INSERT OR REPLACE INTO routing_config (id, overlay, updated_at) "
        "VALUES (1, ?, ?)",
        ("{not json at all", 0.0),
    )
    store.db.commit()
    assert store.get_routing_overlay() == {}


def test_non_object_overlay_is_ignored(tmp_path) -> None:
    """Valid JSON of the wrong shape is still not an overlay."""
    db_path = str(tmp_path / "store.sqlite3")
    store = ConfigStoreManager(sqlite_path=db_path)
    assert store.db is not None
    store.db.execute(
        "INSERT OR REPLACE INTO routing_config (id, overlay, updated_at) "
        "VALUES (1, ?, ?)",
        (json.dumps(["pace"]), 0.0),
    )
    store.db.commit()
    assert store.get_routing_overlay() == {}


def test_no_overlay_reads_empty(tmp_path) -> None:
    """A fresh store has no opinion about routing."""
    store = ConfigStoreManager(sqlite_path=str(tmp_path / "store.sqlite3"))
    assert store.get_routing_overlay() == {}


# -- the boot half: the overlay has to actually reach RoutingConfig ---------


_SERVE_PROVIDER = (
    "[provider.umans]\n"
    'upstream = "https://api.example.com"\n'
    "target = 1\n"
)


def _boot(tmp_path, toml_body: str, overlay: dict[str, Any] | None):
    """Build a serve app the way `switchboard serve` does, with an overlay
    already sitting in the store, and hand back its RoutingConfig."""
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
        log_level=None,
        queue_timeout=None,
        drain_timeout=None,
        route_table_store=store_path,
        max_request_body_bytes=None,
    )
    app = _build_serve_app(args)[0]
    return app.routing_config


def test_boot_applies_the_stored_strategy(tmp_path) -> None:
    """The GUI selection is still in force after a restart."""
    config = _boot(tmp_path, "", {"strategy": "pace"})
    assert config.strategy == RoutingStrategy.PACE


def test_stored_overlay_outranks_toml(tmp_path) -> None:
    """Plan 020 D1: the store wins. The operator changed this more recently
    than whoever last edited the file."""
    config = _boot(
        tmp_path,
        '[routing]\nstrategy = "headroom"\n',
        {"strategy": "pace"},
    )
    assert config.strategy == RoutingStrategy.PACE


def test_toml_still_applies_to_fields_the_overlay_omits(tmp_path) -> None:
    """An overlay is not a whole config. Knobs the operator never touched keep
    following the file."""
    config = _boot(
        tmp_path,
        "[routing]\ndwell_interval = 45.0\n",
        {"strategy": "pace"},
    )
    assert config.strategy == RoutingStrategy.PACE
    assert config.dwell_interval == 45.0


def test_boot_without_an_overlay_uses_toml(tmp_path) -> None:
    config = _boot(tmp_path, '[routing]\nstrategy = "headroom"\n', None)
    assert config.strategy == RoutingStrategy.HEADROOM


def test_boot_drops_an_out_of_range_overlay_value(tmp_path) -> None:
    """A preference that no longer validates must not cost availability: drop
    the field, keep booting, keep the TOML value."""
    config = _boot(
        tmp_path,
        "[routing]\npace_burn_rate_per_day = 0.2\n",
        {"pace_burn_rate_per_day": 99.0, "strategy": "pace"},
    )
    assert config.pace_burn_rate_per_day == 0.2
    assert config.strategy == RoutingStrategy.PACE


def test_boot_ignores_an_immutable_field_in_the_overlay(tmp_path) -> None:
    """Only runtime-mutable fields are honoured, whatever ended up on disk."""
    config = _boot(
        tmp_path,
        "",
        {"affinity_max_entries": 99999, "strategy": "pace"},
    )
    assert config.affinity_max_entries == RoutingConfig().affinity_max_entries
    assert config.strategy == RoutingStrategy.PACE
