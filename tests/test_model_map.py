from __future__ import annotations

import os
import sqlite3
import tempfile

from switchboard.control import ModelMap
from switchboard.model_map import ModelMapManager


def test_empty_manager_returns_feature_off_map() -> None:
    mgr = ModelMapManager()
    mm = mgr.get_model_map()
    assert isinstance(mm, ModelMap)
    assert mm.routes == {}


def test_set_model_then_get_model_map_returns_frozen_snapshot() -> None:
    mgr = ModelMapManager()
    mgr.set_model(
        "umans-kimi-k2.7",
        {"umans": "umans-kimi-k2.7", "ollama-cloud": "kimi-k2.7-code"},
    )
    mm = mgr.get_model_map()
    assert "umans-kimi-k2.7" in mm
    assert mm.providers_for("umans-kimi-k2.7") == frozenset(
        {"umans", "ollama-cloud"}
    )
    assert mm.alias_for("umans-kimi-k2.7", "ollama-cloud") == "kimi-k2.7-code"


def test_set_model_replaces_existing_aliases() -> None:
    mgr = ModelMapManager()
    mgr.set_model("m", {"umans": "a1"})
    mgr.set_model("m", {"ollama-cloud": "a2"})
    mm = mgr.get_model_map()
    assert mm.providers_for("m") == frozenset({"ollama-cloud"})
    assert mm.alias_for("m", "umans") is None


def test_list_models_returns_deep_copy() -> None:
    mgr = ModelMapManager()
    mgr.set_model("m", {"umans": "a1"})
    listed = dict(mgr.list_models())
    listed["m"]["umans"] = "mutated"
    # Manager state is untouched by mutation of the returned copy.
    assert mgr.get_model_map().alias_for("m", "umans") == "a1"


def test_remove_model_returns_true_and_drops_entry() -> None:
    mgr = ModelMapManager()
    mgr.set_model("m", {"umans": "a1"})
    assert mgr.remove_model("m") is True
    assert "m" not in mgr.get_model_map()


def test_remove_model_returns_false_for_unknown() -> None:
    mgr = ModelMapManager()
    assert mgr.remove_model("nope") is False


def test_load_from_config_seeds_models() -> None:
    config = {
        "model": {
            "kimi": {"umans": "umans-kimi", "ollama-cloud": "kimi-ollama"},
        }
    }
    mgr = ModelMapManager()
    mgr.load_from_config(config)
    mm = mgr.get_model_map()
    assert mm.alias_for("kimi", "umans") == "umans-kimi"
    assert mm.alias_for("kimi", "ollama-cloud") == "kimi-ollama"


def test_load_from_config_preserves_persisted_entries() -> None:
    """Same shape as RouteTableManager: file seeds only absent models."""
    config = {"model": {"kimi": {"umans": "from-config"}}}
    mgr = ModelMapManager()
    mgr.set_model("kimi", {"umans": "runtime"})
    mgr.load_from_config(config)
    assert mgr.get_model_map().alias_for("kimi", "umans") == "runtime"


def test_load_from_config_overwrite_true_overrides() -> None:
    config = {"model": {"kimi": {"umans": "from-config"}}}
    mgr = ModelMapManager()
    mgr.set_model("kimi", {"umans": "runtime"})
    mgr.load_from_config(config, overwrite=True)
    assert mgr.get_model_map().alias_for("kimi", "umans") == "from-config"


def test_load_from_config_skips_malformed_entries() -> None:
    config = {
        "model": {
            "good": {"umans": "ok"},
            "bad-not-dict": "nope",
            "empty": {},
        }
    }
    mgr = ModelMapManager()
    mgr.load_from_config(config)
    mm = mgr.get_model_map()
    assert "good" in mm
    assert "bad-not-dict" not in mm
    assert "empty" not in mm


# --- SQLite persistence (shares a connection, like the route-table store) ---


def _tmp_db() -> tuple[str, int]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path, fd


def test_sqlite_persistence_set_model_survives_new_manager() -> None:
    path, _ = _tmp_db()
    try:
        db1 = sqlite3.connect(path)
        mgr1 = ModelMapManager(db=db1)
        mgr1.set_model(
            "persisted", {"umans": "u-alias", "ollama-cloud": "o-alias"}
        )

        db2 = sqlite3.connect(path)
        mgr2 = ModelMapManager(db=db2)
        mm = mgr2.get_model_map()
        assert mm.alias_for("persisted", "umans") == "u-alias"
        assert mm.alias_for("persisted", "ollama-cloud") == "o-alias"
        db1.close()
        db2.close()
    finally:
        os.unlink(path)


def test_sqlite_persistence_remove_model_gone_in_new_manager() -> None:
    path, _ = _tmp_db()
    try:
        db1 = sqlite3.connect(path)
        mgr1 = ModelMapManager(db=db1)
        mgr1.set_model("doomed", {"umans": "u"})
        assert mgr1.remove_model("doomed") is True

        db2 = sqlite3.connect(path)
        mgr2 = ModelMapManager(db=db2)
        assert "doomed" not in mgr2.get_model_map()
        db1.close()
        db2.close()
    finally:
        os.unlink(path)


def test_sqlite_persistence_set_replaces_row() -> None:
    path, _ = _tmp_db()
    try:
        db1 = sqlite3.connect(path)
        mgr1 = ModelMapManager(db=db1)
        mgr1.set_model("m", {"umans": "v1"})
        mgr1.set_model("m", {"umans": "v2"})

        db2 = sqlite3.connect(path)
        mgr2 = ModelMapManager(db=db2)
        assert mgr2.get_model_map().alias_for("m", "umans") == "v2"
        db1.close()
        db2.close()
    finally:
        os.unlink(path)
