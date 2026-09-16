"""Phase 2 tests: basic transfer, duplicate guard, retry budgets, event logs.

Covers the done-when conditions of T2.1 through T2.5:

* T2.1/T2.2 — a file transfers end to end at 0% loss and the output hash matches
* T2.3      — a replayed DATA packet writes nothing and logs DUPLICATE
* T2.4      — a sender with no receiver fails with a clear error instead of hanging
* T2.5      — a 0%-loss run produces a complete, parseable events.csv and summary.json

The transfer tests use real UDP sockets on an ephemeral loopback port, with the
receiver on a thread. That keeps them honest about socket behavior while staying
fast enough to run on every change.
"""

import csv
import hashlib
import json
import socket
import threading
import time

import pytest

import config
import eventlog
import receiver as receiver_mod
import sender as sender_mod
from eventlog import EVENT_COLUMNS, EventLog
from protocol import packet as pk
from protocol.packet import Packet, PacketType
from protocol.strategy import (ReceiverTransferState, SenderTransferState,
                               StopAndWaitReceiver, StopAndWaitSender)
from receiver import DeliveryOrderError, OutputWriter, Receiver, ReceiverError
from sender import Sender, TransferError, segment_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_file(directory, name="source.bin", size=5000, seed=b"\x01"):
    """A deterministic pseudo-random file — compressible data would hide
    nothing, but random data makes a truncated transfer obvious."""
    path = directory / name
    blob = hashlib.sha256(seed).digest()
    while len(blob) < size:
        blob += hashlib.sha256(blob).digest()
    path.write_bytes(blob[:size])
    return path


def sha256_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_transfer(tmp_path, *, size=5000, run_id="t", **sender_kwargs):
    """Run one full transfer over loopback and return (sender, receiver, output)."""
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

    tx = Sender(path=source, host="127.0.0.1", port=port, mode="saw",
                window=config.WINDOW_SIZE, run_id=run_id, log_dir=logs,
                rto=0.3, **sender_kwargs)
    try:
        tx.run()
    finally:
        thread.join(timeout=15)

    assert not thread.is_alive(), "receiver thread did not finish"
    if failures:
        raise failures[0]
    return tx, rx, source, output


def read_events(directory):
    with open(directory / "events.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------
# T2.1 / T2.2 — end-to-end transfer
# ---------------------------------------------------------------------------


def test_file_transfers_end_to_end_and_hash_matches(tmp_path):
    tx, rx, source, output = run_transfer(tmp_path, size=5000)
    assert output.exists()
    assert sha256_of(output) == sha256_of(source)
    assert tx.state == "COMPLETE"
    assert rx.state == "COMPLETE"
    assert rx.integrity_success


@pytest.mark.parametrize("size", [
    1,                              # a single byte
    config.SEGMENT_SIZE - 1,        # just under one segment
    config.SEGMENT_SIZE,            # exactly one segment
    config.SEGMENT_SIZE + 1,        # a full segment plus a remainder of one
    config.SEGMENT_SIZE * 4,        # an exact multiple
    config.SEGMENT_SIZE * 3 + 77,   # a ragged tail
])
def test_transfer_sizes_around_the_segment_boundary(tmp_path, size):
    _, _, source, output = run_transfer(tmp_path, size=size)
    assert output.read_bytes() == source.read_bytes()


def test_segmentation_matches_the_frozen_segment_size(tmp_path):
    source = make_file(tmp_path, size=config.SEGMENT_SIZE * 2 + 5)
    segments, digest, total = segment_file(source, config.SEGMENT_SIZE)
    assert len(segments) == 3
    assert [len(s) for s in segments] == [config.SEGMENT_SIZE, config.SEGMENT_SIZE, 5]
    assert total == config.SEGMENT_SIZE * 2 + 5
    assert digest == sha256_of(source)
    assert b"".join(segments) == source.read_bytes()


def test_empty_file_transfers_with_zero_segments(tmp_path):
    source = tmp_path / "empty.bin"
    source.write_bytes(b"")
    segments, digest, total = segment_file(source, config.SEGMENT_SIZE)
    assert segments == [] and total == 0
    assert digest == hashlib.sha256(b"").hexdigest()


def test_missing_source_file_is_reported(tmp_path):
    tx = Sender(path=tmp_path / "nope.bin", host="127.0.0.1", port=9, mode="saw",
                window=8, run_id="missing", log_dir=tmp_path / "logs")
    with pytest.raises(TransferError, match="no such file"):
        tx.run()


# ---------------------------------------------------------------------------
# T2.3 — duplicate-write guard
# ---------------------------------------------------------------------------


def test_writer_refuses_a_replayed_segment(tmp_path):
    writer = OutputWriter(tmp_path / "out.bin")
    assert writer.write(0, b"aaa") is True
    assert writer.write(1, b"bbb") is True
    assert writer.write(1, b"XXX") is False      # exact replay
    assert writer.write(0, b"YYY") is False      # older replay
    writer.finalize()
    writer.close()
    assert (tmp_path / "out.bin").read_bytes() == b"aaabbb"
    assert writer.bytes_written == 6
    assert writer.segments_written == 2


def test_writer_hash_ignores_duplicates(tmp_path):
    writer = OutputWriter(tmp_path / "out.bin")
    for seq, chunk in enumerate([b"one", b"two", b"three"]):
        writer.write(seq, chunk)
    writer.write(2, b"three")            # replay must not enter the digest
    digest = writer.finalize()
    writer.close()
    assert digest == hashlib.sha256(b"onetwothree").hexdigest()


def test_writer_rejects_a_gap_rather_than_writing_a_hole(tmp_path):
    writer = OutputWriter(tmp_path / "out.bin")
    writer.write(0, b"aaa")
    with pytest.raises(DeliveryOrderError):
        writer.write(2, b"ccc")
    writer.close()


def test_replayed_data_packet_writes_nothing_and_logs_duplicate(tmp_path):
    """T2.3 done-when, exercised over a real socket."""
    output = tmp_path / "received.bin"
    logs = tmp_path / "logs"
    rx = Receiver(output=output, host="127.0.0.1", port=0, run_id="dup",
                  log_dir=logs, idle_timeout=5.0, linger=0.1)
    port = rx.bind()

    errors = []
    thread = threading.Thread(
        target=lambda: errors.append(_safe_run(rx)), daemon=True)
    thread.start()

    payloads = [b"first-segment", b"second-segment"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5.0)
    expected_hash = hashlib.sha256(b"".join(payloads)).hexdigest()
    try:
        start = pk.StartPayload(filename="dup.bin", filesize=sum(map(len, payloads)),
                                sha256=expected_hash, segment_size=config.SEGMENT_SIZE,
                                mode="saw", run_id="dup")
        sock.sendto(pk.encode(Packet(type=PacketType.START, payload=start.to_payload())),
                    ("127.0.0.1", port))
        assert pk.decode(sock.recv(pk.MAX_DATAGRAM_SIZE)).type is PacketType.START_ACK

        for seq, payload in enumerate(payloads):
            for _ in range(2):      # send each DATA twice — the replay is the point
                sock.sendto(pk.encode(Packet(type=PacketType.DATA, seq=seq,
                                             payload=payload)), ("127.0.0.1", port))
                reply = pk.decode(sock.recv(pk.MAX_DATAGRAM_SIZE))
                assert reply.type is PacketType.ACK and reply.ack == seq

        sock.sendto(pk.encode(Packet(
            type=PacketType.FIN, seq=len(payloads),
            payload=pk.FinPayload(total_segments=len(payloads)).to_payload(),
        )), ("127.0.0.1", port))
        verdict = pk.FinAckPayload.from_payload(
            pk.decode(sock.recv(pk.MAX_DATAGRAM_SIZE)).payload)
    finally:
        sock.close()
        thread.join(timeout=10)

    assert verdict.match is True
    assert output.read_bytes() == b"".join(payloads)     # written once each
    assert verdict.bytes_written == sum(map(len, payloads))
    assert rx.duplicates == 2

    events = read_events(rx.log.directory)
    duplicate_rows = [row for row in events if row["event"] == "DUPLICATE"]
    assert len(duplicate_rows) == 2
    assert {row["sequence"] for row in duplicate_rows} == {"0", "1"}
    assert [row["event"] for row in events].count("DELIVER") == 2


def _safe_run(rx):
    try:
        return rx.run()
    except ReceiverError as exc:
        return exc


# ---------------------------------------------------------------------------
# T2.4 — retry budgets and idle timeout
# ---------------------------------------------------------------------------


def test_sender_with_no_receiver_fails_instead_of_hanging(tmp_path, monkeypatch):
    """T2.4 done-when: a clear error, and bounded in time."""
    monkeypatch.setattr(config, "CONTROL_RETRY_LIMIT", 3)
    monkeypatch.setattr(config, "CONTROL_RETRY_TIMEOUT_S", 0.1)

    # A bound-but-silent socket: the port exists, so nothing answers rather than
    # the OS reporting it unreachable.
    dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]

    source = make_file(tmp_path, size=100)
    tx = Sender(path=source, host="127.0.0.1", port=port, mode="saw", window=8,
                run_id="noreceiver", log_dir=tmp_path / "logs", rto=0.1)

    started = time.monotonic()
    with pytest.raises(TransferError, match="no START_ACK"):
        tx.run()
    elapsed = time.monotonic() - started
    dead.close()

    assert elapsed < 5.0, "sender should give up quickly, not hang"
    assert tx.state == "ERROR"
    events = read_events(tx.log.directory)
    assert [row for row in events if row["event"] == "ERROR"]
    assert len([row for row in events if row["event"] == "START"]) == 3


def test_sender_against_a_closed_port_still_paces_its_retries(tmp_path, monkeypatch):
    """Regression: the closed-port case differs from the bound-but-silent one.

    On Windows a datagram to a closed port provokes an ICMP port-unreachable,
    raised on the next recvfrom as ConnectionResetError (network/udp.py). That
    escaped the retry loop entirely and surfaced as "an existing connection was
    forcibly closed" — wrong error, and the budget was never spent. It must
    instead behave exactly like silence.
    """
    monkeypatch.setattr(config, "CONTROL_RETRY_LIMIT", 3)
    monkeypatch.setattr(config, "CONTROL_RETRY_TIMEOUT_S", 0.2)

    # Bind then close, so the port is known to have nothing listening on it.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    source = make_file(tmp_path, size=100)
    tx = Sender(path=source, host="127.0.0.1", port=port, mode="saw", window=8,
                run_id="closedport", log_dir=tmp_path / "logs", rto=0.1)

    started = time.monotonic()
    with pytest.raises(TransferError, match="no START_ACK"):
        tx.run()
    elapsed = time.monotonic() - started

    # Paced, not collapsed: three attempts at 0.2s each, and not instantaneous.
    assert 0.3 < elapsed < 5.0, f"retries were not paced ({elapsed:.3f}s)"
    events = [row["event"] for row in read_events(tx.log.directory)]
    assert events.count("START") == 3
    assert events.count("TIMEOUT") == 3


def test_receiver_idle_timeout_gives_up(tmp_path):
    rx = Receiver(output=tmp_path / "out.bin", host="127.0.0.1", port=0,
                  run_id="idle", log_dir=tmp_path / "logs", idle_timeout=0.3)
    rx.bind()
    started = time.monotonic()
    with pytest.raises(ReceiverError, match="no START"):
        rx.run()
    assert time.monotonic() - started < 3.0


def test_duplicate_start_is_reacked_idempotently(tmp_path):
    """The first START_ACK may be lost; a second START must be answered."""
    rx = Receiver(output=tmp_path / "out.bin", host="127.0.0.1", port=0,
                  run_id="dupstart", log_dir=tmp_path / "logs",
                  idle_timeout=3.0, linger=0.1)
    port = rx.bind()
    thread = threading.Thread(target=lambda: _safe_run(rx), daemon=True)
    thread.start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)
    start = pk.encode(Packet(type=PacketType.START, payload=pk.StartPayload(
        filename="f.bin", filesize=3, sha256=hashlib.sha256(b"abc").hexdigest(),
        segment_size=config.SEGMENT_SIZE, mode="saw", run_id="dupstart").to_payload()))
    try:
        for _ in range(2):
            sock.sendto(start, ("127.0.0.1", port))
            reply = pk.decode(sock.recv(pk.MAX_DATAGRAM_SIZE))
            assert reply.type is PacketType.START_ACK
            assert pk.StartAckPayload.from_payload(reply.payload).accepted

        sock.sendto(pk.encode(Packet(type=PacketType.DATA, seq=0, payload=b"abc")),
                    ("127.0.0.1", port))
        sock.recv(pk.MAX_DATAGRAM_SIZE)
        sock.sendto(pk.encode(Packet(type=PacketType.FIN, seq=1, payload=pk.FinPayload(
            total_segments=1).to_payload())), ("127.0.0.1", port))
        assert pk.FinAckPayload.from_payload(
            pk.decode(sock.recv(pk.MAX_DATAGRAM_SIZE)).payload).match
    finally:
        sock.close()
        thread.join(timeout=10)


def test_segment_size_mismatch_is_refused_at_setup(tmp_path):
    """Caught at the handshake, not surfaced later as a corrupted file."""
    rx = Receiver(output=tmp_path / "out.bin", host="127.0.0.1", port=0,
                  run_id="mismatch", log_dir=tmp_path / "logs", idle_timeout=3.0)
    port = rx.bind()
    thread = threading.Thread(target=lambda: _safe_run(rx), daemon=True)
    thread.start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)
    try:
        sock.sendto(pk.encode(Packet(type=PacketType.START, payload=pk.StartPayload(
            filename="f.bin", filesize=1, sha256="0" * 64, segment_size=512,
            mode="saw", run_id="mismatch").to_payload())), ("127.0.0.1", port))
        reply = pk.StartAckPayload.from_payload(
            pk.decode(sock.recv(pk.MAX_DATAGRAM_SIZE)).payload)
    finally:
        sock.close()
        thread.join(timeout=10)

    assert reply.accepted is False
    assert "segment size" in reply.reason


def test_receiver_ignores_corrupted_packets_and_keeps_going(tmp_path):
    """A corrupted datagram is discarded and logged, not mistaken for silence."""
    rx = Receiver(output=tmp_path / "out.bin", host="127.0.0.1", port=0,
                  run_id="corrupt", log_dir=tmp_path / "logs",
                  idle_timeout=3.0, linger=0.1)
    port = rx.bind()
    thread = threading.Thread(target=lambda: _safe_run(rx), daemon=True)
    thread.start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)
    try:
        sock.sendto(pk.encode(Packet(type=PacketType.START, payload=pk.StartPayload(
            filename="f.bin", filesize=3, sha256=hashlib.sha256(b"abc").hexdigest(),
            segment_size=config.SEGMENT_SIZE, mode="saw",
            run_id="corrupt").to_payload())), ("127.0.0.1", port))
        sock.recv(pk.MAX_DATAGRAM_SIZE)

        corrupted = bytearray(pk.encode(Packet(type=PacketType.DATA, seq=0, payload=b"abc")))
        corrupted[8] ^= 0xFF                      # flip a SEQUENCE bit
        sock.sendto(bytes(corrupted), ("127.0.0.1", port))

        sock.sendto(pk.encode(Packet(type=PacketType.DATA, seq=0, payload=b"abc")),
                    ("127.0.0.1", port))
        assert pk.decode(sock.recv(pk.MAX_DATAGRAM_SIZE)).type is PacketType.ACK

        sock.sendto(pk.encode(Packet(type=PacketType.FIN, seq=1, payload=pk.FinPayload(
            total_segments=1).to_payload())), ("127.0.0.1", port))
        assert pk.FinAckPayload.from_payload(
            pk.decode(sock.recv(pk.MAX_DATAGRAM_SIZE)).payload).match
    finally:
        sock.close()
        thread.join(timeout=10)

    events = read_events(rx.log.directory)
    assert [row for row in events if row["event"] == "CHECKSUM_FAIL"]
    assert (tmp_path / "out.bin").read_bytes() == b"abc"


# ---------------------------------------------------------------------------
# T2.5 — event log and summary
# ---------------------------------------------------------------------------


def test_zero_loss_run_writes_parseable_logs_on_both_endpoints(tmp_path):
    """T2.5 done-when."""
    tx, rx, source, output = run_transfer(tmp_path, size=config.SEGMENT_SIZE * 3,
                                          run_id="logged")

    for log in (tx.log, rx.log):
        rows = read_events(log.directory)
        assert rows, f"{log.endpoint} wrote no events"
        with open(log.directory / "events.csv", newline="", encoding="utf-8") as handle:
            assert next(csv.reader(handle)) == EVENT_COLUMNS
        assert all(set(row) == set(EVENT_COLUMNS) for row in rows)
        assert all(row["run_id"] == "logged" for row in rows)
        assert all(row["endpoint"] == log.endpoint for row in rows)
        assert all(row["event"] in eventlog.EVENT_NAMES for row in rows)

        timestamps = [float(row["timestamp"]) for row in rows]
        assert timestamps == sorted(timestamps), "timestamps must be monotonic"

        summary = json.loads((log.directory / "summary.json").read_text())
        assert summary["run_id"] == "logged"
        assert summary["config"]["segment_size"] == config.SEGMENT_SIZE
        assert summary["metrics"]["integrity_success"] is True
        assert "software" in summary and "python" in summary["software"]

    sender_events = [row["event"] for row in read_events(tx.log.directory)]
    assert sender_events.count("SEND") == 3
    assert sender_events[0] == "START"
    assert "FIN_ACK" in sender_events

    receiver_events = [row["event"] for row in read_events(rx.log.directory)]
    assert receiver_events.count("DELIVER") == 3
    assert receiver_events.count("ACK") == 3


def test_retransmission_count_is_derivable_from_events_alone(tmp_path):
    """CC-06: the log, not an in-memory counter, is the record."""
    tx, _, _, _ = run_transfer(tmp_path, size=config.SEGMENT_SIZE * 2)
    rows = read_events(tx.log.directory)
    assert len([r for r in rows if r["event"] == "RETX"]) == tx.data_retransmitted
    assert len([r for r in rows if r["event"] == "SEND"]) == tx.data_sent


def test_event_log_rejects_an_unknown_event_name(tmp_path):
    with EventLog("x", "sender", log_dir=tmp_path) as log:
        with pytest.raises(ValueError, match="vocabulary"):
            log.emit("NOT_A_REAL_EVENT")


def test_event_log_rejects_an_unknown_endpoint(tmp_path):
    with pytest.raises(ValueError, match="endpoint"):
        EventLog("x", "middlebox", log_dir=tmp_path)


def test_event_log_columns_match_the_spec_exactly():
    assert EVENT_COLUMNS == [
        "timestamp", "run_id", "endpoint", "event", "sequence", "ack",
        "mode", "window_size", "loss_estimate", "rtt_ms", "reason",
    ]


def test_summary_records_the_config_and_commit(tmp_path):
    with EventLog("cfg", "sender", log_dir=tmp_path) as log:
        log.emit("SEND", sequence=0)
        path = log.write_summary({"integrity_success": True})
    summary = json.loads(path.read_text())
    assert summary["config"] == config.snapshot()
    assert summary["event_counts"] == {"SEND": 1}
    assert set(summary["software"]) == {"commit", "working_tree_dirty",
                                        "python", "platform"}


def test_config_snapshot_covers_every_spec_15_parameter():
    snapshot = config.snapshot()
    for key in ["host", "port", "segment_size", "window_size", "rto_s",
                "loss_rate", "rtt_ms", "switch_high", "switch_low",
                "hysteresis_count", "random_seed"]:
        assert key in snapshot, f"specs.md §15 parameter {key} missing from snapshot"


# ---------------------------------------------------------------------------
# Stop-and-wait strategy units
# ---------------------------------------------------------------------------


def test_stop_and_wait_sends_one_segment_at_a_time():
    state = SenderTransferState(segments=[b"a", b"b", b"c"], window_size=8)
    strategy = StopAndWaitSender(state, rto=1.0)
    assert strategy.packets_to_send(0.0) == [0]
    assert strategy.packets_to_send(0.0) == [], "must not send while one is in flight"
    strategy.on_ack(0)
    assert strategy.packets_to_send(0.1) == [1]


def test_stop_and_wait_ignores_a_stale_ack():
    state = SenderTransferState(segments=[b"a", b"b"], window_size=8)
    strategy = StopAndWaitSender(state, rto=1.0)
    strategy.packets_to_send(0.0)
    strategy.on_ack(0)
    result = strategy.on_ack(0)          # replayed ACK for an already-acked segment
    assert result.newly_acked == [] and result.duplicate
    assert state.base == 1, "base must not move on a duplicate ACK"


def test_stop_and_wait_retransmits_only_after_the_rto():
    state = SenderTransferState(segments=[b"a"], window_size=8)
    strategy = StopAndWaitSender(state, rto=0.5)
    strategy.packets_to_send(10.0)
    assert strategy.on_timeout(10.4) == []
    assert strategy.on_timeout(10.5) == [0]


def test_stop_and_wait_completes_only_when_every_segment_is_acked():
    state = SenderTransferState(segments=[b"a", b"b"], window_size=8)
    strategy = StopAndWaitSender(state, rto=1.0)
    assert not strategy.all_acked()
    for seq in (0, 1):
        strategy.packets_to_send(0.0)
        strategy.on_ack(seq)
    assert strategy.all_acked()
    assert state.is_quiescent()


def test_stop_and_wait_receiver_reacks_a_duplicate():
    state = ReceiverTransferState()
    strategy = StopAndWaitReceiver(state)
    first = strategy.on_data(0, b"a")
    assert first.deliver == [(0, b"a")] and first.ack == 0

    repeat = strategy.on_data(0, b"a")
    assert repeat.deliver == [] and repeat.duplicate and repeat.ack == 0


def test_stop_and_wait_receiver_drops_a_future_segment():
    state = ReceiverTransferState()
    strategy = StopAndWaitReceiver(state)
    result = strategy.on_data(5, b"future")
    assert result.deliver == [] and result.ack is None
    assert state.expected_seq == 0
