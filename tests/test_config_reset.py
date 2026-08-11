"""Reclaiming config state from the store (Plan 021 D7, WI-8).

The store outranks the mounted TOML, which is what makes GUI edits survive a
restart — and also what makes a bad one unfixable by editing the configmap and
rolling the pod. These tests pin the way back.

The scenario in `test_reset_recovers_the_live_model_map_divergence` is not
hypothetical: it is the state found on the running deployment on 2026-08-08.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from switchboard.config_reset import (
    SECTIONS,
    ResetError,
    parse_sections,
    reset_sections,
)
from switchboard.model_map import ModelMapManager
from switchboard.route_table import RouteTableManager

# --- section parsing -------------------------------------------------------


def test_parse_single_and_multiple() -> None:
    assert parse_sections("model-map") == ["model-map"]
    assert parse_sections("routes,providers") == ["routes", "providers"]


def test_parse_all_expands() -> None:
    assert parse_sections("all") == sorted(SECTIONS)


def test_all_cannot_be_combined() -> None:
    with pytest.raises(ResetError, match="cannot be combined"):
        parse_sections("all,routes")


def test_unknown_section_names_the_valid_ones() -> None:
    """A typo must not reset nothing (operator concludes the store was clean)
    nor everything (unrecoverable)."""
    with pytest.raises(ResetError, match="unknown section"):
        parse_sections("model_map")
    try:
        parse_sections("nope")
    except ResetError as exc:
        assert "model-map" in str(exc) and "providers" in str(exc)


def test_empty_is_rejected() -> None:
    with pytest.raises(ResetError, match="no sections"):
        parse_sections("  ,  ")


def test_duplicates_collapse_preserving_order() -> None:
    assert parse_sections("routes,providers,routes") == ["routes", "providers"]


# --- reset behaviour -------------------------------------------------------


def _store(tmp_path) -> str:
    return str(tmp_path / "store.sqlite")


def test_reset_clears_rows_and_names_them(tmp_path) -> None:
    path = _store(tmp_path)
    rt = RouteTableManager(sqlite_path=path)
    rt.add_entry("hashedkeyA", ["umans"])
    rt.add_entry("hashedkeyB", ["ollama"])
    rt.set_default_providers(("ollama", "umans"), persist=True)
    rt.close()

    db = sqlite3.connect(path)
    try:
        deleted = reset_sections(db, ["routes", "route-default"])
    finally:
        db.close()

    assert sorted(deleted["routes"]) == ["hashedkeyA", "hashedkeyB"]
    assert deleted["route-default"] == ['["ollama", "umans"]']

    reopened = RouteTableManager(default_providers=("declared",), sqlite_path=path)
    assert reopened.list_entries() == []
    assert reopened.default_providers == ("declared",)
    assert reopened.default_from_store is False
    reopened.close()


def test_reset_recovers_the_live_model_map_divergence(tmp_path) -> None:
    """The case this exists for, reproduced from the live deployment.

    The store's glm-5.2 aliases omitted the primary provider while the
    configmap included it, so every glm-5.2 request routed away from the
    provider being paid for. Because load_from_config seeds only ABSENT models
    when a store is configured, editing the configmap could not fix it.
    """
    path = _store(tmp_path)
    declared: dict[str, Any] = {
        "model": {
            "glm-5.2": {
                "opencode-go": "glm-5.2",
                "ollama-cloud": "glm-5.2",
            }
        }
    }

    conn = sqlite3.connect(path)
    ModelMapManager(db=conn).set_model(
        "glm-5.2", {"ollama-cloud": "glm-5.2"}  # the bad GUI state
    )
    # Confirm the trap first: reloading the declared config does NOT fix it.
    stuck = ModelMapManager(db=conn)
    stuck.load_from_config(declared, overwrite=False)
    assert "opencode-go" not in dict(stuck.list_models())["glm-5.2"]
    conn.close()

    db = sqlite3.connect(path)
    try:
        deleted = reset_sections(db, ["model-map"])
    finally:
        db.close()
    assert deleted["model-map"] == ["glm-5.2"]

    conn2 = sqlite3.connect(path)
    recovered = ModelMapManager(db=conn2)
    recovered.load_from_config(declared, overwrite=False)
    assert dict(recovered.list_models())["glm-5.2"] == {
        "opencode-go": "glm-5.2",
        "ollama-cloud": "glm-5.2",
    }
    conn2.close()


def test_reset_of_a_never_used_section_is_success_not_error(tmp_path) -> None:
    """Tables are created lazily per feature, so a deployment that never used
    the model map has no model_map table. "Reset something never written" is a
    success, and must not raise."""
    path = _store(tmp_path)
    RouteTableManager(sqlite_path=path).close()  # creates routes only

    db = sqlite3.connect(path)
    try:
        deleted = reset_sections(db, sorted(SECTIONS))
    finally:
        db.close()
    assert deleted["model-map"] == []
    assert deleted["providers"] == []


def test_reset_without_a_store_reports_empty(tmp_path) -> None:
    assert reset_sections(None, ["providers"]) == {"providers": []}


def test_reset_all_on_completely_fresh_database(tmp_path) -> None:
    """A database with NO tables at all (never opened by any manager) must
    not raise — _row_labels guards every table, including route_default."""
    path = _store(tmp_path)
    db = sqlite3.connect(path)
    try:
        deleted = reset_sections(db, sorted(SECTIONS))
    finally:
        db.close()
    for section in SECTIONS:
        assert deleted[section] == []


def test_reset_routing_config_clears_runtime_overlay(tmp_path) -> None:
    """routing-config section must be in SECTIONS and clear the overlay."""
    path = _store(tmp_path)
    RouteTableManager(sqlite_path=path).close()
    from switchboard.config_store import ConfigStoreManager

    cs = ConfigStoreManager(sqlite_path=path)
    cs.set_routing_overlay({"strategy": "pace"})
    cs.close()

    db = sqlite3.connect(path)
    try:
        deleted = reset_sections(db, ["routing-config"])
    finally:
        db.close()
    assert len(deleted["routing-config"]) == 1

    db = sqlite3.connect(path)
    try:
        row = db.execute(
            "SELECT overlay FROM routing_config WHERE id = 1"
        ).fetchone()
    finally:
        db.close()
    assert row is None


def test_reset_is_scoped_to_the_named_sections(tmp_path) -> None:
    """Resetting one section must not take the others with it — the operator
    is reclaiming one surface, not wiping the deployment."""
    path = _store(tmp_path)
    rt = RouteTableManager(sqlite_path=path)
    rt.add_entry("keep-me", ["umans"])
    rt.close()
    conn = sqlite3.connect(path)
    ModelMapManager(db=conn).set_model("glm-5.2", {"ollama-cloud": "glm-5.2"})
    conn.close()

    db = sqlite3.connect(path)
    try:
        reset_sections(db, ["model-map"])
    finally:
        db.close()

    rt2 = RouteTableManager(sqlite_path=path)
    assert [e.key for e in rt2.list_entries()] == ["keep-me"]
    rt2.close()
    conn2 = sqlite3.connect(path)
    assert ModelMapManager(db=conn2).list_models() == []
    conn2.close()


def test_every_declared_section_maps_to_a_real_table(tmp_path) -> None:
    """Guards the failure this module could silently have: a SECTIONS entry
    naming a table nobody creates would report success while resetting
    nothing."""
    path = _store(tmp_path)
    RouteTableManager(sqlite_path=path).close()
    conn = sqlite3.connect(path)
    ModelMapManager(db=conn)  # creates model_map
    conn.close()
    from switchboard.config_store import ConfigStoreManager

    ConfigStoreManager(sqlite_path=path).close()

    db = sqlite3.connect(path)
    try:
        present = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        db.close()
    for section, (table, _) in SECTIONS.items():
        assert table in present, f"{section} -> {table} is not a real table"


def test_json_labels_survive_round_trip(tmp_path) -> None:
    """The default-route label is the stored JSON; it must be readable back."""
    path = _store(tmp_path)
    rt = RouteTableManager(sqlite_path=path)
    rt.set_default_providers(("a", "b"), persist=True)
    rt.close()
    db = sqlite3.connect(path)
    try:
        deleted = reset_sections(db, ["route-default"])
    finally:
        db.close()
    assert json.loads(deleted["route-default"][0]) == ["a", "b"]


# --- the admin endpoint ----------------------------------------------------


def _scope(method: str = "POST", authed: bool = True) -> dict[str, Any]:
    headers = [(b"content-type", b"application/json")]
    if authed:
        headers += [
            (b"authorization", b"Bearer admin-secret"),
            (b"sec-fetch-site", b"same-origin"),
        ]
    return {
        "type": "http", "method": method, "path": "/admin/config/reset",
        "raw_path": b"/admin/config/reset", "query_string": b"",
        "headers": headers, "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8801), "scheme": "http",
    }


def _receive(body: bytes):
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def _send():
    msgs: list[dict] = []

    async def send(m: dict) -> None:
        msgs.append(m)

    return msgs, send


def _parse(msgs: list[dict]) -> tuple[int, dict]:
    status, body = 0, b""
    for m in msgs:
        if m["type"] == "http.response.start":
            status = m["status"]
        elif m["type"] == "http.response.body":
            body += m.get("body", b"")
    return status, (json.loads(body) if body else {})


async def _post(rt, payload, *, token="admin-secret", scope=None):
    from switchboard.admin import handle_config_reset

    msgs, send = _send()
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    await handle_config_reset(
        send, _receive(raw), rt, token, scope or _scope(), None
    )
    return _parse(msgs)


@pytest.mark.asyncio
async def test_endpoint_resets_and_reports_what_it_deleted(tmp_path) -> None:
    path = _store(tmp_path)
    rt = RouteTableManager(sqlite_path=path)
    rt.add_entry("keyA", ["umans"])
    try:
        status, data = await _post(rt, {"sections": ["routes"]})
    finally:
        rt.close()
    assert status == 200
    assert data["reset"] == ["routes"]
    assert data["deleted"]["routes"] == ["keyA"]
    assert data["persisted"] is True


@pytest.mark.asyncio
async def test_endpoint_rejects_unknown_section_and_lists_valid(tmp_path) -> None:
    rt = RouteTableManager()
    status, data = await _post(rt, {"sections": ["model_map"]})
    assert status == 400
    assert "model-map" in data["valid_sections"]


@pytest.mark.asyncio
async def test_endpoint_requires_sections(tmp_path) -> None:
    rt = RouteTableManager()
    status, _ = await _post(rt, {})
    assert status == 400


@pytest.mark.asyncio
async def test_endpoint_disabled_without_admin_token(tmp_path) -> None:
    """Reset is destructive; a tokenless deployment must not expose it."""
    path = _store(tmp_path)
    rt = RouteTableManager(sqlite_path=path)
    rt.add_entry("keyA", ["umans"])
    try:
        status, _ = await _post(rt, {"sections": ["routes"]}, token=None)
        assert status == 405
        assert [e.key for e in rt.list_entries()] == ["keyA"]
    finally:
        rt.close()


@pytest.mark.asyncio
async def test_endpoint_requires_auth(tmp_path) -> None:
    path = _store(tmp_path)
    rt = RouteTableManager(sqlite_path=path)
    rt.add_entry("keyA", ["umans"])
    try:
        status, _ = await _post(
            rt, {"sections": ["routes"]}, scope=_scope(authed=False)
        )
        assert status == 403
        assert [e.key for e in rt.list_entries()] == ["keyA"]
    finally:
        rt.close()
