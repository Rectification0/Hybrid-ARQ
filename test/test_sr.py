"""Selective Repeat tests (T4.7), and the T4.8 failure tests for both baselines.

T4.7 requires individual ACK processing, out-of-order buffering with gap-fill
delivery, duplicate handling, and the half-sequence-space window assertion.

T4.8 requires, against **both** GBN and SR: single loss, multiple consecutive
losses, random loss, ACK loss, artificial delay, and packet corruption — each
producing a matching hash or a clearly reported failure, never silent
corruption. Those run over real sockets through the simulator.
"""

import hashlib
import threading

import pytest

import config
from network.simulator import Impairment
from protocol.packet import MAX_SEQUENCE
from protocol.sr import MAX_SR_WINDOW, SrReceiver, SrSender
from protocol.strategy import (ReceiverTransferState, SenderTransferState,
                               make_receiver_strategy, make_sender_strategy)
from receiver import Receiver, ReceiverError
from sender import Sender, TransferError

from test_transfer import make_file, read_events, sha256_of


def sr_sender(total=8, window=4, rto=1.0):
    state = SenderTransferState(segments=[bytes([i]) for i in range(total)],
                                window_size=window)
    return SrSender(state, rto), state


def sr_receiver(window=4):
    state = ReceiverTransferState()
    return SrReceiver(state, window), state


# ---------------------------------------------------------------------------
# Individual ACK processing (T4.7, D6, SR-01)
# ---------------------------------------------------------------------------


def test_an_ack_acknowledges_only_the_segment_it_names():
    """D6, and the whole difference from D5: ACK 3 says nothing about 0..2."""
    strategy, state = sr_sender(total=8, window=8)
    strategy.packets_to_send(0.0)
    result = strategy.on_ack(3, 0.1)
    assert result.newly_acked == [3]
    assert state.acked == {3}
    assert state.base == 0, "base cannot move while 0 is still missing"


def test_base_advances_over_the_contiguous_run_only():
    strategy, state = sr_sender(total=8, window=8)
    strategy.packets_to_send(0.0)
    for seq in (1, 2, 4):
        strategy.on_ack(seq, 0.1)
    assert state.base == 0
    strategy.on_ack(0, 0.2)
    assert state.base == 3, "0,1,2 are contiguous; 3 is still missing"
    strategy.on_ack(3, 0.3)
    assert state.base == 5, "filling 3 also retires the already-acked 4"


def test_duplicate_ack_is_idempotent():
    strategy, state = sr_sender(total=8, window=8)
    strategy.packets_to_send(0.0)
    strategy.on_ack(2, 0.1)
    result = strategy.on_ack(2, 0.2)
    assert result.newly_acked == [] and result.duplicate
    assert state.acked == {2}


def test_ack_below_base_is_stale():
    strategy, state = sr_sender(total=8, window=8)
    strategy.packets_to_send(0.0)
    strategy.on_ack(0, 0.1)
    assert state.base == 1
    result = strategy.on_ack(0, 0.2)
    assert result.stale and state.base == 1


def test_ack_for_a_segment_never_sent_is_refused():
    strategy, state = sr_sender(total=8, window=2)
    strategy.packets_to_send(0.0)
    result = strategy.on_ack(6, 0.1)
    assert result.stale and state.acked == set()


def test_window_slides_as_the_base_advances():
    strategy, state = sr_sender(total=8, window=3)
    assert strategy.packets_to_send(0.0) == [0, 1, 2]
    strategy.on_ack(1, 0.1)
    assert strategy.packets_to_send(0.1) == [], "base is still 0; window is full"
    strategy.on_ack(0, 0.2)
    assert state.base == 2
    assert strategy.packets_to_send(0.2) == [3, 4]


# ---------------------------------------------------------------------------
# Per-segment timers and single-segment retransmission (T4.5, SR-10)
# ---------------------------------------------------------------------------


def test_every_outstanding_segment_gets_its_own_timer():
    """The structural difference from GBN: N timers, not one."""
    strategy, _ = sr_sender(total=8, window=4)
    strategy.packets_to_send(0.0)
    starts = [e.seq for e in strategy.drain_timer_events() if e.kind == "TIMER_START"]
    assert starts == [0, 1, 2, 3]
    assert strategy.outstanding == [0, 1, 2, 3]


def test_timeout_retransmits_only_the_segment_that_timed_out():
    """SR-10 — the property the whole hybrid argument rests on."""
    strategy, _ = sr_sender(total=8, window=4, rto=0.5)
    strategy.packets_to_send(0.0)
    strategy.on_ack(0, 0.1)
    strategy.on_ack(2, 0.1)
    strategy.on_ack(3, 0.1)
    assert strategy.on_timeout(0.6) == [1], "only the unacked segment goes again"


def test_acking_a_segment_cancels_only_its_own_timer():
    strategy, _ = sr_sender(total=8, window=4)
    strategy.packets_to_send(0.0)
    strategy.drain_timer_events()
    strategy.on_ack(2, 0.1)
    assert [(e.kind, e.seq) for e in strategy.drain_timer_events()] == [("TIMER_STOP", 2)]
    assert strategy.outstanding == [0, 1, 3]


def test_timers_expire_independently():
    strategy, _ = sr_sender(total=8, window=4, rto=0.5)
    strategy.packets_to_send(0.0)       # 0..3 armed at t=0
    strategy.on_ack(0, 0.1)
    strategy.on_ack(1, 0.1)
    strategy.packets_to_send(0.3)       # 4, 5 armed at t=0.3
    assert strategy.on_timeout(0.55) == [2, 3], "4 and 5 are not due yet"
    assert strategy.on_timeout(0.85) == [4, 5]


def test_next_timeout_tracks_the_earliest_deadline():
    strategy, _ = sr_sender(total=8, window=4, rto=0.5)
    strategy.packets_to_send(10.0)
    assert strategy.next_timeout(10.0) == pytest.approx(0.5)
    strategy.on_ack(0, 10.1)
    assert strategy.next_timeout(10.1) == pytest.approx(0.4)
    for seq in (1, 2, 3):
        strategy.on_ack(seq, 10.2)
    assert strategy.next_timeout(10.2) is None


def test_retransmission_restarts_only_that_timer():
    strategy, _ = sr_sender(total=4, window=4, rto=0.5)
    strategy.packets_to_send(0.0)
    strategy.on_ack(0, 0.1)
    strategy.on_ack(1, 0.1)
    strategy.on_ack(3, 0.1)
    strategy.drain_timer_events()
    assert strategy.on_timeout(0.6) == [2]
    assert [(e.kind, e.seq) for e in strategy.drain_timer_events()] == [
        ("TIMER_STOP", 2), ("TIMER_START", 2)]
    assert strategy.on_timeout(0.9) == []
    assert strategy.on_timeout(1.2) == [2]


# ---------------------------------------------------------------------------
# Receiver buffering and gap fill (T4.6, SR-04 … SR-09)
# ---------------------------------------------------------------------------


def test_out_of_order_segment_is_buffered_and_acked_individually():
    strategy, state = sr_receiver(window=4)
    result = strategy.on_data(2, b"c")
    assert result.deliver == [], "nothing deliverable while 0 is missing"
    assert result.ack == 2, "SR-07: acknowledge it anyway, it did arrive"
    assert result.buffered
    assert state.expected_seq == 0
    assert strategy.buffered == [2]


def test_gap_fill_delivers_the_whole_contiguous_run_at_once():
    """SR-08 — the payoff for buffering: one arrival releases everything behind it."""
    strategy, state = sr_receiver(window=4)
    strategy.on_data(1, b"b")
    strategy.on_data(2, b"c")
    strategy.on_data(3, b"d")
    assert state.expected_seq == 0

    result = strategy.on_data(0, b"a")
    assert result.deliver == [(0, b"a"), (1, b"b"), (2, b"c"), (3, b"d")]
    assert state.expected_seq == 4
    assert strategy.buffered == [], "buffer drains as it delivers"


def test_partial_gap_fill_delivers_only_what_is_contiguous():
    strategy, state = sr_receiver(window=8)
    strategy.on_data(1, b"b")
    strategy.on_data(3, b"d")
    result = strategy.on_data(0, b"a")
    assert result.deliver == [(0, b"a"), (1, b"b")]
    assert state.expected_seq == 2
    assert strategy.buffered == [3], "3 waits for 2"


def test_in_order_arrival_delivers_immediately():
    strategy, state = sr_receiver(window=4)
    result = strategy.on_data(0, b"a")
    assert result.deliver == [(0, b"a")] and result.ack == 0
    assert not result.buffered and state.expected_seq == 1


def test_duplicate_of_a_buffered_segment_is_reacked_not_rebuffered():
    strategy, _ = sr_receiver(window=4)
    strategy.on_data(2, b"c")
    result = strategy.on_data(2, b"c")
    assert result.duplicate and result.ack == 2
    assert result.deliver == []
    assert strategy.buffered == [2]


def test_duplicate_of_a_delivered_segment_is_reacked():
    """SR-09. Re-ACKing matters more in SR than GBN: with per-segment ACKs a
    lost ACK is not covered by the next one, so silence means retransmitting
    until the retry budget runs out."""
    strategy, state = sr_receiver(window=4)
    strategy.on_data(0, b"a")
    strategy.on_data(1, b"b")
    result = strategy.on_data(0, b"a")
    assert result.duplicate and result.ack == 0
    assert result.deliver == [] and state.expected_seq == 2


def test_segment_beyond_the_receive_window_is_neither_buffered_nor_acked():
    """Under D6 an ACK asserts possession. Acknowledging a segment that was
    never accepted would let the sender retire data the receiver does not have."""
    strategy, state = sr_receiver(window=4)
    result = strategy.on_data(9, b"far")
    assert result.deliver == [] and result.ack is None
    assert strategy.buffered == [] and state.expected_seq == 0


def test_receive_window_edges_are_inclusive_at_the_bottom_only():
    strategy, _ = sr_receiver(window=4)
    assert strategy.on_data(3, b"in").ack == 3        # last in-window sequence
    assert strategy.on_data(4, b"out").ack is None    # first beyond it


# ---------------------------------------------------------------------------
# The half-sequence-space constraint (T4.7)
# ---------------------------------------------------------------------------


def test_window_may_not_exceed_half_the_sequence_space():
    state = SenderTransferState(segments=[b"a"], window_size=MAX_SR_WINDOW + 1)
    with pytest.raises(ValueError, match="half the sequence space"):
        SrSender(state, 1.0)
    with pytest.raises(ValueError, match="half the sequence space"):
        SrReceiver(ReceiverTransferState(), MAX_SR_WINDOW + 1)


def test_a_window_of_exactly_half_the_sequence_space_is_allowed():
    state = SenderTransferState(segments=[b"a"], window_size=MAX_SR_WINDOW)
    assert SrSender(state, 1.0).name == "SR"


def test_half_the_sequence_space_is_what_it_says():
    assert MAX_SR_WINDOW == (MAX_SEQUENCE + 1) // 2 == 2 ** 31


@pytest.mark.parametrize("bad", [0, -1])
def test_window_below_one_is_refused(bad):
    state = SenderTransferState(segments=[b"a"], window_size=bad)
    with pytest.raises(ValueError, match="window_size must be"):
        SrSender(state, 1.0)


def test_factory_builds_sr_by_name():
    state = SenderTransferState(segments=[b"a"], window_size=4)
    assert make_sender_strategy("sr", state, 1.0).name == "SR"
    assert make_receiver_strategy("sr", ReceiverTransferState(), 4).name == "SR"


# ---------------------------------------------------------------------------
# Sender and receiver agree, and SR beats GBN on retransmitted segments
# ---------------------------------------------------------------------------


def test_sr_resends_one_segment_where_gbn_resends_the_range():
    """The core claim behind the hybrid, driven by hand so it is unambiguous."""
    from protocol.gbn import GbnReceiver, GbnSender

    def resent_after_losing_segment_two(make_pair):
        tx, rx = make_pair()
        first = tx.packets_to_send(0.0)
        for seq in first:
            if seq == 2:
                continue                       # segment 2 never arrives
            result = rx.on_data(seq, bytes([seq]))
            if result.ack is not None:
                tx.on_ack(result.ack, 0.0)
        return tx.on_timeout(1.5)

    def gbn_pair():
        state = SenderTransferState(segments=[bytes([i]) for i in range(6)],
                                    window_size=6)
        return GbnSender(state, 1.0), GbnReceiver(ReceiverTransferState())

    def sr_pair():
        state = SenderTransferState(segments=[bytes([i]) for i in range(6)],
                                    window_size=6)
        return SrSender(state, 1.0), SrReceiver(ReceiverTransferState(), 6)

    assert resent_after_losing_segment_two(gbn_pair) == [2, 3, 4, 5]
    assert resent_after_losing_segment_two(sr_pair) == [2]


def test_sr_sender_and_receiver_complete_a_transfer_with_reordering():
    tx, tx_state = sr_sender(total=6, window=6)
    rx, rx_state = sr_receiver(window=6)

    outgoing = tx.packets_to_send(0.0)
    delivered = []
    for seq in reversed(outgoing):              # arrive in reverse order
        result = rx.on_data(seq, bytes([seq]))
        delivered.extend(s for s, _ in result.deliver)
        if result.ack is not None:
            tx.on_ack(result.ack, 0.1)

    assert delivered == [0, 1, 2, 3, 4, 5], "delivery is in order despite arrival order"
    assert tx.all_acked() and tx_state.base == 6
    assert rx_state.expected_seq == 6


# ---------------------------------------------------------------------------
# T4.8 — failure tests over real sockets, against BOTH baselines
# ---------------------------------------------------------------------------


BASELINES = ["gbn", "sr"]


def run_impaired(tmp_path, *, mode, size, window=4, rto=0.25, run_id="x",
                 sender_impairment=None, receiver_impairment=None,
                 idle_timeout=20.0):
    """One transfer through the simulator. Returns (sender, receiver, src, out)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = make_file(tmp_path, size=size)
    output = tmp_path / "received.bin"
    logs = tmp_path / "logs"

    rx = Receiver(output=output, host="127.0.0.1", port=0, run_id=run_id,
                  log_dir=logs, idle_timeout=idle_timeout, linger=0.3,
                  impairment=receiver_impairment)
    port = rx.bind()

    failures = []

    def receive():
        try:
            rx.run()
        except ReceiverError as exc:
            failures.append(exc)

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()

    tx = Sender(path=source, host="127.0.0.1", port=port, mode=mode,
                window=window, run_id=run_id, log_dir=logs, rto=rto,
                impairment=sender_impairment)
    error = None
    try:
        tx.run()
    except TransferError as exc:
        error = exc
    finally:
        thread.join(timeout=60)

    assert not thread.is_alive(), "receiver thread did not finish"
    return tx, rx, source, output, error, failures


def assert_correct_or_reported(source, output, error, receiver_failures):
    """CC-01: either the file is right, or the failure is reported. Never a
    silently wrong file presented as success."""
    if error is None and not receiver_failures:
        assert sha256_of(output) == sha256_of(source), "SILENT CORRUPTION"
    else:
        # A reported failure must not leave a file claimed to be complete.
        assert error is not None or receiver_failures


@pytest.mark.parametrize("mode", BASELINES)
def test_single_loss_recovers(tmp_path, mode):
    """One dropped DATA packet, deterministically, via a one-shot schedule."""
    tx, rx, source, output, error, failures = run_impaired(
        tmp_path, mode=mode, size=config.SEGMENT_SIZE * 12, window=4,
        run_id=f"{mode}_single",
        sender_impairment=Impairment(loss_schedule=((0.0, 0.0), (0.001, 0.12),
                                                    (0.05, 0.0)), seed=5))
    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)


@pytest.mark.parametrize("mode", BASELINES)
def test_multiple_consecutive_losses_recover(tmp_path, mode):
    """A burst: 60% loss for a slice of the transfer, then clean."""
    tx, rx, source, output, error, failures = run_impaired(
        tmp_path, mode=mode, size=config.SEGMENT_SIZE * 12, window=4,
        run_id=f"{mode}_burst",
        sender_impairment=Impairment(loss_schedule=((0.0, 0.6), (0.15, 0.0)), seed=11))
    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)
    assert tx.data_retransmitted > 0, "a burst that cost nothing did not happen"


@pytest.mark.parametrize("mode", BASELINES)
@pytest.mark.parametrize("loss", [0.05, 0.20])
def test_random_loss_recovers(tmp_path, mode, loss):
    tx, rx, source, output, error, failures = run_impaired(
        tmp_path, mode=mode, size=config.SEGMENT_SIZE * 10, window=4,
        run_id=f"{mode}_rand{int(loss * 100)}",
        sender_impairment=Impairment(loss_rate=loss, seed=3))
    assert_correct_or_reported(source, output, error, failures)
    assert error is None and not failures, f"{mode} failed at {loss:.0%} loss: {error}"
    assert sha256_of(output) == sha256_of(source)


@pytest.mark.parametrize("mode", BASELINES)
def test_ack_loss_recovers(tmp_path, mode):
    """Reverse-direction loss. GBN rides it out on the next cumulative ACK;
    SR has to wait for the segment timer, since its ACKs do not cover each other."""
    tx, rx, source, output, error, failures = run_impaired(
        tmp_path, mode=mode, size=config.SEGMENT_SIZE * 8, window=4,
        run_id=f"{mode}_ackloss",
        sender_impairment=Impairment(recv_loss_rate=0.25, seed=17))
    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)


@pytest.mark.parametrize("mode", BASELINES)
def test_artificial_delay_still_completes(tmp_path, mode):
    """A 60 ms RTT, split half per direction as design.md §8 specifies."""
    tx, rx, source, output, error, failures = run_impaired(
        tmp_path, mode=mode, size=config.SEGMENT_SIZE * 6, window=4, rto=0.6,
        run_id=f"{mode}_delay",
        sender_impairment=Impairment(delay_ms=30.0, seed=2),
        receiver_impairment=Impairment(delay_ms=30.0, seed=2))
    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)


@pytest.mark.parametrize("mode", BASELINES)
def test_loss_and_delay_together_recover(tmp_path, mode):
    tx, rx, source, output, error, failures = run_impaired(
        tmp_path, mode=mode, size=config.SEGMENT_SIZE * 8, window=4, rto=0.5,
        run_id=f"{mode}_both",
        sender_impairment=Impairment(loss_rate=0.1, recv_loss_rate=0.1,
                                     delay_ms=15.0, seed=23),
        receiver_impairment=Impairment(delay_ms=15.0, seed=23))
    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)


@pytest.mark.parametrize("mode", BASELINES)
def test_packet_corruption_never_reaches_the_file(tmp_path, mode):
    """IN-03: a corrupted DATA packet is rejected by the checksum and repaired
    by retransmission — it must never be written."""
    import protocol.packet as pk

    source = make_file(tmp_path, size=config.SEGMENT_SIZE * 6)
    output = tmp_path / "received.bin"
    logs = tmp_path / "logs"

    rx = Receiver(output=output, host="127.0.0.1", port=0,
                  run_id=f"{mode}_corrupt", log_dir=logs,
                  idle_timeout=20.0, linger=0.3)
    port = rx.bind()
    failures = []
    thread = threading.Thread(
        target=lambda: failures.append(_run_quiet(rx)), daemon=True)
    thread.start()

    tx = Sender(path=source, host="127.0.0.1", port=port, mode=mode, window=4,
                run_id=f"{mode}_corrupt", log_dir=logs, rto=0.25)

    # Corrupt a payload byte in a seeded fraction of outgoing packets. A raw
    # socket method cannot be reassigned, so wrap it the way the simulator does.
    tx.sock = _CorruptingSocket(tx.sock, probability=0.15)
    error = None
    try:
        tx.run()
    except TransferError as exc:
        error = exc
    finally:
        thread.join(timeout=60)

    assert not thread.is_alive()
    reported = [f for f in failures if f is not None]
    assert error is None and not reported, f"{mode} could not recover: {error}"
    assert sha256_of(output) == sha256_of(source), "corruption reached the file"

    assert tx.sock.corrupted > 0, "no packet was actually corrupted"
    events = [row["event"] for row in read_events(rx.log.directory)]
    assert "CHECKSUM_FAIL" in events, "corruption was not detected as such"


class _CorruptingSocket:
    """Flips a payload bit in a seeded random fraction of outgoing datagrams.

    Corruption in flight, as distinct from the simulator's loss: the packet
    still arrives, but its checksum no longer matches (IN-03).

    Random rather than every-Nth on purpose. A fixed counter phase-locks onto
    GBN's retransmission pattern — the same segment lands on the same counter
    value every time it goes round, so it is corrupted on every attempt and the
    transfer dies against the retry budget. That is an artifact of the test, not
    a protocol defect; real corruption is not synchronised to retransmissions.
    """

    def __init__(self, sock, probability=0.15, seed=4242):
        import random
        self._sock = sock
        self._probability = probability
        self._rng = random.Random(seed)
        self.corrupted = 0

    def sendto(self, data, address):
        import protocol.packet as pk
        if len(data) > pk.HEADER_SIZE + 5 and self._rng.random() < self._probability:
            mutated = bytearray(data)
            mutated[pk.HEADER_SIZE + 3] ^= 0xFF
            self.corrupted += 1
            return self._sock.sendto(bytes(mutated), address)
        return self._sock.sendto(data, address)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _run_quiet(rx):
    try:
        rx.run()
        return None
    except ReceiverError as exc:
        return exc


@pytest.mark.parametrize("mode", BASELINES)
def test_drop_and_checksum_fail_are_logged_as_different_events(tmp_path, mode):
    """T4.3 / IN-04: an injected condition must stay distinguishable from a
    protocol defect in the logs, or the analysis cannot tell them apart."""
    tx, rx, source, output, error, failures = run_impaired(
        tmp_path, mode=mode, size=config.SEGMENT_SIZE * 6, window=4,
        run_id=f"{mode}_dropev",
        sender_impairment=Impairment(loss_rate=0.2, seed=8))
    assert error is None and not failures

    rows = read_events(tx.log.directory)
    drops = [r for r in rows if r["event"] == "DROP"]
    assert drops, "20% loss produced no DROP events"
    assert all(r["event"] != "CHECKSUM_FAIL" for r in drops)
    assert all("send" in r["reason"] or "recv" in r["reason"] for r in drops)
    assert any(r["reason"].endswith("DATA") for r in drops)


def test_sr_retransmits_less_than_gbn_under_the_same_loss(tmp_path):
    """H-02, checked end to end: with the same seed and the same condition, SR
    should resend fewer segments than GBN. Recorded as a measurement, not
    asserted as a law — the experiments in Phase 8 are what establish it."""
    results = {}
    for mode in BASELINES:
        tx, _, source, output, error, failures = run_impaired(
            tmp_path / mode, mode=mode, size=config.SEGMENT_SIZE * 20, window=8,
            run_id=f"cmp_{mode}",
            sender_impairment=Impairment(loss_rate=0.1, seed=1234))
        assert error is None and not failures
        assert sha256_of(output) == sha256_of(source)
        results[mode] = tx.data_retransmitted

    assert results["sr"] <= results["gbn"], (
        f"SR resent more than GBN under identical loss: {results}")


def test_dynamic_loss_change_appears_in_the_event_log(tmp_path):
    """T4.2 done-when, end to end."""
    # The run has to outlast the step or the test passes vacuously, and a clean
    # loopback transfer can finish in about a millisecond. A 5 ms one-way delay
    # over 20 windows puts a floor of ~100 ms on it, so a step at 10 ms is
    # crossed by construction rather than by luck.
    tx, rx, source, output, error, failures = run_impaired(
        tmp_path, mode="gbn", size=config.SEGMENT_SIZE * 40, window=2, rto=0.5,
        run_id="dynamic",
        sender_impairment=Impairment(loss_rate=0.0, seed=31, delay_ms=5.0,
                                     loss_schedule=((0.010, 0.25),)))
    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)

    rows = read_events(tx.log.directory)
    changes = [r for r in rows if r["event"] == "LOSS_CHANGE"]
    assert changes, "the scheduled loss change is not in the event log"
    assert changes[0]["reason"].startswith("0.0000->0.2500")


@pytest.mark.parametrize("mode", BASELINES)
def test_same_seed_reproduces_the_same_transfer(tmp_path, mode):
    """RP-03 at the whole-transfer level: replaying a recorded seed must give
    the same retransmission count, or no result is reproducible."""
    counts = []
    for attempt in range(2):
        tx, _, source, output, error, failures = run_impaired(
            tmp_path / f"{mode}{attempt}", mode=mode,
            size=config.SEGMENT_SIZE * 15, window=4,
            run_id=f"{mode}_seed{attempt}",
            sender_impairment=Impairment(loss_rate=0.15, seed=777))
        assert error is None and not failures
        assert sha256_of(output) == sha256_of(source)
        counts.append(tx.data_retransmitted)
    assert counts[0] == counts[1], f"same seed gave different runs: {counts}"
