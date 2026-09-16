"""Hybrid controller: statistics, loss estimation, the switching rule (T5.1-T5.3, T5.6).

This module is the *decision* half of the hybrid. It answers one question —
"should the transfer be running GBN or SR right now?" — from statistics the
sender feeds it, and it answers in mode names. It never touches a socket, a
packet, a file or a timer.

Layering (design.md §1.1): the controller knows about statistics, thresholds and
hysteresis. It does not know the byte layout, and it does not perform the mode
switch; the MODE handshake itself lives in ``sender.py`` / ``receiver.py``
because it is lifecycle, not arithmetic. What the controller produces is a
:class:`SwitchDecision`; what the endpoints do with it is theirs.

FROZEN DECISION
===============

D8 — loss estimator (T5.2, specs.md §16.9, §10, HY-01)
-------------------------------------------------------
**A fixed-size sliding window over the last ``LOSS_WINDOW_SIZE`` (50) segment
outcomes, where an outcome is recorded when a segment is acknowledged and is 1
if that segment ever required retransmission and 0 if it did not**::

    loss_estimate = retransmitted_segments_in_window / segments_in_window

Rationale:

* It is directly observable on the sender, with no extra packets and no
  cooperation from the receiver.
* It is computed identically in both modes — both count "this segment needed
  sending again" the same way — so a threshold means the same thing on either
  side of a switch. An estimator that changed meaning at the switch boundary
  could not be compared against a fixed threshold at all.
* It is bounded in [0, 1], so ``SWITCH_HIGH`` and ``SWITCH_LOW`` read directly
  as loss rates and sit interpretably inside the loss grid of specs.md §17.1.
* The alternative, an EWMA of the same ratio, reacts more smoothly but adds a
  second tunable (alpha) that interacts with the hysteresis count and makes the
  dynamic-loss experiment (E8) materially harder to explain.

**Known bias, to be reported and not hidden (T10.4).** Under GBN a single lost
segment forces retransmission of the entire outstanding range, so every segment
behind the loss is also recorded as an outcome of 1. The estimator therefore
*over-reads* loss while GBN is active — by roughly the window occupancy —
relative to the same physical loss rate observed under SR. Two consequences
follow:

1. The controller is biased toward *entering* SR. That is the direction that
   fails safe, SR being the mode that tolerates loss, but it means the
   threshold that fires is not the physical loss rate, and the report must
   say so rather than presenting SWITCH_HIGH as a loss percentage.
2. The bias is asymmetric across the dead band: the GBN to SR test sees
   inflated values, the SR to GBN test does not. ``SWITCH_LOW`` is therefore
   evaluated on a different scale from ``SWITCH_HIGH``, which is part of what
   T7.1 has to calibrate against real data.

An outcome is recorded at *acknowledgement*, not at transmission. Counting
transmissions instead would weight a segment by how many times it was resent
and push the ratio above the loss rate even without GBN's range effect;
counting resolved segments keeps one segment worth exactly one observation.

The window is **not** cleared on a mode switch. The new mode inherits the
evidence that justified entering it, which together with the minimum residence
time is what stops a switch from being immediately re-evaluated against an
almost empty window (HY-09).

**This estimator is frozen.** Changing the formula, the window size, or the
moment an outcome is recorded changes every switching decision and invalidates
every experiment already run (specs.md §10).

DECISION RULE (D9 — thresholds are NOT frozen; T7.2 calibrates them)
=====================================================================
Dual thresholds with a dead band, plus a consecutive-confirmation counter and a
minimum residence time (HY-02, HY-03, HY-09)::

    GBN and loss_estimate > SWITCH_HIGH, HYSTERESIS_COUNT times running -> SR
    SR  and loss_estimate < SWITCH_LOW,  HYSTERESIS_COUNT times running -> GBN
    anything else -> stay, and reset the confirmation counter

Comparisons are strict, so a value sitting exactly on a threshold does not
switch: the thresholds bound the dead band inclusively, and a run held precisely
at one is the case hysteresis exists to leave alone.

Evaluation happens every ``EVALUATION_INTERVAL_SEGMENTS`` acknowledged segments
rather than on a wall-clock tick, so the cadence scales with progress instead of
firing repeatedly while a stalled window waits on a timer.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace

import config

#: CLI modes that run the controller rather than one fixed strategy.
HYBRID_MODES = ("hybrid", "fixed-hybrid")

#: The modes the controller chooses between. "saw" is not one of them: it is the
#: Phase 2 placeholder, not a system under evaluation (specs.md §18).
ARQ_MODES = ("gbn", "sr")


def initial_mode() -> str:
    """The mode a hybrid transfer starts in, from ``config.DEFAULT_MODE``.

    Both endpoints read the same config value, which is what lets the receiver
    build the right starting strategy from a START that names only "hybrid" —
    no extra field on the wire, and no way for the two sides to disagree about
    where the transfer began.
    """
    mode = config.DEFAULT_MODE.strip().lower()
    if mode not in ARQ_MODES:
        raise ValueError(
            f"config.DEFAULT_MODE must be one of {ARQ_MODES}, "
            f"got {config.DEFAULT_MODE!r}")
    return mode


# ---------------------------------------------------------------------------
# D8 — the loss estimator
# ---------------------------------------------------------------------------


class LossEstimator:
    """Sliding window of segment outcomes (D8, frozen at T5.2)."""

    def __init__(self, window_size: int = config.LOSS_WINDOW_SIZE):
        if window_size < 1:
            raise ValueError(f"loss window must be >= 1, got {window_size}")
        self.window_size = window_size
        self._outcomes: deque[int] = deque(maxlen=window_size)
        self.total_outcomes = 0

    def record(self, retransmitted: bool) -> None:
        """Record one resolved segment: True if it ever had to be sent again."""
        self._outcomes.append(1 if retransmitted else 0)
        self.total_outcomes += 1

    @property
    def samples(self) -> int:
        """Outcomes currently in the window, at most ``window_size``."""
        return len(self._outcomes)

    def estimate(self) -> float:
        """The observed loss indicator, in [0, 1].

        Zero before any segment has resolved. That is never read as "no loss":
        an evaluation only happens after ``EVALUATION_INTERVAL_SEGMENTS``
        acknowledged segments, so the window is never consulted while empty.
        """
        if not self._outcomes:
            return 0.0
        return sum(self._outcomes) / len(self._outcomes)


# ---------------------------------------------------------------------------
# T5.1 — the statistics collector (specs.md §10)
# ---------------------------------------------------------------------------


class TransferStatistics:
    """Every statistic in specs.md §10, collected on the sender.

    These are a cross-check and a convenience for ``summary.json``; they are not
    the record. Each is independently derivable from ``events.csv`` (CC-06):
    transmissions from SEND plus RETX rows, unique DATA from distinct SEND
    sequences, ACKed from ACK rows, retransmissions from RETX rows, mode and
    loss estimate from their own columns, switches from SWITCH rows, and
    residence from the timestamps between them.
    """

    def __init__(self, mode: str, now: float):
        self.mode = mode
        self.mode_since = now
        self.data_transmissions = 0
        self.unique_data_packets = 0
        self.retransmissions = 0
        self.acked_packets = 0
        self.switch_count = 0
        self.handshake_count = 0        # committed MODE exchanges, no-ops included
        self.abandoned_switches = 0     # handshakes that ran out of retries
        self.rtt_samples: list[float] = []
        self._residence: dict[str, float] = defaultdict(float)

    # -- collection --------------------------------------------------------

    def on_transmission(self, *, retransmitted: bool) -> None:
        self.data_transmissions += 1
        if retransmitted:
            self.retransmissions += 1
        else:
            self.unique_data_packets += 1

    def on_acked(self, count: int = 1) -> None:
        self.acked_packets += count

    def on_rtt_sample(self, rtt_ms: float) -> None:
        self.rtt_samples.append(rtt_ms)

    def enter_mode(self, mode: str, now: float) -> None:
        """Close out the current mode's residence and open the next one's."""
        self._residence[self.mode] += max(0.0, now - self.mode_since)
        if mode != self.mode:
            self.switch_count += 1
        self.mode = mode
        self.mode_since = now

    # -- reporting ---------------------------------------------------------

    def residence_times(self, now: float) -> dict[str, float]:
        """Per-mode residence, including the mode currently running."""
        times = dict(self._residence)
        times[self.mode] = times.get(self.mode, 0.0) + max(0.0, now - self.mode_since)
        return times

    def as_dict(self, now: float, loss_estimate: float) -> dict:
        residence = self.residence_times(now)
        rtt = self.rtt_samples
        return {
            "data_transmissions": self.data_transmissions,
            "unique_data_packets": self.unique_data_packets,
            "acked_packets": self.acked_packets,
            "retransmissions": self.retransmissions,
            "loss_estimate": loss_estimate,
            "mode": self.mode,
            "switch_count": self.switch_count,
            "handshake_count": self.handshake_count,
            "abandoned_switches": self.abandoned_switches,
            "gbn_residence_s": residence.get("gbn", 0.0),
            "sr_residence_s": residence.get("sr", 0.0),
            "rtt_samples": len(rtt),
            "rtt_mean_ms": (sum(rtt) / len(rtt)) if rtt else None,
        }


# ---------------------------------------------------------------------------
# T5.3 — the decision rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HybridSettings:
    """The controller's tunables, all from config.py (D9, not yet frozen)."""

    loss_window_size: int = config.LOSS_WINDOW_SIZE
    switch_high: float = config.SWITCH_HIGH
    switch_low: float = config.SWITCH_LOW
    hysteresis_count: int = config.HYSTERESIS_COUNT
    evaluation_interval_segments: int = config.EVALUATION_INTERVAL_SEGMENTS
    min_mode_residence_s: float = config.MIN_MODE_RESIDENCE_S

    def __post_init__(self) -> None:
        if not 0.0 <= self.switch_low <= self.switch_high <= 1.0:
            raise ValueError(
                f"thresholds must satisfy 0 <= SWITCH_LOW ({self.switch_low}) <= "
                f"SWITCH_HIGH ({self.switch_high}) <= 1; an inverted pair leaves no "
                f"dead band and would oscillate by construction")
        if self.hysteresis_count < 1:
            raise ValueError(f"hysteresis count must be >= 1, got {self.hysteresis_count}")
        if self.evaluation_interval_segments < 1:
            raise ValueError("evaluation interval must be >= 1 segment")
        if self.min_mode_residence_s < 0:
            raise ValueError("minimum residence time must be non-negative")

    @classmethod
    def from_config(cls) -> HybridSettings:
        """Read config.py *now*, rather than at import time.

        The experiment runner (T8.2) and the threshold sweep (T7.1) both set
        these per run, so the values have to be picked up when the controller is
        built, not when this module first happened to be imported.
        """
        return cls(
            loss_window_size=config.LOSS_WINDOW_SIZE,
            switch_high=config.SWITCH_HIGH,
            switch_low=config.SWITCH_LOW,
            hysteresis_count=config.HYSTERESIS_COUNT,
            evaluation_interval_segments=config.EVALUATION_INTERVAL_SEGMENTS,
            min_mode_residence_s=config.MIN_MODE_RESIDENCE_S,
        )

    def with_(self, **changes) -> HybridSettings:
        return replace(self, **changes)


@dataclass(frozen=True)
class SwitchDecision:
    """The controller's verdict that the mode should change.

    ``reason`` is what lands in the SWITCH event's reason column (HY-08).
    """

    from_mode: str
    target_mode: str
    loss_estimate: float
    reason: str = "MODE_THRESHOLD"


class HybridController:
    """Monitors, decides, and remembers — but never switches anything itself.

    Two mode attributes, which hold the same value except under ``fixed-hybrid``:

    ``mode``
        the *decision* mode: what the threshold rule believes the transfer
        should be running. This is what the rule is evaluated against.
    ``active_mode``
        the *wire* mode: which strategy the endpoints actually have live.

    Under ``--mode fixed-hybrid`` (T5.6) the two diverge deliberately. The
    controller decides, the sender drains its window and runs the full MODE
    handshake, and then the wire mode stays exactly where it was. That is what
    makes the control meaningful: it pays the same drain and handshake cost at
    the same moments as the real hybrid, so the difference between the two
    isolates the benefit of changing semantics from the cost of changing them
    (design.md §6.2, §6.3, specs.md §18).
    """

    def __init__(self, *, settings: HybridSettings | None = None,
                 switching_enabled: bool = True, mode: str | None = None,
                 now: float = 0.0):
        self.settings = settings or HybridSettings.from_config()
        self.switching_enabled = switching_enabled
        self.mode = mode or initial_mode()
        if self.mode not in ARQ_MODES:
            raise ValueError(f"hybrid mode must be one of {ARQ_MODES}, got {self.mode!r}")
        self.active_mode = self.mode

        self.estimator = LossEstimator(self.settings.loss_window_size)
        self.stats = TransferStatistics(self.active_mode, now)
        self.epoch = 0
        self.evaluations = 0

        self._confirmations = 0
        self._acked_since_evaluation = 0
        self._mode_since = now

    # -- observation -------------------------------------------------------

    @property
    def loss_estimate(self) -> float:
        return self.estimator.estimate()

    @property
    def confirmations(self) -> int:
        """Consecutive confirming evaluations so far (the hysteresis state)."""
        return self._confirmations

    def on_transmission(self, seq: int, *, retransmitted: bool) -> None:
        self.stats.on_transmission(retransmitted=retransmitted)

    def on_segment_acked(self, seq: int, *, retransmitted: bool, now: float) -> None:
        """One segment resolved. This is the only thing that feeds D8."""
        self.estimator.record(retransmitted)
        self.stats.on_acked()
        self._acked_since_evaluation += 1

    def on_rtt_sample(self, rtt_ms: float) -> None:
        self.stats.on_rtt_sample(rtt_ms)

    # -- decision ----------------------------------------------------------

    def evaluate(self, now: float) -> SwitchDecision | None:
        """Apply the threshold rule. Returns a decision only when it fires.

        Order matters. The residence check comes *before* the cadence counter is
        consumed, so a mode that has not yet served its minimum time does not
        silently burn its evaluation — the evaluation happens as soon as the
        residence requirement is met, instead of being skipped entirely (HY-03).
        """
        if self._acked_since_evaluation < self.settings.evaluation_interval_segments:
            return None
        if now - self._mode_since < self.settings.min_mode_residence_s:
            return None

        self._acked_since_evaluation = 0
        self.evaluations += 1
        estimate = self.estimator.estimate()

        if self.mode == "gbn":
            confirmed, target = estimate > self.settings.switch_high, "sr"
        else:
            confirmed, target = estimate < self.settings.switch_low, "gbn"

        if not confirmed:
            self._confirmations = 0
            return None

        self._confirmations += 1
        if self._confirmations < self.settings.hysteresis_count:
            return None
        return SwitchDecision(from_mode=self.mode, target_mode=target,
                              loss_estimate=estimate)

    # -- transition bookkeeping -------------------------------------------
    #
    # The endpoints run the handshake (design.md §6.3); the controller only
    # records what became of it.

    def begin_switch(self) -> int:
        """Open a new handshake and return its epoch.

        The epoch increments per *attempt set*, not per retransmitted MODE
        packet, so a late echo from an abandoned handshake is recognisable as
        stale and discarded rather than reactivating a superseded switch (HY-07).
        """
        self.epoch += 1
        return self.epoch

    def commit_switch(self, decision: SwitchDecision, now: float) -> None:
        """The handshake completed. Adopt the decision."""
        self.mode = decision.target_mode
        self._mode_since = now
        self._confirmations = 0
        self._acked_since_evaluation = 0
        self.stats.handshake_count += 1
        if self.switching_enabled:
            self.active_mode = decision.target_mode
            self.stats.enter_mode(decision.target_mode, now)

    def abandon_switch(self, now: float) -> None:
        """The handshake ran out of retries. Stay where we are, safely.

        The transfer continues in the current mode and the hysteresis state is
        reset, so a receiver that cannot complete a handshake is not hammered
        with a fresh attempt on the very next evaluation (specs.md §13, "mode
        inconsistency: synchronize or fail safely").
        """
        self._confirmations = 0
        self._acked_since_evaluation = 0
        self._mode_since = now
        self.stats.abandoned_switches += 1

    # -- reporting ---------------------------------------------------------

    def snapshot(self, now: float) -> dict:
        """Controller state for summary.json (specs.md §10, §20)."""
        state = self.stats.as_dict(now, self.loss_estimate)
        state.update({
            "decision_mode": self.mode,
            "active_mode": self.active_mode,
            "switching_enabled": self.switching_enabled,
            "epoch": self.epoch,
            "evaluations": self.evaluations,
            "loss_window_samples": self.estimator.samples,
            "settings": {
                "loss_window_size": self.settings.loss_window_size,
                "switch_high": self.settings.switch_high,
                "switch_low": self.settings.switch_low,
                "hysteresis_count": self.settings.hysteresis_count,
                "evaluation_interval_segments": self.settings.evaluation_interval_segments,
                "min_mode_residence_s": self.settings.min_mode_residence_s,
            },
        })
        return state


def make_controller(mode: str, now: float,
                    settings: HybridSettings | None = None) -> HybridController | None:
    """Build a controller for a CLI mode, or None for a fixed strategy."""
    if mode not in HYBRID_MODES:
        return None
    return HybridController(settings=settings, switching_enabled=(mode == "hybrid"),
                            mode=initial_mode(), now=now)
