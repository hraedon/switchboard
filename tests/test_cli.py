"""Config validation tests (WI-006.8 surface; Plan 013 additions)."""

from __future__ import annotations

import argparse
import os

import pytest

from switchboard.cli import (
    _build_serve_app,
    _ConfigError,
    _resolve,
    _resolve_float,
    _validate_config,
    _validate_config_pre_build,
)


def _base_config() -> dict:
    return {
        "provider": {
            "umans": {"upstream": "https://api.example.com"},
            "ollama-cloud": {"upstream": "https://ollama.example.com"},
        },
    }


class TestUsage24hBudgetValidation:
    def test_valid_config_passes(self) -> None:
        cfg = _base_config()
        cfg["routing"] = {"usage_24h_threshold": 0.85}
        cfg["usage_24h_budget"] = {"umans": {"cap_tokens": 300_000_000}}
        _validate_config(cfg, {"umans": None, "ollama-cloud": None})

    def test_threshold_out_of_range_rejected(self) -> None:
        cfg = _base_config()
        cfg["routing"] = {"usage_24h_threshold": 1.5}
        with pytest.raises(_ConfigError, match="usage_24h_threshold"):
            _validate_config(cfg, {"umans": None})

    def test_threshold_non_number_rejected(self) -> None:
        cfg = _base_config()
        cfg["routing"] = {"usage_24h_threshold": "high"}
        with pytest.raises(_ConfigError, match="usage_24h_threshold"):
            _validate_config(cfg, {"umans": None})

    def test_cap_tokens_non_integer_rejected(self) -> None:
        cfg = _base_config()
        cfg["usage_24h_budget"] = {"umans": {"cap_tokens": 1.5}}
        with pytest.raises(_ConfigError, match="cap_tokens"):
            _validate_config(cfg, {"umans": None})

    def test_cap_tokens_zero_rejected(self) -> None:
        cfg = _base_config()
        cfg["usage_24h_budget"] = {"umans": {"cap_tokens": 0}}
        with pytest.raises(_ConfigError, match="cap_tokens"):
            _validate_config(cfg, {"umans": None})

    def test_unknown_provider_rejected(self) -> None:
        cfg = _base_config()
        cfg["usage_24h_budget"] = {"nosuch": {"cap_tokens": 1000}}
        with pytest.raises(_ConfigError, match="unknown provider"):
            _validate_config(cfg, {"umans": None})

    def test_non_table_budget_rejected(self) -> None:
        cfg = _base_config()
        cfg["usage_24h_budget"] = {"umans": 42}
        with pytest.raises(_ConfigError, match="must be a table"):
            _validate_config(cfg, {"umans": None})


class TestOpportunisticValidation:
    def test_valid_opportunistic_config_passes(self) -> None:
        cfg = _base_config()
        cfg["routing"] = {
            "opportunistic_enabled": True,
            "opportunistic_min_headroom": 0.5,
            "opportunistic_reset_window": 21600.0,
            "opportunistic_margin": 0.10,
        }
        _validate_config(cfg, {"umans": None, "ollama-cloud": None})

    def test_min_headroom_out_of_range_rejected(self) -> None:
        cfg = _base_config()
        cfg["routing"] = {"opportunistic_min_headroom": 1.5}
        with pytest.raises(_ConfigError, match="opportunistic_min_headroom"):
            _validate_config(cfg, {"umans": None})

    def test_min_headroom_zero_rejected(self) -> None:
        cfg = _base_config()
        cfg["routing"] = {"opportunistic_min_headroom": 0.0}
        with pytest.raises(_ConfigError, match="opportunistic_min_headroom"):
            _validate_config(cfg, {"umans": None})

    def test_reset_window_non_positive_rejected(self) -> None:
        cfg = _base_config()
        cfg["routing"] = {"opportunistic_reset_window": 0.0}
        with pytest.raises(_ConfigError, match="opportunistic_reset_window"):
            _validate_config(cfg, {"umans": None})

    def test_margin_negative_rejected(self) -> None:
        cfg = _base_config()
        cfg["routing"] = {"opportunistic_margin": -0.1}
        with pytest.raises(_ConfigError, match="opportunistic_margin"):
            _validate_config(cfg, {"umans": None})

    def test_margin_one_rejected(self) -> None:
        cfg = _base_config()
        cfg["routing"] = {"opportunistic_margin": 1.0}
        with pytest.raises(_ConfigError, match="opportunistic_margin"):
            _validate_config(cfg, {"umans": None})

    def test_enabled_non_boolean_rejected(self) -> None:
        cfg = _base_config()
        cfg["routing"] = {"opportunistic_enabled": "true"}
        with pytest.raises(_ConfigError, match="opportunistic_enabled"):
            _validate_config(cfg, {"umans": None})


def test_api_key_env_missing_fails_closed(tmp_path, monkeypatch) -> None:
    """A typo in a Secret must not silently downgrade to credential passthrough.

    Falling back to "" would forward the CLIENT's key to this upstream,
    recreating the exact cross-provider leak per-provider credentials prevent.
    """
    from switchboard.providers import build_provider_contexts_from_config

    monkeypatch.delenv("SWITCHBOARD_TEST_MISSING_KEY", raising=False)
    config = {
        "provider": {
            "p": {
                "upstream": "https://example.invalid",
                "api_key_env": "SWITCHBOARD_TEST_MISSING_KEY",
            }
        }
    }
    with pytest.raises(ValueError, match="api_key_env"):
        build_provider_contexts_from_config(config)
def _serve_args(**overrides: object) -> argparse.Namespace:
    """Build the argparse Namespace `_build_serve_app` expects.

    Every serve flag defaults to None (i.e. "not supplied"), matching
    `build_parser()` so an omitted flag can never beat a config value.
    """
    defaults: dict[str, object] = {
        "command": "serve",
        "listen": None,
        "config": None,
        "admin_token": None,
        "log_level": None,
        "queue_timeout": None,
        "drain_timeout": None,
        "route_table_store": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _write_serve_config(tmp_path: pytest.TempPathFactory, body: str) -> str:
    cfg = tmp_path / "config.toml"
    cfg.write_text(body)
    return str(cfg)


_SERVE_PROVIDER = (
    "[provider.umans]\n"
    'upstream = "https://api.example.com"\n'
    "target = 1\n"
)


class TestResolvePrecedence:
    """Precedence is flag → env var → config file → built-in default."""

    def test_cli_flag_wins_over_config(self) -> None:
        args = _serve_args(listen="127.0.0.1:9999")
        assert (
            _resolve("listen", args, {"listen": "127.0.0.1:8900"})
            == "127.0.0.1:9999"
        )

    def test_env_var_wins_over_config(self) -> None:
        args = _serve_args()
        os.environ["SWITCHBOARD_LISTEN"] = "127.0.0.1:7777"
        try:
            assert (
                _resolve("listen", args, {"listen": "127.0.0.1:8900"})
                == "127.0.0.1:7777"
            )
        finally:
            del os.environ["SWITCHBOARD_LISTEN"]

    def test_config_used_when_no_flag_or_env(self) -> None:
        args = _serve_args()
        assert (
            _resolve("listen", args, {"listen": "127.0.0.1:8900"})
            == "127.0.0.1:8900"
        )

    def test_default_used_when_no_config_key(self) -> None:
        args = _serve_args()
        assert _resolve("listen", args, {}) == "127.0.0.1:8801"

    def test_default_used_when_no_config_file(self) -> None:
        args = _serve_args()
        assert _resolve("listen", args) == "127.0.0.1:8801"

    def test_resolve_float_uses_config(self) -> None:
        args = _serve_args()
        assert (
            _resolve_float("queue_timeout", args, {"queue_timeout": 12.5})
            == 12.5
        )

    def test_resolve_float_uses_default(self) -> None:
        args = _serve_args()
        assert _resolve_float("queue_timeout", args, {}) == 30.0

    def test_resolve_float_rejects_invalid_config_string(self) -> None:
        args = _serve_args()
        with pytest.raises(_ConfigError, match="queue_timeout"):
            _resolve_float("queue_timeout", args, {"queue_timeout": "abc"})

    def test_resolve_float_rejects_bool_config(self) -> None:
        args = _serve_args()
        with pytest.raises(_ConfigError, match="queue_timeout"):
            _resolve_float("queue_timeout", args, {"queue_timeout": True})

    def test_resolve_float_rejects_invalid_env(self, monkeypatch) -> None:
        args = _serve_args()
        monkeypatch.setenv("SWITCHBOARD_QUEUE_TIMEOUT", "abc")
        with pytest.raises(_ConfigError, match="queue_timeout"):
            _resolve_float("queue_timeout", args, {})


class TestServeAppPrecedence:
    """End-to-end wiring: the resolved values reach the bind parameters."""

    def test_listen_config_only(self, tmp_path) -> None:
        cfg = _write_serve_config(
            tmp_path, 'listen = "127.0.0.1:8900"\n' + _SERVE_PROVIDER
        )
        _, host, port, _, _ = _build_serve_app(_serve_args(config=cfg))
        assert (host, port) == ("127.0.0.1", 8900)

    def test_listen_cli_flag_wins_over_config(self, tmp_path) -> None:
        cfg = _write_serve_config(
            tmp_path, 'listen = "127.0.0.1:8900"\n' + _SERVE_PROVIDER
        )
        _, host, port, _, _ = _build_serve_app(
            _serve_args(config=cfg, listen="127.0.0.1:9999")
        )
        assert (host, port) == ("127.0.0.1", 9999)

    def test_listen_default_when_unconfigured(self, tmp_path) -> None:
        cfg = _write_serve_config(tmp_path, _SERVE_PROVIDER)
        _, host, port, _, _ = _build_serve_app(_serve_args(config=cfg))
        assert (host, port) == ("127.0.0.1", 8801)

    def test_queue_and_drain_timeouts_from_config(self, tmp_path) -> None:
        cfg = _write_serve_config(
            tmp_path,
            "queue_timeout = 12.5\n"
            "drain_timeout = 17.0\n"
            "log_level = \"WARNING\"\n"
            + _SERVE_PROVIDER,
        )
        app, _, _, log_level, drain_timeout = _build_serve_app(
            _serve_args(config=cfg)
        )
        assert app._queue_timeout == 12.5
        assert drain_timeout == 17.0
        assert log_level == "warning"

    def test_admin_token_from_config(self, tmp_path) -> None:
        cfg = _write_serve_config(
            tmp_path, 'admin_token = "s3cret"\n' + _SERVE_PROVIDER
        )
        app, _, _, _, _ = _build_serve_app(_serve_args(config=cfg))
        assert app._admin_token == "s3cret"


class TestServeKeyValidation:
    """Config values must be validated as strictly as the equivalent flags.

    A typo like ``log_level = "WARN"`` or ``queue_timeout = "abc"`` must
    fail startup with a switchboard error naming the key (WI-3b).
    """

    def test_valid_serve_keys_pass(self) -> None:
        cfg = _base_config()
        cfg["log_level"] = "WARNING"
        cfg["queue_timeout"] = 12.5
        cfg["drain_timeout"] = 17.0
        cfg["listen"] = "127.0.0.1:8900"
        cfg["admin_token"] = "s3cret"
        _validate_config_pre_build(cfg)

    def test_invalid_log_level_rejected(self) -> None:
        with pytest.raises(_ConfigError, match="log_level"):
            _validate_config_pre_build({"log_level": "WARN"})

    def test_non_string_log_level_rejected(self) -> None:
        with pytest.raises(_ConfigError, match="log_level"):
            _validate_config_pre_build({"log_level": 1})

    def test_string_queue_timeout_rejected(self) -> None:
        with pytest.raises(_ConfigError, match="queue_timeout"):
            _validate_config_pre_build({"queue_timeout": "abc"})

    def test_bool_queue_timeout_rejected(self) -> None:
        with pytest.raises(_ConfigError, match="queue_timeout"):
            _validate_config_pre_build({"queue_timeout": True})

    def test_string_drain_timeout_rejected(self) -> None:
        with pytest.raises(_ConfigError, match="drain_timeout"):
            _validate_config_pre_build({"drain_timeout": "abc"})

    def test_non_string_listen_rejected(self) -> None:
        with pytest.raises(_ConfigError, match="listen"):
            _validate_config_pre_build({"listen": 42})

    def test_non_string_admin_token_rejected(self) -> None:
        with pytest.raises(_ConfigError, match="admin_token"):
            _validate_config_pre_build({"admin_token": 42})


class TestServeKeyStartup:
    """The invalid values fail the serve startup path, not just validation."""

    def test_bad_log_level_fails_startup(self, tmp_path) -> None:
        cfg = _write_serve_config(
            tmp_path, 'log_level = "WARN"\n' + _SERVE_PROVIDER
        )
        with pytest.raises(_ConfigError, match="log_level"):
            _build_serve_app(_serve_args(config=cfg))

    def test_string_queue_timeout_fails_startup(self, tmp_path) -> None:
        cfg = _write_serve_config(
            tmp_path, 'queue_timeout = "abc"\n' + _SERVE_PROVIDER
        )
        with pytest.raises(_ConfigError, match="queue_timeout"):
            _build_serve_app(_serve_args(config=cfg))

    def test_bool_queue_timeout_fails_startup(self, tmp_path) -> None:
        cfg = _write_serve_config(
            tmp_path, "queue_timeout = true\n" + _SERVE_PROVIDER
        )
        with pytest.raises(_ConfigError, match="queue_timeout"):
            _build_serve_app(_serve_args(config=cfg))

    def test_valid_config_starts(self, tmp_path) -> None:
        cfg = _write_serve_config(
            tmp_path,
            "listen = \"127.0.0.1:8899\"\n"
            "queue_timeout = 12.5\n"
            "drain_timeout = 17.0\n"
            "log_level = \"WARNING\"\n"
            'admin_token = "s3cret"\n'
            + _SERVE_PROVIDER,
        )
        app, host, port, log_level, drain_timeout = _build_serve_app(
            _serve_args(config=cfg)
        )
        assert (host, port) == ("127.0.0.1", 8899)
        assert app._queue_timeout == 12.5
        assert drain_timeout == 17.0
        assert log_level == "warning"
        assert app._admin_token == "s3cret"


class TestConfigStoreBootMerge:
    """Plan 020 WI-4: the config store overlays TOML providers at boot (D1)."""

    def test_store_row_added_and_tombstone_suppresses_toml(
        self, tmp_path
    ) -> None:
        from switchboard.config_store import ConfigStoreManager

        store_file = str(tmp_path / "rt.db")
        seed = ConfigStoreManager(sqlite_path=store_file)
        seed.upsert("gui", {
            "upstream": "http://127.0.0.1:9002",
            "provider_type": "generic",
            "target": 1,
            "key_mode": "passthrough",
        })
        # Tombstone for the TOML-declared provider: enabled=0 removes it
        # from the effective set even though the file still declares it.
        seed.upsert("umans", {
            "upstream": "https://api.example.com",
            "provider_type": "generic",
            "target": 1,
            "key_mode": "passthrough",
            "enabled": 0,
        })
        seed.close()

        cfg = _write_serve_config(tmp_path, _SERVE_PROVIDER)
        app, _, _, _, _ = _build_serve_app(
            _serve_args(config=cfg, route_table_store=store_file)
        )
        assert set(app._providers) == {"gui"}
        assert app._providers["gui"].upstream_url == "http://127.0.0.1:9002"
        # The admin layer received the boot TOML identity for D1 tombstones.
        assert app._toml_provider_names == frozenset({"umans"})
        assert app._config_store.get("gui") is not None
        # The default route fell back to the EFFECTIVE set, not the TOML one.
        assert app._route_table.default_providers == ("gui",)

    def test_memory_only_store_when_no_store_path(self, tmp_path) -> None:
        cfg = _write_serve_config(tmp_path, _SERVE_PROVIDER)
        app, _, _, _, _ = _build_serve_app(_serve_args(config=cfg))
        assert set(app._providers) == {"umans"}
        assert app._config_store.db is None
        assert app._toml_provider_names == frozenset({"umans"})

    def test_store_row_replaces_toml_section_wholesale(
        self, tmp_path
    ) -> None:
        from switchboard.config_store import ConfigStoreManager

        store_file = str(tmp_path / "rt.db")
        seed = ConfigStoreManager(sqlite_path=store_file)
        seed.upsert("umans", {
            "upstream": "http://127.0.0.1:9009",
            "provider_type": "generic",
            "target": 4,
            "key_mode": "passthrough",
        })
        seed.close()

        cfg = _write_serve_config(tmp_path, _SERVE_PROVIDER)
        app, _, _, _, _ = _build_serve_app(
            _serve_args(config=cfg, route_table_store=store_file)
        )
        assert set(app._providers) == {"umans"}
        # The store row won: upstream comes from the row, not the TOML.
        assert app._providers["umans"].upstream_url == "http://127.0.0.1:9009"
