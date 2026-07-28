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
