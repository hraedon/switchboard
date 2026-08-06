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
