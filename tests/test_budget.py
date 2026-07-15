"""Unit tests for the pure token-budget core (Plan 012 §4.3)."""

from __future__ import annotations

import pytest

from switchboard.budget import (
    TokenBudgetConfig,
    compute_utilization,
    is_over_budget,
    project_utilization,
)


class TestTokenBudgetConfig:
    def test_defaults(self) -> None:
        cfg = TokenBudgetConfig(cap_tokens=1_000_000)
        assert cfg.window_seconds == 3600.0
        assert cfg.soft_threshold == 0.85

    def test_zero_cap_rejected(self) -> None:
        with pytest.raises(ValueError, match="cap_tokens"):
            TokenBudgetConfig(cap_tokens=0)

    def test_negative_cap_rejected(self) -> None:
        with pytest.raises(ValueError, match="cap_tokens"):
            TokenBudgetConfig(cap_tokens=-1)

    def test_zero_window_rejected(self) -> None:
        with pytest.raises(ValueError, match="window_seconds"):
            TokenBudgetConfig(cap_tokens=100, window_seconds=0)

    def test_threshold_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="soft_threshold"):
            TokenBudgetConfig(cap_tokens=100, soft_threshold=0.0)

    def test_threshold_above_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="soft_threshold"):
            TokenBudgetConfig(cap_tokens=100, soft_threshold=1.5)


class TestComputeUtilization:
    def test_own_only(self) -> None:
        assert compute_utilization(500_000, None, 1_000_000) == 0.5

    def test_provider_wide_wins_when_higher(self) -> None:
        assert compute_utilization(100_000, 800_000, 1_000_000) == 0.8

    def test_own_wins_when_higher(self) -> None:
        assert compute_utilization(900_000, 800_000, 1_000_000) == 0.9

    def test_none_when_no_cap(self) -> None:
        assert compute_utilization(500, None, 0) is None

    def test_none_when_negative_cap(self) -> None:
        assert compute_utilization(500, None, -1) is None

    def test_zero_usage(self) -> None:
        assert compute_utilization(0, None, 1_000_000) == 0.0

    def test_over_cap(self) -> None:
        assert compute_utilization(1_500_000, None, 1_000_000) == 1.5


class TestProjectUtilization:
    def test_linear_projection(self) -> None:
        # 400K tokens in 25% of a 1-hour window → project to 1.6M / 1M = 1.6
        result = project_utilization(
            400_000, elapsed_seconds=900, window_seconds=3600,
            cap_tokens=1_000_000,
        )
        assert result == pytest.approx(1.6)

    def test_no_projection_early_window(self) -> None:
        # elapsed < 10% → return current utilization, not projected
        result = project_utilization(
            100_000, elapsed_seconds=60, window_seconds=3600,
            cap_tokens=1_000_000,
        )
        assert result == pytest.approx(0.1)

    def test_zero_elapsed(self) -> None:
        result = project_utilization(
            0, elapsed_seconds=0, window_seconds=3600,
            cap_tokens=1_000_000,
        )
        assert result == 0.0

    def test_none_when_no_cap(self) -> None:
        assert project_utilization(
            500, elapsed_seconds=1800, window_seconds=3600, cap_tokens=0,
        ) is None

    def test_never_negative(self) -> None:
        result = project_utilization(
            0, elapsed_seconds=1800, window_seconds=3600, cap_tokens=1_000_000,
        )
        assert result == 0.0


class TestIsOverBudget:
    def test_at_threshold(self) -> None:
        assert is_over_budget(0.85, 0.85) is True

    def test_above_threshold(self) -> None:
        assert is_over_budget(0.9, 0.85) is True

    def test_below_threshold(self) -> None:
        assert is_over_budget(0.5, 0.85) is False

    def test_none_utilization(self) -> None:
        assert is_over_budget(None, 0.85) is False
