"""Unit tests for the low-interactivity threshold estimator (Plan 010 Feature C)."""

from __future__ import annotations

from switchboard.threshold import (
    EstimatorState,
    ThresholdSample,
    update,
)


def _feed(samples: list[ThresholdSample]) -> EstimatorState:
    state = EstimatorState()
    for s in samples:
        state = update(state, s)
    return state


def _s(
    window: str,
    requests: int,
    tokens: int,
    low: bool,
    *,
    sessions: int = 1,
) -> ThresholdSample:
    return ThresholdSample(
        window_id=window,
        requests=requests,
        tokens=tokens,
        concurrent_sessions=sessions,
        low_interactivity=low,
    )


def test_single_edge_brackets_both_dimensions() -> None:
    # OFF at 100 req / 1000 tok, then ON at 120 req / 1300 tok.
    state = _feed([
        _s("w1", 100, 1000, False),
        _s("w1", 120, 1300, True),
    ])
    assert state.estimate.edges == 1
    assert state.estimate.requests.lower == 100
    assert state.estimate.requests.upper == 120
    assert state.estimate.requests.best_guess == 110
    assert state.estimate.tokens.lower == 1000
    assert state.estimate.tokens.upper == 1300
    assert state.estimate.tokens.best_guess == 1150


def test_window_starting_on_records_no_edge() -> None:
    # Residual penalty carried into a fresh window: no OFF seen → not evidence.
    state = _feed([
        _s("w2", 5, 50, True),
        _s("w2", 8, 90, True),
    ])
    assert state.estimate.edges == 0
    assert state.estimate.requests.best_guess is None


def test_multiple_windows_tighten_bracket() -> None:
    state = _feed([
        # w1: OFF up to 100, ON at 150 → (100, 150]
        _s("w1", 90, 900, False),
        _s("w1", 100, 1000, False),
        _s("w1", 150, 1500, True),
        # w2: OFF up to 110, ON at 130 → tightens to (110, 130]
        _s("w2", 105, 1050, False),
        _s("w2", 110, 1100, False),
        _s("w2", 130, 1300, True),
    ])
    assert state.estimate.edges == 2
    assert state.estimate.requests.lower == 110  # max of OFF brackets
    assert state.estimate.requests.upper == 130  # min of ON brackets
    assert state.estimate.requests.best_guess == 120
    assert not state.estimate.requests.contradicted


def test_contradiction_surfaces_instead_of_false_number() -> None:
    state = _feed([
        # w1: still OFF at 200, ON at 210
        _s("w1", 200, 100, False),
        _s("w1", 210, 105, True),
        # w2: ON already at 150 (lower than w1's OFF-at-200) → lower(200) >= upper(150)
        _s("w2", 140, 90, False),
        _s("w2", 150, 95, True),
    ])
    req = state.estimate.requests
    assert req.contradicted is True
    assert req.best_guess is None


def test_only_upper_bound_when_no_off_observed_but_edge_via_flap() -> None:
    # Same window, OFF then ON then OFF then ON — first edge counts; second
    # edge is suppressed (edge_recorded) so we don't double count within a window.
    state = _feed([
        _s("w1", 50, 500, False),
        _s("w1", 60, 600, True),
        _s("w1", 55, 610, False),
        _s("w1", 70, 700, True),
    ])
    assert state.estimate.edges == 1
    assert state.estimate.requests.upper == 60


def test_tokens_can_be_binding_while_requests_contradict() -> None:
    # requests contradict but tokens stay consistent — the estimator keeps them
    # independent so the binding dimension is visible.
    state = _feed([
        _s("w1", 300, 1000, False),
        _s("w1", 310, 1100, True),   # req (300,310], tok (1000,1100]
        _s("w2", 200, 1050, False),
        _s("w2", 205, 1150, True),   # req: lower 300 >= upper 205 → contradict
    ])
    assert state.estimate.requests.contradicted is True
    assert state.estimate.tokens.contradicted is False
    assert state.estimate.tokens.best_guess is not None


def test_last_edge_concurrent_sessions_recorded() -> None:
    state = _feed([
        _s("w1", 100, 1000, False, sessions=2),
        _s("w1", 120, 1200, True, sessions=4),
    ])
    assert state.estimate.last_edge_concurrent_sessions == 4
