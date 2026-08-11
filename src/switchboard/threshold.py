"""Pure estimator for the low-interactivity trigger threshold (Plan 010 Feature C).

Learns, over time, the per-window usage level at which umans' low-interactivity
service mode engages — while being honest that the request/token-threshold
hypothesis may be **wrong**.  It is built to *test* the hypothesis, not assume it.

Method.  Each ``/v1/usage`` poll (once Feature 0 labels it) is a
:class:`ThresholdSample` ``(window_id, requests, tokens, concurrent_sessions,
low_interactivity)``.  Counters reset per request window, so ``window_id``
identifies the window.  A genuine OFF→ON **edge within one window** brackets the
trigger:

* every OFF sample proves the trigger is *above* its usage → lower bound;
* the first ON sample at the edge proves it is *at or below* its usage → upper
  bound.

A window that *starts* ON (residual penalty carried over from an earlier window,
since the penalty outlasts the request window) has no OFF→ON edge and is
correctly ignored — it is not evidence about the trigger.

Across windows the estimate tightens: ``lower`` = the largest OFF-bracketed
usage ever seen, ``upper`` = the smallest ON-at-edge usage ever seen.  If
``lower >= upper`` the single-threshold hypothesis is contradicted for that
dimension — surfaced as :attr:`DimensionEstimate.contradicted` rather than
emitting a false point.  Computed independently for **requests** and **tokens**;
whichever stays tight and consistent is the likely binding constraint.

Pure and deterministic: :func:`update` takes prior state + one sample and returns
new state.  No clock, no I/O.  The shell derives ``window_id`` and persists state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class ThresholdSample:
    """One labelled usage observation."""

    window_id: str
    requests: int
    tokens: int
    concurrent_sessions: int
    low_interactivity: bool


@dataclass(frozen=True)
class ThresholdEvent:
    """A recorded threshold observation — trigger or non-trigger.

    Emitted by :func:`update` when an edge is detected (``triggered=True``)
    or when a window ends without low-interactivity ever engaging
    (``triggered=False``).  The shell persists these to SQLite for a 30-day
    rolling history so operators can see the dynamic threshold over time.

    For trigger events, ``requests``/``tokens``/``concurrent_sessions`` are
    the values at the moment low-interactivity engaged.  For non-trigger
    events, they are the **maximum** OFF usage observed in that window — the
    highest usage that did *not* trigger low priority.
    """

    window_id: str
    requests: int
    tokens: int
    concurrent_sessions: int
    triggered: bool


@dataclass(frozen=True)
class DimensionEstimate:
    """Bracketed estimate for one usage dimension (requests or tokens).

    ``lower`` = largest usage proven still-OFF at an edge; the trigger is above
    it.  ``upper`` = smallest usage proven ON at an edge; the trigger is at or
    below it.  ``edges`` counts the OFF→ON transitions that contributed.
    """

    lower: int | None = None  # trigger > lower
    upper: int | None = None  # trigger <= upper
    edges: int = 0

    @property
    def contradicted(self) -> bool:
        """True if the observed brackets are mutually inconsistent.

        ``lower >= upper`` means some window stayed OFF at a usage level at or
        above where another window was already ON — the trigger is not a stable
        single value in this dimension (noisy, or this is not the binding
        constraint).  A finding, not a number.
        """
        return (
            self.lower is not None
            and self.upper is not None
            and self.lower >= self.upper
        )

    @property
    def best_guess(self) -> int | None:
        """Midpoint of the consistent bracket, or None if unknown/contradicted.

        Returns ``upper`` alone when only an upper bound is known (windows that
        started with an OFF observation are needed for a lower bound).

        For adjacent integer bounds (upper == lower + 1) returns ``upper`` —
        the proven-trigger value — rather than floor division, which would
        return the proven-non-trigger ``lower``.
        """
        if self.contradicted:
            return None
        if self.lower is not None and self.upper is not None:
            if self.upper == self.lower + 1:
                return self.upper
            return (self.lower + self.upper) // 2
        if self.upper is not None:
            return self.upper
        return None


@dataclass(frozen=True)
class _WindowProgress:
    """Transient per-window state used to detect an OFF→ON edge."""

    window_id: str
    saw_off: bool = False
    max_off_requests: int = 0
    max_off_tokens: int = 0
    prev_low: bool = False
    edge_recorded: bool = False


@dataclass(frozen=True)
class ThresholdEstimate:
    """The learned estimate, plus context for surfacing."""

    requests: DimensionEstimate = field(default_factory=DimensionEstimate)
    tokens: DimensionEstimate = field(default_factory=DimensionEstimate)
    edges: int = 0  # total OFF→ON edges observed
    last_edge_concurrent_sessions: int | None = None  # control dimension


@dataclass(frozen=True)
class EstimatorState:
    """Full persisted estimator state: the estimate + current window progress."""

    estimate: ThresholdEstimate = field(default_factory=ThresholdEstimate)
    window: _WindowProgress | None = None


def _tighten(dim: DimensionEstimate, *, off_usage: int | None, on_usage: int) -> DimensionEstimate:
    """Fold one edge's bracket into a dimension estimate."""
    upper = on_usage if dim.upper is None else min(dim.upper, on_usage)
    lower = dim.lower
    if off_usage is not None:
        lower = off_usage if dim.lower is None else max(dim.lower, off_usage)
    return DimensionEstimate(lower=lower, upper=upper, edges=dim.edges + 1)


def update(
    state: EstimatorState, sample: ThresholdSample,
) -> tuple[EstimatorState, ThresholdEvent | None]:
    """Fold one sample into the estimator state.  Pure and deterministic.

    Returns ``(new_state, event)`` where ``event`` is a
    :class:`ThresholdEvent` when:

    * an OFF→ON edge is detected (``triggered=True``), or
    * a window ends without low-interactivity ever engaging
      (``triggered=False`` — the max OFF usage in that window).

    ``event`` is ``None`` for intermediate samples that don't produce an
    observation worth recording.
    """
    win = state.window
    event: ThresholdEvent | None = None

    if win is not None and win.window_id != sample.window_id:
        # Window transition: if the previous window had OFF samples but no
        # edge was recorded, emit a non-trigger event with the max OFF usage.
        if win.saw_off and not win.edge_recorded:
            event = ThresholdEvent(
                window_id=win.window_id,
                requests=win.max_off_requests,
                tokens=win.max_off_tokens,
                concurrent_sessions=0,
                triggered=False,
            )
        # New window: no carried OFF/edge state.  A window that starts ON has
        # saw_off=False, so its first ON sample records no edge (residual
        # penalty, not a trigger).
        win = _WindowProgress(window_id=sample.window_id)
    elif win is None:
        win = _WindowProgress(window_id=sample.window_id)

    if not sample.low_interactivity:
        return (
            EstimatorState(
                estimate=state.estimate,
                window=replace(
                    win,
                    saw_off=True,
                    max_off_requests=max(win.max_off_requests, sample.requests),
                    max_off_tokens=max(win.max_off_tokens, sample.tokens),
                    prev_low=False,
                ),
            ),
            event,
        )

    # sample.low_interactivity is True.
    is_edge = (not win.prev_low) and (not win.edge_recorded) and win.saw_off
    if not is_edge:
        return (
            EstimatorState(
                estimate=state.estimate,
                window=replace(win, prev_low=True),
            ),
            event,
        )

    est = state.estimate
    new_estimate = ThresholdEstimate(
        requests=_tighten(
            est.requests, off_usage=win.max_off_requests, on_usage=sample.requests
        ),
        tokens=_tighten(
            est.tokens, off_usage=win.max_off_tokens, on_usage=sample.tokens
        ),
        edges=est.edges + 1,
        last_edge_concurrent_sessions=sample.concurrent_sessions,
    )
    trigger_event = ThresholdEvent(
        window_id=sample.window_id,
        requests=sample.requests,
        tokens=sample.tokens,
        concurrent_sessions=sample.concurrent_sessions,
        triggered=True,
    )
    return (
        EstimatorState(
            estimate=new_estimate,
            window=replace(win, prev_low=True, edge_recorded=True),
        ),
        trigger_event,
    )
