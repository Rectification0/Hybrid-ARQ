"""Hybrid controller tests (T5.7) and switching integration tests (T5.8).

T5.7 requires: threshold evaluation at, just below and just above each
threshold; hysteresis suppressing a single-observation spike; a stale MODE echo
being discarded; and a failed handshake leaving the transfer in a valid single
mode.

T5.8 requires: a transfer through GBN to SR, a transfer through SR to GBN, a
matching hash in both, and a transfer held near the threshold that does not
oscillate.

The integration tests drive real sockets through the impairment simulator, with
the controller's tunables supplied per test. Deliberately *not* the config
defaults: D9 is not frozen until T7.2, so a test that depended on today's
SWITCH_HIGH would have to be rewritten the moment calibration moves it. What the
tests pin is the rule, not the numbers.
"""

import csv
import hashlib
import threading

import pytest

import config
from eventlog import EventLog
from network.simulator import Impairment
from protocol import hybrid
from protocol.hybrid import (HybridController, HybridSettings, LossEstimator,
                             SwitchDecision, TransferStatistics)
from protocol.packet import Packet, PacketType
from protocol import packet as pk
from protocol.strategy import (ReceiverTransferState, SenderTransferState,
                               make_sender_strategy)
from receiver import Receiver, ReceiverError
from sender import Sender, TransferError

from test_transfer import make_file, read_events, sha256_of


def settings(**changes) -> HybridSettings:
    """Explicit, test-local tunables — never the unfrozen config defaults."""
    base = HybridSettings(loss_window_size=20, switch_high=0.05, switch_low=0.02,
                          hysteresis_count=3, evaluation_interval_segments=5,
                          min_mode_residence_s=0.0)
    return base.with_(**changes)


def controller(mode="gbn", now=0.0, switching_enabled=True, **changes) -> HybridController:
    return HybridController(settings=settings(**changes), mode=mode,
                            switching_enabled=switching_enabled, now=now)


def feed(ctrl, outcomes, *, now=0.0):
    """Resolve one segment per outcome; returns the decisions that fired."""
    decisions = []
    for index, retransmitted in enumerate(outcomes):
        ctrl.on_segment_acked(index, retransmitted=bool(retransmitted), now=now)
        decision = ctrl.evaluate(now)
        if decision is not None:
            decisions.append(decision)
            ctrl.commit_switch(decision, now)
    return decisions


# ---------------------------------------------------------------------------
# T5.2 / D8 — the loss estimator
# ---------------------------------------------------------------------------


def test_estimate_is_the_retransmitted_fraction_of_the_window():
    estimator = LossEstimator(window_size=10)
    for outcome in [True, False, False, False]:
        estimator.record(outcome)
    assert estimator.estimate() == pytest.approx(0.25)


def test_window_holds_only_the_most_recent_outcomes():
    """D8 is a *sliding* window: old evidence has to age out, or the estimator
    could never follow a condition that changes (E8)."""
    estimator = LossEstimator(window_size=4)
    for _ in range(4):
        estimator.record(True)
    assert estimator.estimate() == 1.0
    for _ in range(4):
        estimator.record(False)
    assert estimator.samples == 4
    assert estimator.estimate() == 0.0
    assert estimator.total_outcomes == 8


def test_empty_window_reads_zero():
    assert LossEstimator(window_size=5).estimate() == 0.0


def test_estimate_stays_within_zero_and_one():
    estimator = LossEstimator(window_size=3)
    for outcome in (True, True, True):
        estimator.record(outcome)
    assert estimator.estimate() == 1.0


@pytest.mark.parametrize("bad", [0, -1])
def test_loss_window_below_one_is_refused(bad):
    with pytest.raises(ValueError, match="loss window"):
        LossEstimator(window_size=bad)


def test_estimator_counts_a_segment_once_however_often_it_was_resent():
    """D8 records an outcome at acknowledgement, not per transmission: a segment
    sent four times is still one observation, or the ratio would exceed the
    loss rate even with no GBN range effect."""
    ctrl = controller()
    for seq in range(10):
        ctrl.on_transmission(seq, retransmitted=False)
    for seq in range(3):
        for _ in range(4):
            ctrl.on_transmission(seq, retransmitted=True)
    for seq in range(10):
        ctrl.on_segment_acked(seq, retransmitted=seq < 3, now=1.0)
    assert ctrl.loss_estimate == pytest.approx(0.3)
    assert ctrl.estimator.samples == 10
    assert ctrl.stats.retransmissions == 12, "transmissions are still counted in full"


# ---------------------------------------------------------------------------
# T5.1 — the statistics collector (specs.md §10)
# ---------------------------------------------------------------------------


def test_statistics_separate_unique_packets_from_retransmissions():
    stats = TransferStatistics("gbn", now=0.0)
    for _ in range(5):
        stats.on_transmission(retransmitted=False)
    for _ in range(2):
        stats.on_transmission(retransmitted=True)
    assert stats.data_transmissions == 7
    assert stats.unique_data_packets == 5
    assert stats.retransmissions == 2


def test_residence_accumulates_per_mode_and_counts_transitions():
    stats = TransferStatistics("gbn", now=0.0)
    stats.enter_mode("sr", now=2.0)
    stats.enter_mode("gbn", now=5.0)
    times = stats.residence_times(now=6.0)
    assert times["gbn"] == pytest.approx(3.0)       # 0->2 and 5->6
    assert times["sr"] == pytest.approx(3.0)        # 2->5
    assert stats.switch_count == 2


def test_re_entering_the_same_mode_is_not_a_switch():
    """A fixed-hybrid handshake must not inflate the switch count: nothing
    changed mode, so nothing transitioned."""
    stats = TransferStatistics("gbn", now=0.0)
    stats.enter_mode("gbn", now=1.0)
    assert stats.switch_count == 0
    assert stats.residence_times(now=1.0)["gbn"] == pytest.approx(1.0)


def test_statistics_cover_every_item_in_spec_10():
    ctrl = controller()
    ctrl.on_transmission(0, retransmitted=False)
    ctrl.on_segment_acked(0, retransmitted=False, now=0.5)
    ctrl.on_rtt_sample(12.5)
    snapshot = ctrl.snapshot(now=1.0)
    for field in ("data_transmissions", "unique_data_packets", "acked_packets",
                  "retransmissions", "loss_estimate", "mode", "switch_count",
                  "gbn_residence_s", "sr_residence_s", "rtt_samples", "rtt_mean_ms"):
        assert field in snapshot, f"specs.md §10 statistic {field} is not reported"
    assert snapshot["rtt_mean_ms"] == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# T5.3 / T5.7 — the threshold rule, at and around each threshold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retransmitted, switches", [
    (1, True),      # 1 of 5 = 0.20, above SWITCH_HIGH = 0.10
    (0, False),     # 0.00, below
])
def test_gbn_switches_only_above_switch_high(retransmitted, switches):
    ctrl = controller(mode="gbn", switch_high=0.10, hysteresis_count=1,
                      evaluation_interval_segments=5)
    outcomes = [True] * retransmitted + [False] * (5 - retransmitted)
    decisions = feed(ctrl, outcomes)
    assert bool(decisions) is switches
    if switches:
        assert decisions[0].target_mode == "sr"


def test_exactly_at_switch_high_does_not_switch():
    """The comparison is strict, so the thresholds bound the dead band
    inclusively: a run held precisely on one is the case hysteresis exists to
    leave alone."""
    ctrl = controller(mode="gbn", switch_high=0.20, hysteresis_count=1,
                      evaluation_interval_segments=5)
    assert feed(ctrl, [True, False, False, False, False]) == []
    assert ctrl.loss_estimate == pytest.approx(0.20)


def test_sr_returns_to_gbn_only_below_switch_low():
    ctrl = controller(mode="sr", switch_high=0.60, switch_low=0.25,
                      hysteresis_count=1, evaluation_interval_segments=4)
    decisions = feed(ctrl, [False, False, False, False])     # estimate 0.0
    assert [d.target_mode for d in decisions] == ["gbn"]


def test_exactly_at_switch_low_does_not_return_to_gbn():
    ctrl = controller(mode="sr", switch_high=0.60, switch_low=0.25,
                      hysteresis_count=1, evaluation_interval_segments=4)
    assert feed(ctrl, [True, False, False, False]) == []     # estimate 0.25
    assert ctrl.mode == "sr"


def test_just_above_switch_low_holds_sr():
    ctrl = controller(mode="sr", switch_high=0.60, switch_low=0.20,
                      hysteresis_count=1, evaluation_interval_segments=5)
    assert feed(ctrl, [True, True, False, False, False]) == []   # 0.40
    assert ctrl.mode == "sr"


def test_no_evaluation_before_the_cadence_is_reached():
    ctrl = controller(mode="gbn", switch_high=0.0, switch_low=0.0,
                      hysteresis_count=1, evaluation_interval_segments=10)
    for seq in range(9):
        ctrl.on_segment_acked(seq, retransmitted=True, now=1.0)
        assert ctrl.evaluate(1.0) is None
    assert ctrl.evaluations == 0
    ctrl.on_segment_acked(9, retransmitted=True, now=1.0)
    assert ctrl.evaluate(1.0) is not None


# ---------------------------------------------------------------------------
# T5.7 — hysteresis
# ---------------------------------------------------------------------------


def test_hysteresis_suppresses_a_single_observation_spike():
    """HY-09. One unlucky burst that is not repeated must not move the mode.

    The window is sized to the evaluation interval so each evaluation reads
    fresh evidence; with a longer window the spike is still *in* the window
    several evaluations later, and confirming it again is correct behaviour
    rather than a hysteresis failure.
    """
    ctrl = controller(mode="gbn", switch_high=0.10, hysteresis_count=3,
                      evaluation_interval_segments=4, loss_window_size=4)
    spike = [True, True, False, False]              # 0.50 — over the threshold
    calm = [False] * 4                              # and then it passes
    decisions = feed(ctrl, spike) + feed(ctrl, calm) + feed(ctrl, calm)
    assert decisions == []
    assert ctrl.mode == "gbn"


def test_a_sustained_condition_switches_after_the_confirmation_count():
    ctrl = controller(mode="gbn", switch_high=0.10, hysteresis_count=3,
                      evaluation_interval_segments=4)
    burst = [True, True, False, False]
    assert feed(ctrl, burst) == [] and ctrl.confirmations == 1
    assert feed(ctrl, burst) == [] and ctrl.confirmations == 2
    decisions = feed(ctrl, burst)
    assert [d.target_mode for d in decisions] == ["sr"]
    assert ctrl.confirmations == 0, "the counter resets once the switch is taken"


def test_a_calm_evaluation_resets_the_confirmation_counter():
    ctrl = controller(mode="gbn", switch_high=0.10, hysteresis_count=3,
                      evaluation_interval_segments=4, loss_window_size=4)
    feed(ctrl, [True, True, False, False])
    assert ctrl.confirmations == 1
    feed(ctrl, [False] * 4)
    assert ctrl.confirmations == 0


def test_minimum_residence_time_defers_an_evaluation_rather_than_losing_it():
    """HY-03. The residence check runs before the cadence counter is consumed,
    so the evaluation happens as soon as the mode has served its time."""
    ctrl = controller(mode="gbn", switch_high=0.10, hysteresis_count=1,
                      evaluation_interval_segments=4, min_mode_residence_s=5.0,
                      now=0.0)
    for seq in range(4):
        ctrl.on_segment_acked(seq, retransmitted=True, now=1.0)
    assert ctrl.evaluate(1.0) is None, "too soon to leave the mode"
    assert ctrl.evaluations == 0
    decision = ctrl.evaluate(6.0)
    assert decision is not None and decision.target_mode == "sr"


def test_an_oscillating_condition_cannot_flap_the_mode():
    """CC-04 in miniature: a loss rate wandering across a single point, with a
    dead band around it, must settle rather than alternate."""
    ctrl = controller(mode="gbn", switch_high=0.60, switch_low=0.10,
                      hysteresis_count=3, evaluation_interval_segments=4)
    switches = 0
    for round_index in range(20):
        heavy = round_index % 2 == 0
        outcomes = [True, True, False, False] if heavy else [True, False, False, False]
        switches += len(feed(ctrl, outcomes))
    assert switches == 0, "a value inside the dead band must not move the mode"


def test_inverted_thresholds_are_refused():
    with pytest.raises(ValueError, match="dead band"):
        HybridSettings(switch_high=0.02, switch_low=0.05)


@pytest.mark.parametrize("bad", [{"hysteresis_count": 0},
                                 {"evaluation_interval_segments": 0},
                                 {"min_mode_residence_s": -1.0}])
def test_nonsensical_settings_are_refused(bad):
    with pytest.raises(ValueError):
        HybridSettings(**bad)


def test_settings_are_read_from_config_when_the_controller_is_built(monkeypatch):
    """T7.1 sweeps these per run, so they must be read at construction rather
    than captured when this module was first imported."""
    monkeypatch.setattr(config, "SWITCH_HIGH", 0.42)
    assert HybridSettings.from_config().switch_high == 0.42


def test_fixed_hybrid_decides_but_never_changes_the_wire_mode():
    """T5.6: the control pays the decision and the handshake, not the switch."""
    ctrl = controller(mode="gbn", switching_enabled=False, switch_high=0.10,
                      hysteresis_count=1, evaluation_interval_segments=4)
    decisions = feed(ctrl, [True, True, False, False])
    assert [d.target_mode for d in decisions] == ["sr"]
    assert ctrl.mode == "sr", "the decision mode follows the rule"
    assert ctrl.active_mode == "gbn", "the wire mode does not"
    assert ctrl.stats.switch_count == 0
    assert ctrl.stats.handshake_count == 1


def test_abandoning_a_switch_resets_the_hysteresis_state():
    ctrl = controller(mode="gbn", switch_high=0.10, hysteresis_count=1,
                      evaluation_interval_segments=4, min_mode_residence_s=2.0)
    for seq in range(4):
        ctrl.on_segment_acked(seq, retransmitted=True, now=3.0)
    assert ctrl.evaluate(3.0) is not None
    ctrl.abandon_switch(now=3.0)
    assert ctrl.mode == "gbn"
    assert ctrl.stats.abandoned_switches == 1
    for seq in range(4, 8):
        ctrl.on_segment_acked(seq, retransmitted=True, now=3.5)
    assert ctrl.evaluate(3.5) is None, "the residence clock restarts after a failure"


def test_epoch_increments_once_per_handshake():
    ctrl = controller()
    assert ctrl.begin_switch() == 1
    assert ctrl.begin_switch() == 2
    assert ctrl.epoch == 2


def test_initial_mode_rejects_a_nonsense_default(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_MODE", "saw")
    with pytest.raises(ValueError, match="DEFAULT_MODE"):
        hybrid.initial_mode()


# ---------------------------------------------------------------------------
# T5.4 — the transfer state survives the swap (HY-05, HY-07)
# ---------------------------------------------------------------------------


def test_rebuilding_a_strategy_preserves_every_unacknowledged_segment():
    """The invariant is structural: payloads live in the transfer state, not in
    the strategy, so there is nothing for a rebuild to drop."""
    segments = [bytes([i]) * 4 for i in range(10)]
    state = SenderTransferState(segments=segments, window_size=4)
    gbn = make_sender_strategy("gbn", state, 1.0)
    gbn.packets_to_send(0.0)
    gbn.on_ack(1, 0.1)                      # 0 and 1 done, window drained to 2

    state.next_seq = state.base             # quiesce, as the sender does
    sr = make_sender_strategy("sr", state, 1.0)

    assert state.base == 2 and state.next_seq == 2
    assert state.acked == {0, 1}
    assert [state.payload(seq) for seq in range(2, 10)] == segments[2:]
    assert sr.packets_to_send(0.2) == [2, 3, 4, 5], "sending resumes at the boundary"


# ---------------------------------------------------------------------------
# T5.7 — the receiving half of the handshake
# ---------------------------------------------------------------------------


def mode_receiver(tmp_path, mode="gbn", expected_seq=4):
    """A receiver positioned mid-transfer, with its sends captured."""
    rx = Receiver(output=tmp_path / "out.bin", host="127.0.0.1", port=0,
                  run_id="mode", log_dir=tmp_path / "logs")
    rx.log = EventLog("mode", "receiver", log_dir=tmp_path / "logs")
    rx.mode = mode
    rx.requested_mode = "hybrid"
    rx.window_size = 4
    sent = []
    rx._send = lambda pkt, address: sent.append(pkt)
    transfer = ReceiverTransferState(expected_seq=expected_seq)
    from protocol.strategy import make_receiver_strategy
    strategy = make_receiver_strategy(mode, transfer, rx.window_size)
    return rx, strategy, transfer, sent


def mode_packet(mode="sr", effective_from_seq=4, epoch=1, reason="MODE_THRESHOLD"):
    return Packet(type=PacketType.MODE, seq=effective_from_seq,
                  payload=pk.ModePayload(mode=mode, effective_from_seq=effective_from_seq,
                                         epoch=epoch, reason=reason).to_payload())


def test_receiver_adopts_a_valid_mode_request_and_echoes_it(tmp_path):
    rx, strategy, transfer, sent = mode_receiver(tmp_path)
    rebuilt = rx._handle_mode(mode_packet(), strategy, transfer, ("127.0.0.1", 1))
    assert rx.mode == "sr" and rx.mode_epoch == 1 and rx.switch_count == 1
    assert rebuilt.name == "SR"
    assert len(sent) == 1 and sent[0].type is PacketType.MODE
    echo = pk.ModePayload.from_payload(sent[0].payload)
    assert (echo.mode, echo.epoch, echo.reason) == ("sr", 1, "ECHO")
    rx.log.close()


def test_a_stale_mode_epoch_is_discarded(tmp_path):
    """HY-07: a MODE from a superseded handshake must not reactivate it."""
    rx, strategy, transfer, sent = mode_receiver(tmp_path)
    rx._handle_mode(mode_packet(epoch=3), strategy, transfer, ("127.0.0.1", 1))
    sent.clear()

    rebuilt = rx._handle_mode(mode_packet(mode="gbn", epoch=2), strategy, transfer,
                              ("127.0.0.1", 1))
    assert rx.mode == "sr", "the superseded request did not take effect"
    assert rx.mode_epoch == 3
    assert sent == [], "and it was not even echoed"
    assert rebuilt is strategy
    rx.log.close()
    reasons = [row["reason"] for row in read_events(rx.log.directory)
               if row["event"] == "MODE"]
    assert any(reason.startswith("STALE") for reason in reasons)


def test_a_repeated_mode_request_is_reacked_without_switching_twice(tmp_path):
    """The echo may be lost; answering again is cheaper than stranding a sender
    that has already drained its window."""
    rx, strategy, transfer, sent = mode_receiver(tmp_path)
    rx._handle_mode(mode_packet(epoch=1), strategy, transfer, ("127.0.0.1", 1))
    sent.clear()
    rx._handle_mode(mode_packet(epoch=1), strategy, transfer, ("127.0.0.1", 1))
    assert rx.switch_count == 1, "the switch happened once"
    assert len(sent) == 1, "and was acknowledged again"
    rx.log.close()


def test_a_mode_request_is_refused_when_the_receiver_is_not_quiescent(tmp_path):
    """The sender only sends MODE once everything it sent is acknowledged. If
    the receiver disagrees about the boundary, switching semantics on top of
    that disagreement is what would corrupt the file — so it refuses."""
    rx, strategy, transfer, sent = mode_receiver(tmp_path, expected_seq=2)
    rebuilt = rx._handle_mode(mode_packet(effective_from_seq=4), strategy, transfer,
                              ("127.0.0.1", 1))
    assert rx.mode == "gbn" and rx.switch_count == 0
    assert sent == [], "refusing means not echoing; the sender then abandons"
    assert rebuilt is strategy
    rx.log.close()


def test_a_mode_request_naming_an_unknown_mode_is_refused(tmp_path):
    rx, strategy, transfer, sent = mode_receiver(tmp_path)
    rx._handle_mode(mode_packet(mode="saw"), strategy, transfer, ("127.0.0.1", 1))
    assert rx.mode == "gbn" and sent == []
    rx.log.close()


def test_a_switch_is_refused_while_the_sr_receive_buffer_holds_segments(tmp_path):
    rx, strategy, transfer, sent = mode_receiver(tmp_path, mode="sr", expected_seq=4)
    strategy.on_data(5, b"ahead of the gap")
    assert strategy.buffered == [5]
    rx._handle_mode(mode_packet(mode="gbn"), strategy, transfer, ("127.0.0.1", 1))
    assert rx.mode == "sr" and sent == []
    rx.log.close()


# ---------------------------------------------------------------------------
# T5.8 — integration
# ---------------------------------------------------------------------------


def run_hybrid(tmp_path, *, mode="hybrid", size, window=8, rto=0.25, run_id="hy",
               hybrid_settings=None, sender_impairment=None, idle_timeout=20.0,
               patch_receiver=None):
    """One hybrid transfer over loopback. Returns (sender, receiver, src, out)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = make_file(tmp_path, size=size)
    output = tmp_path / "received.bin"
    logs = tmp_path / "logs"

    rx = Receiver(output=output, host="127.0.0.1", port=0, run_id=run_id,
                  log_dir=logs, idle_timeout=idle_timeout, linger=0.3)
    if patch_receiver is not None:
        patch_receiver(rx)
    port = rx.bind()

    failures = []

    def receive():
        try:
            rx.run()
        except ReceiverError as exc:
            failures.append(exc)

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()

    tx = Sender(path=source, host="127.0.0.1", port=port, mode=mode, window=window,
                run_id=run_id, log_dir=logs, rto=rto, impairment=sender_impairment,
                hybrid_settings=hybrid_settings or settings())
    error = None
    try:
        tx.run()
    except TransferError as exc:
        error = exc
    finally:
        thread.join(timeout=60)

    assert not thread.is_alive(), "receiver thread did not finish"
    return tx, rx, source, output, error, failures


def switch_rows(log):
    return [row for row in read_events(log.directory) if row["event"] == "SWITCH"]


def test_transfer_switches_gbn_to_sr_and_the_hash_matches(tmp_path):
    """T5.4's done-when: a switch mid-transfer preserves every unacked segment
    and the final hash matches."""
    tx, rx, source, output, error, failures = run_hybrid(
        tmp_path, size=config.SEGMENT_SIZE * 60,
        sender_impairment=Impairment(loss_rate=0.10, seed=7),
        hybrid_settings=settings(switch_high=0.02, switch_low=0.0,
                                 hysteresis_count=1, evaluation_interval_segments=5),
        run_id="gbn_to_sr")

    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source), "SILENT CORRUPTION across a switch"
    assert tx.active_mode == "sr" and rx.mode == "sr"
    assert tx.controller.stats.switch_count >= 1
    assert rx.switch_count == tx.controller.stats.switch_count

    for log in (tx.log, rx.log):
        rows = switch_rows(log)
        assert rows, f"{log.endpoint} recorded no SWITCH event"
        assert rows[0]["mode"] == "sr"
        assert "from=gbn" in rows[0]["reason"]
        assert "epoch=1" in rows[0]["reason"]


def test_transfer_switches_sr_to_gbn_and_the_hash_matches(tmp_path, monkeypatch):
    """The other direction, started from SR via config.DEFAULT_MODE — which is
    how both endpoints agree on a starting mode without a field on the wire."""
    monkeypatch.setattr(config, "DEFAULT_MODE", "SR")
    tx, rx, source, output, error, failures = run_hybrid(
        tmp_path, size=config.SEGMENT_SIZE * 40,
        hybrid_settings=settings(switch_high=0.95, switch_low=0.90,
                                 hysteresis_count=1, evaluation_interval_segments=5),
        run_id="sr_to_gbn")

    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)
    assert tx.active_mode == "gbn" and rx.mode == "gbn"
    assert switch_rows(tx.log)[0]["mode"] == "gbn"
    assert "from=sr" in switch_rows(rx.log)[0]["reason"]


def test_switch_events_carry_the_loss_estimate_and_epoch(tmp_path):
    """T5.5 / HY-08: timestamp, old and new mode, loss estimate, reason, epoch."""
    tx, _, _, _, error, failures = run_hybrid(
        tmp_path, size=config.SEGMENT_SIZE * 40,
        sender_impairment=Impairment(loss_rate=0.10, seed=11),
        hybrid_settings=settings(switch_high=0.02, switch_low=0.0,
                                 hysteresis_count=1, evaluation_interval_segments=5),
        run_id="switch_log")
    assert error is None and not failures

    row = switch_rows(tx.log)[0]
    assert float(row["timestamp"]) > 0
    assert row["mode"] == "sr"
    assert 0.0 <= float(row["loss_estimate"]) <= 1.0
    assert row["reason"].startswith("MODE_THRESHOLD;from=gbn;epoch=")
    assert row["sequence"], "the boundary sequence is recorded"


def test_a_transfer_inside_the_dead_band_never_switches(tmp_path):
    """CC-04: thresholds straddling the observed loss leave the mode alone, and
    the transfer still completes correctly."""
    tx, rx, source, output, error, failures = run_hybrid(
        tmp_path, size=config.SEGMENT_SIZE * 40,
        sender_impairment=Impairment(loss_rate=0.05, seed=3),
        hybrid_settings=settings(switch_high=0.999, switch_low=0.0,
                                 hysteresis_count=1, evaluation_interval_segments=5),
        run_id="dead_band")

    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)
    assert tx.controller.evaluations > 0, "the rule was actually exercised"
    assert tx.controller.stats.switch_count == 0
    assert switch_rows(tx.log) == [] and switch_rows(rx.log) == []
    assert tx.active_mode == "gbn" and rx.mode == "gbn"


def test_repeated_switches_do_not_corrupt_the_transfer(tmp_path):
    """M7's completion condition: runtime switching with no corruption across
    repeated switches. A loss schedule drives the condition up and back down."""
    tx, rx, source, output, error, failures = run_hybrid(
        tmp_path, size=config.SEGMENT_SIZE * 120, rto=0.2,
        sender_impairment=Impairment(loss_rate=0.15, seed=5,
                                     loss_schedule=((1.5, 0.0),)),
        hybrid_settings=settings(switch_high=0.05, switch_low=0.02,
                                 hysteresis_count=1, evaluation_interval_segments=5),
        run_id="repeated")

    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)
    assert rx.switch_count == tx.controller.stats.switch_count
    assert len(switch_rows(tx.log)) == len(switch_rows(rx.log))
    # Every switch the sender recorded is one the receiver adopted, in the same
    # order — a half-switched transfer would show up here as a mismatch.
    assert ([row["mode"] for row in switch_rows(tx.log)]
            == [row["mode"] for row in switch_rows(rx.log)])


def test_a_failed_handshake_leaves_the_transfer_in_one_valid_mode(tmp_path, monkeypatch):
    """T5.7's last case, and specs.md §13: a switch that cannot be negotiated is
    abandoned, and the transfer finishes in the mode it was already in."""
    monkeypatch.setattr(config, "CONTROL_RETRY_TIMEOUT_S", 0.2)

    def deaf_to_mode(rx):
        rx._handle_mode = lambda pkt, strategy, transfer, address: strategy

    tx, rx, source, output, error, failures = run_hybrid(
        tmp_path, size=config.SEGMENT_SIZE * 40,
        sender_impairment=Impairment(loss_rate=0.08, seed=13),
        hybrid_settings=settings(switch_high=0.02, switch_low=0.0,
                                 hysteresis_count=1, evaluation_interval_segments=5),
        run_id="abandoned", patch_receiver=deaf_to_mode)

    assert error is None and not failures, "an abandoned switch is not a failed transfer"
    assert sha256_of(output) == sha256_of(source)
    assert tx.controller.stats.abandoned_switches >= 1
    assert tx.controller.stats.switch_count == 0
    assert tx.active_mode == "gbn" and rx.mode == "gbn", "both sides, one mode"
    assert switch_rows(tx.log) == []
    reasons = [row["reason"] for row in read_events(tx.log.directory)
               if row["event"] == "TIMEOUT"]
    assert any("MODE_HANDSHAKE_ABANDONED" in reason for reason in reasons)


def test_fixed_hybrid_runs_the_machinery_without_changing_mode(tmp_path):
    """T5.6: the switching-overhead control drains and handshakes at the same
    moments as the hybrid, and stays in one mode throughout."""
    tx, rx, source, output, error, failures = run_hybrid(
        tmp_path, mode="fixed-hybrid", size=config.SEGMENT_SIZE * 60,
        sender_impairment=Impairment(loss_rate=0.10, seed=7),
        hybrid_settings=settings(switch_high=0.02, switch_low=0.0,
                                 hysteresis_count=1, evaluation_interval_segments=5),
        run_id="fixed_hybrid")

    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)
    assert tx.active_mode == "gbn" and rx.mode == "gbn"
    assert tx.controller.stats.handshake_count >= 1, "the MODE exchange did run"
    assert tx.controller.stats.switch_count == 0, "but nothing switched"

    rows = switch_rows(tx.log)
    assert rows and all("FIXED_HYBRID_NOOP" in row["reason"] for row in rows)
    assert all(row["mode"] == "gbn" for row in rows)


def test_hybrid_summary_reports_the_controller_state(tmp_path):
    """specs.md §10 and §20 both land in summary.json, so a run can be read back
    without re-deriving the controller's state from the event log."""
    tx, _, _, _, error, failures = run_hybrid(
        tmp_path, size=config.SEGMENT_SIZE * 40,
        sender_impairment=Impairment(loss_rate=0.10, seed=17),
        hybrid_settings=settings(switch_high=0.02, switch_low=0.0,
                                 hysteresis_count=1, evaluation_interval_segments=5),
        run_id="summary")
    assert error is None and not failures

    import json
    document = json.loads((tx.log.directory / "summary.json").read_text(encoding="utf-8"))
    controller_state = document["controller"]
    assert controller_state["active_mode"] == "sr"
    assert controller_state["switching_enabled"] is True
    assert controller_state["settings"]["switch_high"] == 0.02
    assert document["metrics"]["switch_count"] >= 1
    assert document["metrics"]["gbn_residence_s"] > 0.0
    assert document["metrics"]["sr_residence_s"] > 0.0


def test_every_hybrid_run_metric_is_derivable_from_the_event_log(tmp_path):
    """CC-06: the switch count in summary.json must be recoverable from
    events.csv alone, or the log is not sufficient to explain the result."""
    tx, _, _, _, error, failures = run_hybrid(
        tmp_path, size=config.SEGMENT_SIZE * 40,
        sender_impairment=Impairment(loss_rate=0.10, seed=19),
        hybrid_settings=settings(switch_high=0.02, switch_low=0.0,
                                 hysteresis_count=1, evaluation_interval_segments=5),
        run_id="derivable")
    assert error is None and not failures

    rows = read_events(tx.log.directory)
    real_switches = [row for row in rows
                     if row["event"] == "SWITCH" and "FIXED_HYBRID_NOOP" not in row["reason"]]
    assert len(real_switches) == tx.controller.stats.switch_count
    assert sum(1 for row in rows if row["event"] == "RETX") == tx.data_retransmitted
    assert sum(1 for row in rows if row["event"] == "SEND") == tx.data_sent


def test_the_cli_accepts_both_hybrid_modes():
    """Phase 5 implements them, so neither may still be reported as pending."""
    import sender as sender_mod
    assert sender_mod.MODE_TASKS["hybrid"] is None
    assert sender_mod.MODE_TASKS["fixed-hybrid"] is None
