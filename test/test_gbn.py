"""Go-Back-N tests (T3.6 unit, T3.7 integration).

T3.6 requires cumulative ACK processing, base non-regression, and timeout
retransmission of the correct range. T3.7 requires a GBN transfer at 0% loss
with a matching hash.

Loss is not simulated here — the simulator arrives in T4.1, and controlled-loss
GBN validation is T4.8. What *is* exercised is every GBN rule that can be driven
directly: the window, the single timer, the range retransmission, and the ACK
convention frozen as D5.
"""

import hashlib
import threading

import pytest

import config
from protocol.gbn import GbnReceiver, GbnSender
from protocol.strategy import (ReceiverTransferState, SenderTransferState,
                               make_receiver_strategy, make_sender_strategy)
from receiver import Receiver, ReceiverError
from sender import Sender

from test_transfer import make_file, read_events, sha256_of


def sender_for(total=8, window=4, rto=1.0):
    state = SenderTransferState(segments=[bytes([i]) for i in range(total)],
                                window_size=window)
    return GbnSender(state, rto), state


# ---------------------------------------------------------------------------
# Window and the single timer (T3.2)
# ---------------------------------------------------------------------------


def test_sender_fills_the_window_but_no_further():
    strategy, state = sender_for(total=8, window=4)
    assert strategy.packets_to_send(0.0) == [0, 1, 2, 3]
    assert state.next_seq == 4
    assert strategy.packets_to_send(0.0) == [], "window is full"


def test_window_never_exceeds_the_remaining_segments():
    strategy, _ = sender_for(total=3, window=8)
    assert strategy.packets_to_send(0.0) == [0, 1, 2]


def test_window_reopens_as_the_base_advances():
    strategy, state = sender_for(total=8, window=4)
    strategy.packets_to_send(0.0)
    strategy.on_ack(1, 0.1)                      # 0 and 1 acknowledged
    assert state.base == 2
    assert strategy.packets_to_send(0.1) == [4, 5]


def test_a_single_timer_covers_the_whole_window():
    """specs.md §7.1: one timer, on the oldest outstanding packet."""
    strategy, _ = sender_for(total=8, window=4)
    strategy.packets_to_send(0.0)
    starts = [e for e in strategy.drain_timer_events() if e.kind == "TIMER_START"]
    assert len(starts) == 1 and starts[0].seq == 0


def test_timer_moves_to_the_new_oldest_on_a_partial_ack():
    strategy, _ = sender_for(total=8, window=4)
    strategy.packets_to_send(0.0)
    strategy.drain_timer_events()
    strategy.on_ack(1, 0.1)
    events = [(e.kind, e.seq) for e in strategy.drain_timer_events()]
    assert events == [("TIMER_STOP", 0), ("TIMER_START", 2)]


def test_timer_stops_when_the_window_drains():
    strategy, _ = sender_for(total=4, window=4)
    strategy.packets_to_send(0.0)
    strategy.drain_timer_events()
    strategy.on_ack(3, 0.1)
    assert [(e.kind, e.seq) for e in strategy.drain_timer_events()] == [("TIMER_STOP", 0)]
    assert strategy.next_timeout(0.1) is None


def test_next_timeout_counts_down_from_the_arming_instant():
    strategy, _ = sender_for(total=4, window=4, rto=0.5)
    strategy.packets_to_send(10.0)
    assert strategy.next_timeout(10.0) == pytest.approx(0.5)
    assert strategy.next_timeout(10.4) == pytest.approx(0.1)
    assert strategy.next_timeout(10.9) == 0.0


def test_window_size_must_be_at_least_one():
    state = SenderTransferState(segments=[b"a"], window_size=0)
    with pytest.raises(ValueError, match="window_size"):
        GbnSender(state, 1.0)


# ---------------------------------------------------------------------------
# Cumulative ACK processing (T3.3, GBN-01, GBN-02, GBN-03)
# ---------------------------------------------------------------------------


def test_one_ack_acknowledges_everything_up_to_it():
    """GBN-01: ACK n means 0..n arrived, so a lost ACK is repaired by the next."""
    strategy, state = sender_for(total=8, window=8)
    strategy.packets_to_send(0.0)
    result = strategy.on_ack(4, 0.1)
    assert result.newly_acked == [0, 1, 2, 3, 4]
    assert state.base == 5
    assert state.acked == {0, 1, 2, 3, 4}


def test_valid_ack_advances_the_base():
    strategy, state = sender_for(total=8, window=8)
    strategy.packets_to_send(0.0)
    for expected_base, ack in [(1, 0), (2, 1), (6, 5)]:
        strategy.on_ack(ack, 0.1)
        assert state.base == expected_base


@pytest.mark.parametrize("stale_ack", [0, 1, 2])
def test_stale_ack_never_moves_the_base_backward(stale_ack):
    """GBN-03 — the invariant that makes duplicate ACKs harmless."""
    strategy, state = sender_for(total=8, window=8)
    strategy.packets_to_send(0.0)
    strategy.on_ack(3, 0.1)
    assert state.base == 4

    result = strategy.on_ack(stale_ack, 0.2)
    assert state.base == 4, "a stale ACK moved the base backward"
    assert result.newly_acked == []
    assert result.stale and result.duplicate


def test_duplicate_of_the_latest_ack_is_ignored():
    strategy, state = sender_for(total=8, window=8)
    strategy.packets_to_send(0.0)
    strategy.on_ack(3, 0.1)
    result = strategy.on_ack(3, 0.2)
    assert state.base == 4 and result.newly_acked == []
    assert result.duplicate


def test_ack_for_a_segment_never_sent_is_ignored():
    strategy, state = sender_for(total=8, window=2)
    strategy.packets_to_send(0.0)            # only 0 and 1 are outstanding
    result = strategy.on_ack(5, 0.1)
    assert state.base == 0 and result.newly_acked == []
    assert result.stale


def test_base_never_passes_next_seq():
    strategy, state = sender_for(total=8, window=3)
    strategy.packets_to_send(0.0)
    strategy.on_ack(2, 0.1)
    assert state.base == state.next_seq == 3
    assert state.is_quiescent()


def test_transfer_completes_only_when_every_segment_is_acked():
    strategy, state = sender_for(total=4, window=4)
    strategy.packets_to_send(0.0)
    strategy.on_ack(2, 0.1)
    assert not strategy.all_acked()
    strategy.on_ack(3, 0.2)
    assert strategy.all_acked()


# ---------------------------------------------------------------------------
# Timeout and range retransmission (T3.4, GBN-04)
# ---------------------------------------------------------------------------


def test_timeout_retransmits_the_entire_outstanding_range():
    """GBN-04, and the defining cost of GBN: correctly received later segments
    go again because one earlier segment did not arrive."""
    strategy, _ = sender_for(total=8, window=4, rto=0.5)
    strategy.packets_to_send(0.0)
    assert strategy.on_timeout(0.5) == [0, 1, 2, 3]


def test_timeout_range_starts_at_the_base_not_at_zero():
    strategy, state = sender_for(total=8, window=4, rto=0.5)
    strategy.packets_to_send(0.0)
    strategy.on_ack(1, 0.1)                  # base -> 2
    strategy.packets_to_send(0.1)            # 4, 5 join the window
    assert state.next_seq == 6
    assert strategy.on_timeout(0.7) == [2, 3, 4, 5]


def test_no_retransmission_before_the_rto_elapses():
    strategy, _ = sender_for(total=4, window=4, rto=0.5)
    strategy.packets_to_send(10.0)
    assert strategy.on_timeout(10.49) == []
    assert strategy.on_timeout(10.50) == [0, 1, 2, 3]


def test_timeout_restarts_the_timer_so_it_can_fire_again():
    strategy, _ = sender_for(total=4, window=4, rto=0.5)
    strategy.packets_to_send(0.0)
    strategy.drain_timer_events()
    strategy.on_timeout(0.5)
    assert [(e.kind, e.seq) for e in strategy.drain_timer_events()] == [
        ("TIMER_STOP", 0), ("TIMER_START", 0)]
    assert strategy.on_timeout(0.9) == []
    assert strategy.on_timeout(1.0) == [0, 1, 2, 3]


def test_no_timeout_when_nothing_is_outstanding():
    strategy, _ = sender_for(total=4, window=4, rto=0.5)
    assert strategy.on_timeout(99.0) == []
    strategy.packets_to_send(0.0)
    strategy.on_ack(3, 0.1)
    assert strategy.on_timeout(99.0) == []


# ---------------------------------------------------------------------------
# Receiver rules and D5 (T3.5, T3.1)
# ---------------------------------------------------------------------------


def test_receiver_delivers_in_order_and_acks_each():
    strategy = GbnReceiver(ReceiverTransferState())
    for seq in range(3):
        result = strategy.on_data(seq, bytes([seq]))
        assert result.deliver == [(seq, bytes([seq]))]
        assert result.ack == seq, "D5: ACK carries the highest in-order sequence"


def test_receiver_discards_out_of_order_and_reacks_the_last_in_order():
    """GBN keeps no buffer — a correctly received future segment is thrown away."""
    strategy = GbnReceiver(ReceiverTransferState())
    strategy.on_data(0, b"a")
    strategy.on_data(1, b"b")

    result = strategy.on_data(5, b"future")
    assert result.deliver == [], "GBN must not buffer"
    assert result.ack == 1, "re-ACK the last in-order sequence"
    assert not result.duplicate


def test_receiver_reacks_a_duplicate_of_an_already_delivered_segment():
    strategy = GbnReceiver(ReceiverTransferState())
    strategy.on_data(0, b"a")
    strategy.on_data(1, b"b")
    result = strategy.on_data(0, b"a")
    assert result.deliver == [] and result.duplicate
    assert result.ack == 1


def test_receiver_sends_no_ack_before_the_first_in_order_segment():
    """D5's one awkward corner: with "highest in-order received" there is no
    value meaning "nothing yet", since 0 already means segment 0 arrived. The
    receiver stays silent and the sender's timer covers it."""
    strategy = GbnReceiver(ReceiverTransferState())
    result = strategy.on_data(3, b"future")
    assert result.ack is None
    assert result.deliver == []


def test_receiver_expected_seq_only_advances_in_order():
    state = ReceiverTransferState()
    strategy = GbnReceiver(state)
    strategy.on_data(2, b"c")
    strategy.on_data(1, b"b")
    assert state.expected_seq == 0
    strategy.on_data(0, b"a")
    assert state.expected_seq == 1


# ---------------------------------------------------------------------------
# Sender and receiver agree (the D5 round trip)
# ---------------------------------------------------------------------------


def test_sender_and_receiver_agree_on_ack_semantics():
    """The point of freezing D5: both endpoints read the same ACK identically."""
    tx, tx_state = sender_for(total=6, window=3)
    rx = GbnReceiver(ReceiverTransferState())

    delivered = []
    while not tx.all_acked():
        for seq in tx.packets_to_send(0.0):
            result = rx.on_data(seq, bytes([seq]))
            delivered.extend(s for s, _ in result.deliver)
            if result.ack is not None:
                tx.on_ack(result.ack, 0.0)
    assert delivered == [0, 1, 2, 3, 4, 5]
    assert tx_state.base == 6


def test_go_back_n_recovers_a_dropped_segment_without_the_network():
    """Drive the loss by hand: segment 2 never reaches the receiver."""
    tx, _ = sender_for(total=5, window=5, rto=0.1)
    rx = GbnReceiver(ReceiverTransferState())

    first_pass = tx.packets_to_send(0.0)
    assert first_pass == [0, 1, 2, 3, 4]
    for seq in first_pass:
        if seq == 2:
            continue                                  # dropped in flight
        result = rx.on_data(seq, bytes([seq]))
        if result.ack is not None:
            tx.on_ack(result.ack, 0.0)

    # 0 and 1 were delivered; 3 and 4 were discarded for want of 2.
    assert rx.state.expected_seq == 2

    resent = tx.on_timeout(0.2)
    assert resent == [2, 3, 4], "GBN goes back to base and resends the range"
    for seq in resent:
        result = rx.on_data(seq, bytes([seq]))
        if result.ack is not None:
            tx.on_ack(result.ack, 0.3)
    assert tx.all_acked()
    assert rx.state.expected_seq == 5


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_builds_gbn_by_name():
    state = SenderTransferState(segments=[b"a"], window_size=4)
    assert make_sender_strategy("gbn", state, 1.0).name == "GBN"
    assert make_receiver_strategy("gbn", ReceiverTransferState()).name == "GBN"


def test_factory_rejects_an_unimplemented_mode():
    state = SenderTransferState(segments=[b"a"], window_size=4)
    with pytest.raises(ValueError, match="no sender strategy"):
        make_sender_strategy("sr", state, 1.0)


# ---------------------------------------------------------------------------
# T3.7 — integration at 0% loss
# ---------------------------------------------------------------------------


def run_gbn_transfer(tmp_path, *, size, window, run_id="gbn"):
    source = make_file(tmp_path, size=size)
    output = tmp_path / "received.bin"
    logs = tmp_path / "logs"

    rx = Receiver(output=output, host="127.0.0.1", port=0, run_id=run_id,
                  log_dir=logs, idle_timeout=10.0, linger=0.2)
    port = rx.bind()

    failures = []

    def receive():
        try:
            rx.run()
        except ReceiverError as exc:
            failures.append(exc)

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()

    tx = Sender(path=source, host="127.0.0.1", port=port, mode="gbn",
                window=window, run_id=run_id, log_dir=logs, rto=0.5)
    try:
        tx.run()
    finally:
        thread.join(timeout=20)

    assert not thread.is_alive()
    if failures:
        raise failures[0]
    return tx, rx, source, output


def test_gbn_transfers_a_file_at_zero_loss(tmp_path):
    """T3.7 done-when."""
    tx, rx, source, output = run_gbn_transfer(tmp_path, size=config.SEGMENT_SIZE * 10,
                                              window=4)
    assert sha256_of(output) == sha256_of(source)
    assert tx.state == "COMPLETE" and rx.state == "COMPLETE"
    assert tx.data_retransmitted == 0, "no loss, so nothing should be resent"


@pytest.mark.parametrize("window", [1, 2, 8, 32])
def test_gbn_transfers_at_several_window_sizes(tmp_path, window):
    _, _, source, output = run_gbn_transfer(tmp_path, size=config.SEGMENT_SIZE * 5 + 13,
                                            window=window, run_id=f"gbn_w{window}")
    assert output.read_bytes() == source.read_bytes()


def test_gbn_window_of_one_behaves_like_stop_and_wait(tmp_path):
    tx, _, source, output = run_gbn_transfer(tmp_path, size=config.SEGMENT_SIZE * 3,
                                             window=1, run_id="gbn_w1")
    assert sha256_of(output) == sha256_of(source)
    assert tx.data_sent == 3


def test_gbn_run_logs_timer_events(tmp_path):
    """TO-02: timer start/stop are recorded, not just retransmissions."""
    tx, _, _, _ = run_gbn_transfer(tmp_path, size=config.SEGMENT_SIZE * 4,
                                   window=2, run_id="gbn_timers")
    events = [row["event"] for row in read_events(tx.log.directory)]
    assert "TIMER_START" in events and "TIMER_STOP" in events


def test_gbn_logs_record_the_mode(tmp_path):
    tx, rx, _, _ = run_gbn_transfer(tmp_path, size=config.SEGMENT_SIZE * 2,
                                    window=4, run_id="gbn_mode")
    for log in (tx.log, rx.log):
        modes = {row["mode"] for row in read_events(log.directory) if row["mode"]}
        assert modes == {"gbn"}, f"{log.endpoint} logged modes {modes}"


def test_gbn_pipelines_rather_than_waiting_per_segment(tmp_path):
    """A window larger than one must put several segments in flight at once.

    Checked through the log rather than by timing: consecutive SEND events with
    no intervening ACK is exactly what pipelining looks like on the wire.
    """
    tx, _, _, _ = run_gbn_transfer(tmp_path, size=config.SEGMENT_SIZE * 8,
                                   window=8, run_id="gbn_pipeline")
    events = [row["event"] for row in read_events(tx.log.directory)]
    sends = events.index("SEND")
    burst = 0
    for event in events[sends:]:
        if event != "SEND":
            break
        burst += 1
    assert burst > 1, "segments were sent one at a time despite a window of 8"
