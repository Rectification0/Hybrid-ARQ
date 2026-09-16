"""Impairment tests (T4.1, T4.2, T4.3).

The done-when conditions:

* T4.1 — the same seed reproduces an identical drop sequence, and 0% loss with
  0 delay is behaviourally identical to a raw socket
* T4.2 — a scheduled loss change is visible in the event log
* T4.3 — DROP is logged distinctly from CHECKSUM_FAIL

Reproducibility is the one that matters most: every experimental result in
Phase 8 is only defensible if the condition that produced it can be replayed
exactly from a recorded seed (RP-03).
"""

import socket
import threading
import time

import pytest

import config
from network.simulator import ImpairedSocket, Impairment
from network.udp import open_udp_socket


def udp_pair():
    """A bound receiver socket and an unbound sender socket on loopback."""
    rx = open_udp_socket(("127.0.0.1", 0))
    tx = open_udp_socket()
    return tx, rx, rx.getsockname()


def drain(sock, count, timeout=2.0):
    """Read up to ``count`` datagrams, stopping early on timeout."""
    got = []
    sock.settimeout(timeout)
    for _ in range(count):
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            break
        got.append(data)
    return got


# ---------------------------------------------------------------------------
# T4.1 — reproducibility
# ---------------------------------------------------------------------------


def drop_sequence(seed, rate=0.4, count=200):
    """Which of ``count`` sends get dropped, as a tuple of booleans."""
    sent = []
    sock = ImpairedSocket(_NullSocket(sent), loss_rate=rate, seed=seed)
    for index in range(count):
        sock.sendto(str(index).encode(), ("127.0.0.1", 1))
    delivered = {int(payload) for payload in sent}
    return tuple(index in delivered for index in range(count))


class _NullSocket:
    """Records what actually reaches the wire; never touches the network."""

    def __init__(self, sink):
        self.sink = sink

    def sendto(self, data, address):
        self.sink.append(data)
        return len(data)

    def settimeout(self, value):
        pass

    def close(self):
        pass


def test_same_seed_reproduces_an_identical_drop_sequence():
    """T4.1 done-when, and the basis of RP-03."""
    assert drop_sequence(seed=1234) == drop_sequence(seed=1234)


def test_different_seeds_produce_different_drop_sequences():
    assert drop_sequence(seed=1) != drop_sequence(seed=2)


def test_drop_rate_is_approximately_the_configured_rate():
    delivered = drop_sequence(seed=7, rate=0.2, count=2000)
    observed = 1.0 - (sum(delivered) / len(delivered))
    assert 0.17 < observed < 0.23, f"observed loss {observed:.3f} far from 0.20"


def test_send_and_receive_draw_from_separate_streams():
    """Separate per-direction RNGs are what keep the forward drop sequence a
    pure function of the seed, independent of when ACKs happen to arrive."""
    a = drop_sequence(seed=99)

    # Same seed, but a socket that also consumes receive-side randomness.
    sent = []
    sock = ImpairedSocket(_NullSocket(sent), loss_rate=0.4, recv_loss_rate=0.5, seed=99)
    for index in range(200):
        sock.sendto(str(index).encode(), ("127.0.0.1", 1))
        sock._recv_rng.random()          # simulate interleaved receive activity
    delivered = {int(p) for p in sent}
    b = tuple(i in delivered for i in range(200))

    assert a == b, "receive-side draws perturbed the send-side drop sequence"


# ---------------------------------------------------------------------------
# T4.1 — a no-op condition is a raw socket
# ---------------------------------------------------------------------------


def test_zero_loss_zero_delay_is_not_wrapped_at_all():
    """The clean baseline must not run through code the impaired runs skip, or
    the comparison measures the wrapper too."""
    raw = open_udp_socket()
    try:
        assert Impairment().wrap(raw) is raw
        assert Impairment(seed=99).wrap(raw) is raw
    finally:
        raw.close()


@pytest.mark.parametrize("impairment", [
    Impairment(loss_rate=0.01),
    Impairment(recv_loss_rate=0.01),
    Impairment(delay_ms=1.0),
    Impairment(loss_schedule=((1.0, 0.5),)),
])
def test_any_configured_impairment_does_wrap(impairment):
    raw = open_udp_socket()
    try:
        wrapped = impairment.wrap(raw)
        assert isinstance(wrapped, ImpairedSocket)
    finally:
        raw.close()


def test_zero_impairment_delivers_every_datagram():
    tx, rx, address = udp_pair()
    wrapped = ImpairedSocket(tx, loss_rate=0.0, delay_ms=0.0, seed=1)
    try:
        for index in range(20):
            wrapped.sendto(str(index).encode(), address)
        assert len(drain(rx, 20)) == 20
    finally:
        wrapped.close()
        rx.close()


# ---------------------------------------------------------------------------
# T4.1 — delay and jitter
# ---------------------------------------------------------------------------


def test_delayed_datagram_arrives_late_but_arrives():
    tx, rx, address = udp_pair()
    wrapped = ImpairedSocket(tx, delay_ms=120.0, seed=1)
    try:
        started = time.monotonic()
        wrapped.sendto(b"delayed", address)
        rx.settimeout(3.0)
        data, _ = rx.recvfrom(4096)
        elapsed = time.monotonic() - started
        assert data == b"delayed"
        assert elapsed >= 0.10, f"arrived after only {elapsed:.3f}s"
    finally:
        wrapped.close()
        rx.close()


def test_sendto_does_not_block_for_the_delay():
    """A blocking sleep would serialise a window that should be in flight
    together — at a 500 ms RTT that would dominate every measurement."""
    tx, rx, address = udp_pair()
    wrapped = ImpairedSocket(tx, delay_ms=200.0, seed=1)
    try:
        started = time.monotonic()
        for index in range(5):
            wrapped.sendto(str(index).encode(), address)
        call_time = time.monotonic() - started
        assert call_time < 0.05, f"sendto blocked for {call_time:.3f}s"
        assert len(drain(rx, 5, timeout=3.0)) == 5
    finally:
        wrapped.close()
        rx.close()


def test_delayed_datagrams_keep_their_order_at_zero_jitter():
    tx, rx, address = udp_pair()
    wrapped = ImpairedSocket(tx, delay_ms=40.0, seed=1)
    try:
        for index in range(10):
            wrapped.sendto(str(index).encode(), address)
        received = [int(p) for p in drain(rx, 10, timeout=3.0)]
        assert received == list(range(10))
    finally:
        wrapped.close()
        rx.close()


def test_jitter_without_a_base_delay_is_refused():
    """Symmetric jitter around zero would need negative delays; clamping at
    zero would silently bias every sample upward."""
    raw = open_udp_socket()
    try:
        with pytest.raises(ValueError, match="jitter_ms requires"):
            ImpairedSocket(raw, delay_ms=0.0, jitter_ms=5.0)
    finally:
        raw.close()


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_loss_rate_outside_zero_to_one_is_refused(bad):
    raw = open_udp_socket()
    try:
        with pytest.raises(ValueError, match="must be in"):
            ImpairedSocket(raw, loss_rate=bad)
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# T4.2 — ACK-direction impairment and the loss schedule
# ---------------------------------------------------------------------------


def test_receive_direction_loss_discards_incoming_datagrams():
    """ACK loss (specs.md §25.3), modelled where the sender can observe it."""
    tx, rx, address = udp_pair()
    wrapped = ImpairedSocket(rx, recv_loss_rate=1.0, seed=1)
    try:
        wrapped.settimeout(0.3)
        tx.sendto(b"ack", address)
        with pytest.raises(socket.timeout):
            wrapped.recvfrom(4096)
        assert wrapped.dropped_receiving >= 1
    finally:
        wrapped.close()
        tx.close()


def test_receive_loss_does_not_extend_the_callers_timeout():
    """A discarded datagram must not buy the caller a second full wait."""
    tx, rx, address = udp_pair()
    wrapped = ImpairedSocket(rx, recv_loss_rate=1.0, seed=1)
    stop = threading.Event()

    def flood():
        while not stop.is_set():
            tx.sendto(b"x", address)
            time.sleep(0.01)

    thread = threading.Thread(target=flood, daemon=True)
    thread.start()
    try:
        wrapped.settimeout(0.3)
        started = time.monotonic()
        with pytest.raises(socket.timeout):
            wrapped.recvfrom(4096)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"timeout stretched to {elapsed:.3f}s under drops"
    finally:
        stop.set()
        thread.join(timeout=1)
        wrapped.close()
        tx.close()


def test_loss_schedule_steps_the_rate_over_time():
    events = []
    sock = ImpairedSocket(_NullSocket([]), loss_rate=0.0, seed=1,
                          loss_schedule=((0.05, 0.5), (0.10, 0.0)),
                          on_event=lambda kind, direction, raw, **d:
                              events.append((kind, d.get("reason"))))
    assert sock.current_loss_rate() == 0.0
    time.sleep(0.07)
    assert sock.current_loss_rate() == 0.5
    time.sleep(0.05)
    assert sock.current_loss_rate() == 0.0

    changes = [e for e in events if e[0] == "LOSS_CHANGE"]
    assert len(changes) == 2
    assert changes[0][1] == "0.0000->0.5000"
    assert changes[1][1] == "0.5000->0.0000"


def test_loss_schedule_is_reported_once_per_change_not_per_packet():
    events = []
    sock = ImpairedSocket(_NullSocket([]), seed=1, loss_schedule=((0.02, 0.3),),
                          on_event=lambda *a, **k: events.append(a[0]))
    time.sleep(0.04)
    for _ in range(50):
        sock.current_loss_rate()
    assert events.count("LOSS_CHANGE") == 1


def test_loss_schedule_parses_from_the_cli_form():
    from sender import parse_loss_schedule
    assert parse_loss_schedule("5:0.1,10:0.0") == ((5.0, 0.1), (10.0, 0.0))
    assert parse_loss_schedule(None) == ()
    assert parse_loss_schedule("") == ()
    with pytest.raises(ValueError, match="not t:rate"):
        parse_loss_schedule("nonsense")


# ---------------------------------------------------------------------------
# T4.3 — DROP is its own event
# ---------------------------------------------------------------------------


def test_drop_reports_the_direction_and_the_raw_packet():
    seen = []
    sock = ImpairedSocket(_NullSocket([]), loss_rate=1.0, seed=1,
                          on_event=lambda kind, direction, raw, **d:
                              seen.append((kind, direction, raw)))
    sock.sendto(b"payload", ("127.0.0.1", 1))
    assert seen == [("DROP", "send", b"payload")]


def test_impairment_round_trips_through_its_dict_form():
    impairment = Impairment(loss_rate=0.05, recv_loss_rate=0.01, delay_ms=25.0,
                            jitter_ms=5.0, seed=42, loss_schedule=((1.0, 0.2),))
    recorded = impairment.as_dict()
    assert recorded["loss_rate"] == 0.05
    assert recorded["seed"] == 42
    assert recorded["loss_schedule"] == [[1.0, 0.2]]
