"""Config validation tests (WI-006.8 surface; Plan 013 additions)."""

from __future__ import annotations

import pytest

from switchboard.cli import _ConfigError, _validate_config


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
