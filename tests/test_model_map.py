from __future__ import annotations

import logging
import os
import sqlite3
import tempfile

import pytest

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


def test_remove_provider_scrubs_aliases_preferences_and_empty_entries() -> None:
    mgr = ModelMapManager()
    mgr.set_model("shared", {"gone": "g", "kept": "k"}, ["gone", "kept"])
    mgr.set_model("gone-only", {"gone": "g"}, ["gone"])
    mgr.set_model("untouched", {"kept": "k"}, ["kept"])

    assert mgr.remove_provider("gone") == (1, 1)

    assert dict(mgr.list_models()) == {
        "shared": {"kept": "k"},
        "untouched": {"kept": "k"},
    }
    assert mgr.preference_for("shared") == ("kept",)
    assert mgr.preference_for("untouched") == ("kept",)


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


def test_sqlite_persistence_remove_provider_survives_new_manager() -> None:
    path, _ = _tmp_db()
    try:
        db1 = sqlite3.connect(path)
        mgr1 = ModelMapManager(db=db1)
        mgr1.set_model(
            "shared", {"gone": "g", "kept": "k"}, ["gone", "kept"]
        )
        mgr1.set_model("gone-only", {"gone": "g"}, ["gone"])

        assert mgr1.remove_provider("gone") == (1, 1)

        db2 = sqlite3.connect(path)
        mgr2 = ModelMapManager(db=db2)
        assert dict(mgr2.list_models()) == {"shared": {"kept": "k"}}
        assert mgr2.preference_for("shared") == ("kept",)
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


# --- WI-12b: DB-first write ordering ---------------------------------------


def test_set_model_db_failure_raises_and_leaves_memory_unchanged() -> None:
    """A failed DB write must not leave memory claiming a phantom save."""
    db = sqlite3.connect(":memory:")
    mgr = ModelMapManager(db=db)
    mgr.set_model("m", {"umans": "v1"})
    db.close()  # every subsequent execute() raises sqlite3.ProgrammingError

    with pytest.raises(sqlite3.Error):
        mgr.set_model("m", {"umans": "v2"})
    # Memory still holds the last successfully persisted value.
    assert mgr.get_model_map().alias_for("m", "umans") == "v1"

    with pytest.raises(sqlite3.Error):
        mgr.set_model("brand-new", {"umans": "x"})
    assert "brand-new" not in mgr.get_model_map()


def test_remove_model_db_failure_raises_and_keeps_entry() -> None:
    db = sqlite3.connect(":memory:")
    mgr = ModelMapManager(db=db)
    mgr.set_model("m", {"umans": "v1"})
    db.close()

    with pytest.raises(sqlite3.Error):
        mgr.remove_model("m")
    assert mgr.get_model_map().alias_for("m", "umans") == "v1"


# --- WI-12b: distrust the store at load ------------------------------------


def _seed_raw_row(db: sqlite3.Connection, model: str, aliases_json: str) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS model_map "
        "(model TEXT PRIMARY KEY, aliases TEXT)"
    )
    db.execute(
        "INSERT OR REPLACE INTO model_map (model, aliases) VALUES (?, ?)",
        (model, aliases_json),
    )
    db.commit()


def test_load_skips_corrupt_json_row_and_keeps_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = sqlite3.connect(":memory:")
    _seed_raw_row(db, "good", '{"umans": "ok"}')
    _seed_raw_row(db, "corrupt", "{not json")

    with caplog.at_level(logging.WARNING, logger="switchboard.model_map"):
        mgr = ModelMapManager(db=db)  # must not raise

    mm = mgr.get_model_map()
    assert mm.alias_for("good", "umans") == "ok"
    assert "corrupt" not in mm
    assert any("corrupt" in r.message for r in caplog.records)
    # The bad row is left in the DB for forensics, not deleted.
    count = db.execute(
        "SELECT COUNT(*) FROM model_map WHERE model = 'corrupt'"
    ).fetchone()[0]
    assert count == 1
    db.close()


def test_load_skips_non_object_json_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = sqlite3.connect(":memory:")
    _seed_raw_row(db, "listy", '["not", "a", "dict"]')

    with caplog.at_level(logging.WARNING, logger="switchboard.model_map"):
        mgr = ModelMapManager(db=db)

    assert "listy" not in mgr.get_model_map()
    assert any("listy" in r.message for r in caplog.records)
    db.close()


def test_valid_providers_skips_fully_unknown_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = sqlite3.connect(":memory:")
    _seed_raw_row(db, "stale", '{"gone-prov": "x", "also-gone": "y"}')

    with caplog.at_level(logging.WARNING, logger="switchboard.model_map"):
        mgr = ModelMapManager(db=db, valid_providers=frozenset({"umans"}))

    assert "stale" not in mgr.get_model_map()
    assert any("stale" in r.message for r in caplog.records)
    db.close()


def test_valid_providers_loads_known_subset_of_mixed_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = sqlite3.connect(":memory:")
    _seed_raw_row(db, "mixed", '{"umans": "ok", "gone-prov": "x"}')

    with caplog.at_level(logging.WARNING, logger="switchboard.model_map"):
        mgr = ModelMapManager(db=db, valid_providers=frozenset({"umans"}))

    mm = mgr.get_model_map()
    assert mm.alias_for("mixed", "umans") == "ok"
    assert mm.providers_for("mixed") == frozenset({"umans"})
    assert any("gone-prov" in r.message for r in caplog.records)
    db.close()


def test_valid_providers_none_disables_validation() -> None:
    db = sqlite3.connect(":memory:")
    _seed_raw_row(db, "stale", '{"gone-prov": "x"}')

    mgr = ModelMapManager(db=db)  # None: prior behavior, load everything
    assert mgr.get_model_map().alias_for("stale", "gone-prov") == "x"
    db.close()


# --- Plan 026 W3.1/W3.2: per-model provider preference ----------------------


def test_set_model_stores_and_exposes_a_preference() -> None:
    mgr = ModelMapManager()
    mgr.set_model("m", {"umans": "a", "zai": "b"}, ["zai", "umans"])
    assert mgr.preference_for("m") == ("zai", "umans")
    assert mgr.list_preferences() == {"m": ["zai", "umans"]}
    assert mgr.get_model_map().preference_for("m") == ("zai", "umans")


def test_preference_absent_is_an_empty_tuple() -> None:
    mgr = ModelMapManager()
    mgr.set_model("m", {"umans": "a"})
    assert mgr.preference_for("m") == ()
    assert mgr.get_model_map().preference_for("m") == ()
    # And for a model that is not mapped at all.
    assert mgr.get_model_map().preference_for("nope") == ()
    assert mgr.list_preferences() == {}


def test_empty_preference_normalizes_to_none() -> None:
    mgr = ModelMapManager()
    mgr.set_model("m", {"umans": "a"}, [])
    assert mgr.preference_for("m") == ()


def test_set_model_clears_a_preference_on_replacement() -> None:
    """POST replaces the whole entry, so omitting the preference clears it."""
    mgr = ModelMapManager()
    mgr.set_model("m", {"umans": "a", "zai": "b"}, ["zai"])
    mgr.set_model("m", {"umans": "a", "zai": "b"})
    assert mgr.preference_for("m") == ()


def test_remove_model_drops_its_preference() -> None:
    mgr = ModelMapManager()
    mgr.set_model("m", {"umans": "a"}, ["umans"])
    assert mgr.remove_model("m") is True
    assert mgr.preference_for("m") == ()


def test_preference_naming_an_alias_less_provider_is_refused() -> None:
    """Dead config at best — the model map reorders, it never adds."""
    mgr = ModelMapManager()
    with pytest.raises(ValueError, match="ghost"):
        mgr.set_model("m", {"umans": "a"}, ["umans", "ghost"])
    # Nothing was written.
    assert "m" not in mgr.get_model_map()


def test_preference_with_duplicates_is_refused() -> None:
    mgr = ModelMapManager()
    with pytest.raises(ValueError, match="repeats"):
        mgr.set_model("m", {"umans": "a", "zai": "b"}, ["zai", "zai"])


def test_preference_survives_a_new_manager_over_sqlite() -> None:
    path, _ = _tmp_db()
    try:
        db1 = sqlite3.connect(path)
        mgr1 = ModelMapManager(db=db1)
        mgr1.set_model("m", {"umans": "a", "zai": "b"}, ["zai", "umans"])

        db2 = sqlite3.connect(path)
        mgr2 = ModelMapManager(db=db2)
        assert mgr2.preference_for("m") == ("zai", "umans")
        # Clearing it persists too.
        mgr2.set_model("m", {"umans": "a", "zai": "b"})
        db3 = sqlite3.connect(path)
        assert ModelMapManager(db=db3).preference_for("m") == ()
        db1.close()
        db2.close()
        db3.close()
    finally:
        os.unlink(path)


def test_migration_adds_preference_to_a_pre026_db() -> None:
    """A model_map table created before Plan 026 gains the column on open,
    keeping its rows intact (mirrors the Plan 025 config-store migration)."""
    path, _ = _tmp_db()
    try:
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE model_map (model TEXT PRIMARY KEY, aliases TEXT)"
        )
        db.execute(
            "INSERT INTO model_map (model, aliases) VALUES (?, ?)",
            ("old", '{"umans": "u-alias"}'),
        )
        db.commit()
        db.close()

        db2 = sqlite3.connect(path)
        mgr = ModelMapManager(db=db2)
        columns = {
            r[1] for r in db2.execute("PRAGMA table_info(model_map)")
        }
        assert "preference" in columns
        assert mgr.get_model_map().alias_for("old", "umans") == "u-alias"
        assert mgr.preference_for("old") == ()
        # The migrated file accepts the new field on the next write.
        mgr.set_model("old", {"umans": "u-alias"}, ["umans"])
        db3 = sqlite3.connect(path)
        assert ModelMapManager(db=db3).preference_for("old") == ("umans",)
        db2.close()
        db3.close()
    finally:
        os.unlink(path)


def test_migration_is_idempotent() -> None:
    path, _ = _tmp_db()
    try:
        for _ in range(3):
            db = sqlite3.connect(path)
            ModelMapManager(db=db)  # must not raise on an already-migrated file
            db.close()
        db = sqlite3.connect(path)
        columns = [
            r[1] for r in db.execute("PRAGMA table_info(model_map)")
        ]
        assert columns.count("preference") == 1
        db.close()
    finally:
        os.unlink(path)


def _seed_raw_row_with_preference(
    db: sqlite3.Connection, model: str, aliases_json: str, preference: str | None
) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS model_map "
        "(model TEXT PRIMARY KEY, aliases TEXT, preference TEXT)"
    )
    db.execute(
        "INSERT OR REPLACE INTO model_map (model, aliases, preference) "
        "VALUES (?, ?, ?)",
        (model, aliases_json, preference),
    )
    db.commit()


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",              # corrupt
        '{"umans": 0}',           # not a list
        '["umans", ""]',          # empty name
        '["umans", 7]',           # non-string entry
        '["umans", "umans"]',     # duplicate
    ],
)
def test_load_tolerates_a_malformed_preference(
    raw: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed preference costs the row nothing: it loads with no
    preference and a warning, never a crash."""
    db = sqlite3.connect(":memory:")
    _seed_raw_row_with_preference(db, "m", '{"umans": "ok"}', raw)

    with caplog.at_level(logging.WARNING, logger="switchboard.model_map"):
        mgr = ModelMapManager(db=db)

    assert mgr.get_model_map().alias_for("m", "umans") == "ok"
    assert mgr.preference_for("m") == ()
    assert any("m" in r.message for r in caplog.records)
    db.close()


def test_load_drops_preference_names_without_an_alias(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider dropped by valid_providers cannot stay in the preference —
    it holds no alias, so it could never rank anything."""
    db = sqlite3.connect(":memory:")
    _seed_raw_row_with_preference(
        db, "m", '{"umans": "ok", "gone-prov": "x"}', '["gone-prov", "umans"]',
    )

    with caplog.at_level(logging.WARNING, logger="switchboard.model_map"):
        mgr = ModelMapManager(db=db, valid_providers=frozenset({"umans"}))

    assert mgr.preference_for("m") == ("umans",)
    assert any("gone-prov" in r.message for r in caplog.records)
    db.close()


def test_load_preference_of_only_unknown_providers_is_unset(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = sqlite3.connect(":memory:")
    _seed_raw_row_with_preference(
        db, "m", '{"umans": "ok", "gone-prov": "x"}', '["gone-prov"]',
    )

    with caplog.at_level(logging.WARNING, logger="switchboard.model_map"):
        mgr = ModelMapManager(db=db, valid_providers=frozenset({"umans"}))

    assert mgr.preference_for("m") == ()
    db.close()


def test_load_null_preference_is_unset() -> None:
    db = sqlite3.connect(":memory:")
    _seed_raw_row_with_preference(db, "m", '{"umans": "ok"}', None)
    mgr = ModelMapManager(db=db)
    assert mgr.preference_for("m") == ()
    db.close()


def test_config_overwrite_carries_a_still_valid_preference() -> None:
    """TOML has no preference shape (Plan 026 W3.1), so a config overwrite must
    not silently discard one; names that lost their alias are dropped."""
    mgr = ModelMapManager()
    mgr.set_model("kimi", {"umans": "u", "zai": "z"}, ["zai", "umans"])
    mgr.load_from_config(
        {"model": {"kimi": {"umans": "from-config"}}}, overwrite=True,
    )
    assert mgr.get_model_map().alias_for("kimi", "umans") == "from-config"
    assert mgr.preference_for("kimi") == ("umans",)
