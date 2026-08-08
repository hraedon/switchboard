from __future__ import annotations

import os
import tempfile

from switchboard.route_table import RouteTableManager


def test_lookup_returns_default_when_key_not_in_table() -> None:
    mgr = RouteTableManager(default_providers=("umans", "ollama"))
    assert mgr.lookup("unknown") == ("umans", "ollama")


def test_lookup_returns_entry_providers_when_key_matches() -> None:
    mgr = RouteTableManager(default_providers=("umans", "ollama"))
    mgr.add_entry("abc123", ["ollama", "umans"])
    assert mgr.lookup("abc123") == ("ollama", "umans")


def test_get_entry_distinguishes_keyed_match_from_default() -> None:
    """get_entry returns None on a keyed miss (unlike lookup, which returns
    the default). HMAC rotation needs that distinction to try multiple hash
    candidates before settling for the default route."""
    mgr = RouteTableManager(default_providers=("umans", "ollama"))
    mgr.add_entry("abc123", ["ollama", "umans"])
    assert mgr.get_entry("abc123") == ("ollama", "umans")
    assert mgr.get_entry("not-a-keyed-entry") is None


def test_get_entry_with_no_default_still_none_on_miss() -> None:
    mgr = RouteTableManager()
    assert mgr.get_entry("anything") is None


def test_add_entry_adds_and_persists_in_memory() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    mgr.add_entry("key1", ["umans", "ollama"])
    assert mgr.lookup("key1") == ("umans", "ollama")


def test_add_entry_updates_existing_entry() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    mgr.add_entry("key1", ["umans"])
    mgr.add_entry("key1", ["ollama", "umans"])
    assert mgr.lookup("key1") == ("ollama", "umans")


def test_remove_entry_removes_and_returns_true() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    mgr.add_entry("key1", ["umans"])
    assert mgr.remove_entry("key1") is True
    assert mgr.lookup("key1") == ("umans",)


def test_remove_entry_returns_false_for_unknown_key() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    assert mgr.remove_entry("nonexistent") is False


def test_list_entries_returns_all_entries() -> None:
    mgr = RouteTableManager(default_providers=("umans",))
    mgr.add_entry("key1", ["umans"])
    mgr.add_entry("key2", ["ollama"])
    entries = mgr.list_entries()
    keys = {e.key for e in entries}
    assert keys == {"key1", "key2"}


def test_get_route_table_returns_frozen_snapshot() -> None:
    mgr = RouteTableManager(default_providers=("umans", "ollama"))
    mgr.add_entry("key1", ["umans"])
    table = mgr.get_route_table()
    assert table.default_providers == ("umans", "ollama")
    assert "key1" in table.entries
    assert table.entries["key1"].providers == ("umans",)
    table.entries["key2"] = table.entries["key1"]
    assert "key2" not in mgr.get_route_table().entries


def test_load_from_config_loads_from_parsed_toml_dict() -> None:
    config = {
        "route": {
            "default": {"providers": ["umans", "ollama"]},
            "key_abc123": {"providers": ["ollama", "umans"]},
        }
    }
    mgr = RouteTableManager()
    mgr.load_from_config(config)
    assert mgr.lookup("nonexistent") == ("umans", "ollama")
    assert mgr.lookup("key_abc123") == ("ollama", "umans")


def test_load_from_config_preserves_persisted_entries() -> None:
    """WI-006.7: file entries seed only absent keys by default."""
    config = {
        "route": {
            "default": {"providers": ["umans", "ollama"]},
            "key_abc123": {"providers": ["ollama", "umans"]},
        }
    }
    mgr = RouteTableManager()
    mgr.add_entry("key_abc123", ["umans"])
    mgr.load_from_config(config)
    # Persisted entry should be preserved, not overwritten.
    assert mgr.lookup("key_abc123") == ("umans",)


def test_load_from_config_overwrite_true_overrides() -> None:
    """WI-006.7: overwrite=True lets file entries override persisted."""
    config = {
        "route": {
            "default": {"providers": ["umans", "ollama"]},
            "key_abc123": {"providers": ["ollama", "umans"]},
        }
    }
    mgr = RouteTableManager()
    mgr.add_entry("key_abc123", ["umans"])
    mgr.load_from_config(config, overwrite=True)
    assert mgr.lookup("key_abc123") == ("ollama", "umans")


def test_sqlite_persistence_add_entry_survives_new_manager() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        mgr1 = RouteTableManager(
            default_providers=("umans",),
            sqlite_path=path,
        )
        mgr1.add_entry("persisted_key", ["umans", "ollama"])
        mgr1.close()

        mgr2 = RouteTableManager(
            default_providers=("umans",),
            sqlite_path=path,
        )
        assert mgr2.lookup("persisted_key") == ("umans", "ollama")
        mgr2.close()
    finally:
        os.unlink(path)


def test_sqlite_persistence_remove_entry_gone_in_new_manager() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        mgr1 = RouteTableManager(
            default_providers=("umans",),
            sqlite_path=path,
        )
        mgr1.add_entry("to_remove", ["umans"])
        mgr1.remove_entry("to_remove")
        mgr1.close()

        mgr2 = RouteTableManager(
            default_providers=("umans",),
            sqlite_path=path,
        )
        assert mgr2.lookup("to_remove") == ("umans",)
        assert len(mgr2.list_entries()) == 0
        mgr2.close()
    finally:
        os.unlink(path)
