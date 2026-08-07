"""Tests for the provider config store (Plan 020 WI-1).

The security tests pin the D2 contract with a sentinel credential: no masked
accessor, repr, or log line may ever contain it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from switchboard.config_store import ConfigStoreManager
from switchboard.providers import build_provider_contexts_from_config

SENTINEL = "sk-SENTINEL-do-not-leak-4242zzzz"


def _stored_fields(**overrides: Any) -> dict[str, object]:
    fields: dict[str, object] = {
        "upstream": "https://api.example.com",
        "provider_type": "generic",
        "target": 3,
        "key_mode": "stored",
        "api_key_stored": SENTINEL,
    }
    fields.update(overrides)
    return fields


def _env_fields(**overrides: Any) -> dict[str, object]:
    fields: dict[str, object] = {
        "upstream": "https://api.example.com",
        "provider_type": "generic",
        "target": 2,
        "key_mode": "env",
        "api_key_env": "EXAMPLE_KEY",
    }
    fields.update(overrides)
    return fields


# -- round trip ---------------------------------------------------------------


def test_upsert_get_list_remove_round_trip() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert("p1", _env_fields())

    got = mgr.get("p1")
    assert got is not None
    assert got["name"] == "p1"
    assert got["upstream"] == "https://api.example.com"
    assert got["provider_type"] == "generic"
    assert got["target"] == 2
    assert got["key_mode"] == "env"
    assert got["api_key_env"] == "EXAMPLE_KEY"
    assert got["account"] == "default"
    assert got["enabled"] is True

    assert [p["name"] for p in mgr.list_providers()] == ["p1"]
    assert mgr.remove("p1") is True
    assert mgr.get("p1") is None
    assert mgr.remove("p1") is False


def test_sqlite_path_round_trip_survives_restart(tmp_path: Path) -> None:
    db_file = str(tmp_path / "config.db")
    mgr = ConfigStoreManager(sqlite_path=db_file)
    mgr.upsert("p1", _stored_fields())
    mgr.close()

    mgr2 = ConfigStoreManager(sqlite_path=db_file)
    got = mgr2.get("p1")
    assert got is not None
    assert got["api_key_set"] is True
    assert mgr2.resolve_key("p1") == ("stored", SENTINEL, "authorization")
    mgr2.close()


def test_shared_db_connection_round_trip(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "shared.db"))
    mgr = ConfigStoreManager(db=conn)
    mgr.upsert("p1", _env_fields())

    mgr2 = ConfigStoreManager(db=conn)
    assert mgr2.get("p1") is not None
    conn.close()


def test_db_and_sqlite_path_are_mutually_exclusive(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError):
        ConfigStoreManager(db=conn, sqlite_path=str(tmp_path / "x.db"))
    conn.close()


def test_upsert_preserves_created_at_and_bumps_updated_at() -> None:
    ticks = iter([100.0, 200.0])
    mgr = ConfigStoreManager(clock=lambda: next(ticks))
    mgr.upsert("p1", _env_fields())
    mgr.upsert("p1", _env_fields(target=5))
    got = mgr.get("p1")
    assert got is not None
    assert got["created_at"] == 100.0
    assert got["updated_at"] == 200.0
    assert got["target"] == 5


# -- masking (security) --------------------------------------------------------


def test_masked_accessors_never_contain_credential() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert("p1", _stored_fields())

    got = mgr.get("p1")
    assert got is not None
    assert "api_key_stored" not in got
    assert SENTINEL not in json.dumps(got)
    assert got["api_key_set"] is True
    assert got["api_key_hint"] == SENTINEL[-4:]

    assert SENTINEL not in json.dumps(mgr.list_providers())


def test_unset_key_masks_to_false_and_empty_hint() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert("p1", _env_fields())
    got = mgr.get("p1")
    assert got is not None
    assert got["api_key_set"] is False
    assert got["api_key_hint"] == ""


def test_repr_and_str_never_contain_credential() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert("p1", _stored_fields())
    assert SENTINEL not in repr(mgr)
    assert SENTINEL not in str(mgr)
    assert "p1" in repr(mgr)


def test_log_lines_never_contain_credential(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    with caplog.at_level(logging.DEBUG, logger="switchboard.config_store"):
        db_file = str(tmp_path / "config.db")
        mgr = ConfigStoreManager(sqlite_path=db_file)
        mgr.upsert("p1", _stored_fields())
        mgr.remove("p1")
        mgr.upsert("p2", _stored_fields())
        mgr.close()
        ConfigStoreManager(sqlite_path=db_file).close()
    assert SENTINEL not in caplog.text


# -- key semantics --------------------------------------------------------------


def test_key_absent_on_edit_keeps_stored_key() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert("p1", _stored_fields())
    # GUI edit round-trips everything except the credential.
    edit = _stored_fields(target=9)
    del edit["api_key_stored"]
    mgr.upsert("p1", edit)
    assert mgr.resolve_key("p1") == ("stored", SENTINEL, "authorization")
    got = mgr.get("p1")
    assert got is not None
    assert got["target"] == 9

    # Explicit None means the same as absent.
    mgr.upsert("p1", _stored_fields(api_key_stored=None))
    assert mgr.resolve_key("p1") == ("stored", SENTINEL, "authorization")


def test_key_present_on_edit_overwrites_stored_key() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert("p1", _stored_fields())
    mgr.upsert("p1", _stored_fields(api_key_stored="sk-new-key-abcd"))
    assert mgr.resolve_key("p1") == (
        "stored",
        "sk-new-key-abcd",
        "authorization",
    )


def test_resolve_key_env_and_passthrough_modes() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert("p-env", _env_fields(auth_header="x-api-key"))
    mgr.upsert(
        "p-pass",
        {
            "upstream": "https://api.example.com",
            "provider_type": "generic",
            "target": 1,
            "key_mode": "passthrough",
        },
    )
    assert mgr.resolve_key("p-env") == ("env", "EXAMPLE_KEY", "x-api-key")
    assert mgr.resolve_key("p-pass") == ("passthrough", "", "authorization")
    assert mgr.resolve_key("absent") is None


# -- validation ------------------------------------------------------------------


def test_unknown_provider_type_rejected() -> None:
    mgr = ConfigStoreManager()
    with pytest.raises(ValueError, match="unknown provider"):
        mgr.upsert("p1", _env_fields(provider_type="no-such-type"))
    assert mgr.get("p1") is None


def test_target_below_one_rejected() -> None:
    mgr = ConfigStoreManager()
    with pytest.raises(ValueError, match="target"):
        mgr.upsert("p1", _env_fields(target=0))


def test_env_mode_without_env_name_rejected() -> None:
    mgr = ConfigStoreManager()
    fields = _env_fields()
    del fields["api_key_env"]
    with pytest.raises(ValueError, match="api_key_env"):
        mgr.upsert("p1", fields)
    with pytest.raises(ValueError, match="api_key_env"):
        mgr.upsert("p1", _env_fields(api_key_env=""))


def test_stored_mode_without_key_rejected_for_new_provider() -> None:
    mgr = ConfigStoreManager()
    fields = _stored_fields()
    del fields["api_key_stored"]
    with pytest.raises(ValueError, match="api_key_stored"):
        mgr.upsert("p1", fields)


def test_bad_key_mode_rejected() -> None:
    mgr = ConfigStoreManager()
    with pytest.raises(ValueError, match="key_mode"):
        mgr.upsert("p1", _env_fields(key_mode="vault"))


def test_missing_upstream_rejected() -> None:
    mgr = ConfigStoreManager()
    fields = _env_fields()
    del fields["upstream"]
    with pytest.raises(ValueError, match="upstream"):
        mgr.upsert("p1", fields)


def test_db_failure_leaves_memory_unchanged(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "shared.db"))
    mgr = ConfigStoreManager(db=conn)
    mgr.upsert("p1", _env_fields())
    conn.execute("DROP TABLE provider_config")
    conn.commit()
    with pytest.raises(sqlite3.Error):
        mgr.upsert("p2", _env_fields())
    assert mgr.get("p2") is None
    assert mgr.get("p1") is not None
    conn.close()


# -- merge precedence --------------------------------------------------------------


def _toml_config() -> dict[str, object]:
    return {
        "provider": {
            "toml-only": {
                "upstream": "https://toml-only.example.com",
                "type": "generic",
                "target": 1,
            },
            "overridden": {
                "upstream": "https://old.example.com",
                "type": "generic",
                "target": 1,
                "usage_key_env": "OLD_USAGE_KEY",
            },
            "disabled-by-store": {
                "upstream": "https://gone.example.com",
                "type": "generic",
                "target": 1,
            },
        }
    }


def test_effective_providers_merge_precedence() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert(
        "overridden",
        _env_fields(upstream="https://new.example.com", target=7),
    )
    mgr.upsert("store-only", _env_fields())
    mgr.upsert("disabled-by-store", _env_fields(enabled=False))

    effective = mgr.effective_providers(_toml_config())

    # TOML-only provider passes through untouched.
    assert effective["toml-only"]["upstream"] == "https://toml-only.example.com"
    # Store row replaces the TOML table WHOLESALE — no field merging: the
    # TOML-only usage_key_env must not survive into the effective section.
    assert effective["overridden"]["upstream"] == "https://new.example.com"
    assert effective["overridden"]["target"] == 7
    assert "usage_key_env" not in effective["overridden"]
    # Store-only provider is added; disabled row removes the TOML provider.
    assert "store-only" in effective
    assert "disabled-by-store" not in effective


def test_effective_providers_with_empty_toml() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert("p1", _env_fields())
    effective = mgr.effective_providers({})
    assert set(effective) == {"p1"}


# -- to_provider_section shape --------------------------------------------------


def test_to_provider_section_env_mode_shape() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert(
        "p1",
        _env_fields(
            auth_header="x-api-key",
            auth_prefix="",
            dashboard_url="http://dash.local/readings",
            dashboard_token_env="DASH_TOKEN",
        ),
    )
    section = mgr.to_provider_section("p1")
    assert section == {
        "upstream": "https://api.example.com",
        "type": "generic",
        "target": 2,
        "api_key_env": "EXAMPLE_KEY",
        "auth_header": "x-api-key",
        "auth_prefix": "",
        "dashboard_url": "http://dash.local/readings",
        "dashboard_token_env": "DASH_TOKEN",
    }


def test_to_provider_section_stored_mode_uses_inline_api_key() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert("p1", _stored_fields())
    section = mgr.to_provider_section("p1")
    assert section["api_key"] == SENTINEL
    assert "api_key_env" not in section


def test_to_provider_section_passthrough_has_no_key_fields() -> None:
    mgr = ConfigStoreManager()
    mgr.upsert(
        "p1",
        {
            "upstream": "https://api.example.com",
            "provider_type": "generic",
            "target": 1,
            "key_mode": "passthrough",
        },
    )
    section = mgr.to_provider_section("p1")
    assert "api_key" not in section
    assert "api_key_env" not in section


def test_to_provider_section_unknown_name_raises_key_error() -> None:
    mgr = ConfigStoreManager()
    with pytest.raises(KeyError):
        mgr.to_provider_section("absent")


# -- integration: merged sections drive the existing construction path ----------


def test_effective_sections_drive_build_provider_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXAMPLE_KEY", "env-resolved-key")
    mgr = ConfigStoreManager()
    mgr.upsert("stored-prov", _stored_fields(auth_header="x-api-key"))
    mgr.upsert("env-prov", _env_fields())

    merged = mgr.effective_providers(_toml_config())
    contexts = build_provider_contexts_from_config({"provider": merged})

    assert set(contexts) >= {"stored-prov", "env-prov", "toml-only"}
    stored_ctx = contexts["stored-prov"]
    assert stored_ctx.upstream_url == "https://api.example.com"
    assert stored_ctx.api_key == SENTINEL
    assert stored_ctx.auth_header == "x-api-key"
    assert stored_ctx.auth_prefix == ""  # derived for non-authorization header
    env_ctx = contexts["env-prov"]
    assert env_ctx.api_key == "env-resolved-key"
    assert env_ctx.auth_prefix == "Bearer "


# -- fail-safe loading ------------------------------------------------------------


def test_corrupt_row_is_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_file = str(tmp_path / "config.db")
    seed = ConfigStoreManager(sqlite_path=db_file)
    seed.upsert("good", _env_fields())
    # Bypass validation: plant a row with an unknown provider_type and a
    # sentinel credential — the load must skip it without echoing the value.
    assert seed.db is not None
    seed.db.execute(
        "INSERT INTO provider_config (name, account, upstream, provider_type,"
        " target, key_mode, api_key_env, api_key_stored, enabled, created_at,"
        " updated_at) VALUES ('bad', 'default', 'https://x', 'no-such-type',"
        " 1, 'stored', NULL, ?, 1, 0, 0)",
        (SENTINEL,),
    )
    seed.db.commit()
    seed.close()

    with caplog.at_level(logging.WARNING, logger="switchboard.config_store"):
        mgr = ConfigStoreManager(sqlite_path=db_file)
    assert mgr.get("good") is not None
    assert mgr.get("bad") is None
    assert "bad" in caplog.text
    assert SENTINEL not in caplog.text
    mgr.close()


def test_whole_table_failure_yields_empty_store(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_file = str(tmp_path / "config.db")
    conn = sqlite3.connect(db_file)
    # Pre-existing table with the wrong shape: CREATE IF NOT EXISTS is a
    # no-op and the load SELECT fails outright.
    conn.execute("CREATE TABLE provider_config (x TEXT)")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING, logger="switchboard.config_store"):
        mgr = ConfigStoreManager(sqlite_path=db_file)
    assert mgr.list_providers() == []
    assert "empty store" in caplog.text
    mgr.close()


def test_sqlite_file_is_chmodded_0600(tmp_path: Path) -> None:
    db_file = tmp_path / "config.db"
    mgr = ConfigStoreManager(sqlite_path=str(db_file))
    assert (db_file.stat().st_mode & 0o777) == 0o600
    mgr.close()


def test_usage_key_env_round_trips_and_reaches_section(tmp_path) -> None:
    """usage_key_env survives store round-trip and lands in the TOML-shaped
    section so a store-managed umans provider keeps its usage-history key
    (WI-1 review finding 4)."""
    mgr = ConfigStoreManager(sqlite_path=str(tmp_path / "cfg.sqlite"))
    mgr.upsert(
        "umans-a",
        {
            "upstream": "https://api.code.umans.ai",
            "provider_type": "umans",
            "target": 3,
            "key_mode": "env",
            "api_key_env": "UMANS_KEY",
            "usage_key_env": "UMANS_USAGE_KEY",
        },
    )
    assert mgr.get("umans-a")["usage_key_env"] == "UMANS_USAGE_KEY"
    section = mgr.to_provider_section("umans-a")
    assert section["usage_key_env"] == "UMANS_USAGE_KEY"
    mgr.close()

    reloaded = ConfigStoreManager(sqlite_path=str(tmp_path / "cfg.sqlite"))
    assert reloaded.get("umans-a")["usage_key_env"] == "UMANS_USAGE_KEY"
    reloaded.close()
