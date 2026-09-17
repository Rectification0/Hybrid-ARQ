"""Logging completeness (T6.1, T6.2, T6.3).

T6.1 audits the design.md §9 event vocabulary against the code: every name must
have an emission point, and every emission point must be reachable in a run.
T6.2 confirms that every specs.md §20 metric is computable from events.csv alone
(CC-06) — checked by deriving them and comparing against what the endpoints
computed as they ran. T6.3 confirms that summary.json records the configuration
the run actually used, not the module defaults (RP-01, RP-02, RP-08).

The audit is written as a test rather than a document because a document goes
stale silently. An event name added to the vocabulary with no emitter, or a
metric that drifts into being computed only in memory, fails here.
"""

import csv
import json
import re
import threading
from pathlib import Path

import pytest

import config
import metrics as metrics_mod
from eventlog import EVENT_COLUMNS, EVENT_NAMES, EventLog
from network.simulator import Impairment
from protocol import hybrid
from receiver import Receiver, ReceiverError
from sender import Sender, TransferError

from test_transfer import make_file, read_events, sha256_of

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Where an event may legitimately be emitted from. The endpoints own logging
#: (design.md §1.1); strategy.py names the timer transitions it queues for the
#: endpoint to drain, and simulator.py names what it did to a packet for the
#: endpoint to decode and record.
EMITTING_SOURCES = ("sender.py", "receiver.py", "protocol/strategy.py",
                    "network/simulator.py")


def emitting_source_text() -> str:
    return "\n".join((PROJECT_ROOT / name).read_text(encoding="utf-8")
                     for name in EMITTING_SOURCES)


# ---------------------------------------------------------------------------
# T6.1 — every event in the vocabulary has an emission point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event", sorted(EVENT_NAMES))
def test_every_event_name_has_an_emission_point(event):
    """A name in the vocabulary that nothing emits is a hole in the record: the
    reader of a log has no way to tell "never happened" from "never logged"."""
    assert re.search(rf'"{event}"', emitting_source_text()), (
        f"{event} is in the design.md §9 vocabulary but no module emits it")


def test_no_event_is_emitted_outside_the_vocabulary():
    """The emitter already raises on an unknown name; this catches the reverse
    mistake — a plausible-looking name that was never added to the vocabulary."""
    emitted = set(re.findall(r'(?:emit|_log)\(\s*"([A-Z_]+)"', emitting_source_text()))
    assert emitted <= EVENT_NAMES, f"emitted but not in the vocabulary: {emitted - EVENT_NAMES}"


def test_the_vocabulary_matches_the_design_document():
    """design.md §9 is the source of the list; the code must not drift from it."""
    design = (PROJECT_ROOT / "design.md").read_text(encoding="utf-8")
    listed = design.split("Event vocabulary:", 1)[1].split(".", 1)[0]
    documented = set(re.findall(r"`([A-Z_]+)`", listed))
    assert documented == set(EVENT_NAMES)


def run_transfer(tmp_path, *, mode="gbn", size, run_id="log", rto=0.25,
                 sender_impairment=None, idle_timeout=20.0, hybrid_settings=None):
    """One transfer over loopback, returning both endpoints and their logs."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = make_file(tmp_path, size=size)
    output = tmp_path / "received.bin"
    logs = tmp_path / "logs"

    rx = Receiver(output=output, host="127.0.0.1", port=0, run_id=run_id,
                  log_dir=logs, idle_timeout=idle_timeout, linger=0.3)
    port = rx.bind()
    failures = []

    def receive():
        try:
            rx.run()
        except ReceiverError as exc:
            failures.append(exc)

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()

    tx = Sender(path=source, host="127.0.0.1", port=port, mode=mode, window=8,
                run_id=run_id, log_dir=logs, rto=rto, impairment=sender_impairment,
                hybrid_settings=hybrid_settings)
    error = None
    try:
        tx.run()
    except TransferError as exc:
        error = exc
    finally:
        thread.join(timeout=60)

    assert not thread.is_alive(), "receiver thread did not finish"
    return tx, rx, source, output, error, failures


def observed_events(*logs):
    seen = set()
    for log in logs:
        seen.update(row["event"] for row in read_events(log.directory))
    return seen


def test_a_lossy_hybrid_run_exercises_the_whole_vocabulary(tmp_path):
    """The audit's live half: with loss, a switch and a completed transfer, most
    of the vocabulary should actually appear. The exceptions are named, so an
    event that silently stopped being emitted cannot hide among them."""
    tx, rx, source, output, error, failures = run_transfer(
        tmp_path, mode="hybrid", size=config.SEGMENT_SIZE * 60, run_id="vocab",
        sender_impairment=Impairment(loss_rate=0.10, recv_loss_rate=0.05, seed=7),
        hybrid_settings=hybrid.HybridSettings(
            loss_window_size=20, switch_high=0.02, switch_low=0.0,
            hysteresis_count=1, evaluation_interval_segments=5,
            min_mode_residence_s=0.0))
    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)

    seen = observed_events(tx.log, rx.log)
    expected = {"SEND", "RETX", "ACK", "TIMEOUT", "TIMER_START", "TIMER_STOP",
                "SWITCH", "MODE", "DROP", "DUPLICATE", "DELIVER",
                "START", "START_ACK", "FIN", "FIN_ACK"}
    assert expected <= seen, f"never emitted: {sorted(expected - seen)}"

    # The rest need conditions this run deliberately does not create: real
    # corruption (CHECKSUM_FAIL, MALFORMED), a scheduled condition change
    # (LOSS_CHANGE), and a failed transfer (ERROR). Each is covered by its own
    # test elsewhere — T4.8 for corruption, T4.2 for the schedule, T2.4 for the
    # failure path — which is what keeps this list honest rather than convenient.
    assert seen <= EVENT_NAMES


def test_the_receiver_logs_its_idle_timeout_as_a_timeout(tmp_path):
    """Previously the idle timeout produced only an ERROR, so a log could not
    distinguish "gave up waiting" from any other failure."""
    rx = Receiver(output=tmp_path / "out.bin", host="127.0.0.1", port=0,
                  run_id="idle", log_dir=tmp_path / "logs", idle_timeout=0.3)
    rx.bind()
    with pytest.raises(ReceiverError):
        rx.run()

    rows = read_events(rx.log.directory)
    events = [row["event"] for row in rows]
    assert "TIMEOUT" in events and "ERROR" in events
    timeout = next(row for row in rows if row["event"] == "TIMEOUT")
    assert timeout["reason"].startswith("START;idle")


def test_a_receiver_that_never_sees_a_start_still_writes_a_log(tmp_path):
    """M8: an event log for *every* run. A receiver whose sender never appeared
    is exactly the run that most needs explaining afterwards."""
    rx = Receiver(output=tmp_path / "out.bin", host="127.0.0.1", port=0,
                  run_id="lonely", log_dir=tmp_path / "logs", idle_timeout=0.3)
    rx.bind()
    with pytest.raises(ReceiverError):
        rx.run()

    directory = tmp_path / "logs" / "lonely" / "receiver"
    assert (directory / "events.csv").exists()
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["metrics"]["integrity_success"] is False
    assert summary["metrics"]["error"]
    assert summary["final_state"] == "ERROR"


def test_events_seen_before_start_are_replayed_onto_the_timeline(tmp_path):
    """A packet rejected before the log exists must still appear in it, at the
    time it actually arrived rather than at zero (T6.1)."""
    import socket as socket_mod

    rx = Receiver(output=tmp_path / "out.bin", host="127.0.0.1", port=0,
                  run_id="early", log_dir=tmp_path / "logs", idle_timeout=0.4)
    port = rx.bind()

    probe = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_DGRAM)
    probe.sendto(b"not a packet at all", ("127.0.0.1", port))
    probe.close()

    with pytest.raises(ReceiverError):
        rx.run()

    rows = read_events(rx.log.directory)
    malformed = [row for row in rows if row["event"] == "MALFORMED"]
    assert malformed, "a packet rejected before START was dropped from the log"
    assert float(malformed[0]["timestamp"]) >= 0.0
    assert [row["event"] for row in rows].index("MALFORMED") == 0


@pytest.mark.parametrize("source", ["eventlog.py", "sender.py", "receiver.py",
                                    "network/simulator.py"])
def test_nothing_uses_the_coarse_windows_clock(source):
    """``time.monotonic`` is ``GetTickCount64`` on Windows — 15.6 ms of
    resolution, which quantises every RTT sample and residence time to a tick
    and makes the 10 ms RTT condition of E7 unmeasurable. ``perf_counter`` is
    monotonic too, at ~100 ns. Mixing the two would also compare timestamps from
    different origins, so the rule is all-or-nothing."""
    text = (PROJECT_ROOT / source).read_text(encoding="utf-8")
    assert "time.monotonic()" not in text


def test_the_log_clock_resolves_far_below_one_millisecond():
    """The measurement the logs exist to make has to survive the clock."""
    import time
    assert time.get_clock_info("perf_counter").resolution < 1e-4


def test_replayed_events_keep_their_order_and_timeline(tmp_path):
    """The replay is a re-emission, so the ordering and the clock have to come
    from when the event happened, not from when the log opened."""
    log = EventLog("replay", "receiver", log_dir=tmp_path, t0=100.0)
    log.emit("MALFORMED", timestamp=0.25, reason="early")
    log.emit("START", reason="live")
    log.close()

    rows = read_events(log.directory)
    assert [row["event"] for row in rows] == ["MALFORMED", "START"]
    assert float(rows[0]["timestamp"]) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# T6.2 — every specs.md §20 metric is computable from events.csv alone
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def derived_run(tmp_path_factory):
    """One impaired hybrid transfer, with its logs and both endpoints' metrics."""
    tmp_path = tmp_path_factory.mktemp("derive")
    tx, rx, source, output, error, failures = run_transfer(
        tmp_path, mode="hybrid", size=config.SEGMENT_SIZE * 60, run_id="derive",
        sender_impairment=Impairment(loss_rate=0.10, seed=23),
        hybrid_settings=hybrid.HybridSettings(
            loss_window_size=20, switch_high=0.02, switch_low=0.0,
            hysteresis_count=1, evaluation_interval_segments=5,
            min_mode_residence_s=0.0))
    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)

    run_directory = tmp_path / "logs" / "derive"
    derived = metrics_mod.derive_from_run(run_directory)
    sender_summary = json.loads(
        (tx.log.directory / "summary.json").read_text(encoding="utf-8"))
    receiver_summary = json.loads(
        (rx.log.directory / "summary.json").read_text(encoding="utf-8"))
    return derived, sender_summary, receiver_summary


def test_retransmission_count_is_derivable(derived_run):
    derived, sender, _ = derived_run
    assert derived["retransmission_count"] == sender["metrics"]["retransmission_count"]
    assert derived["retransmission_count"] > 0, "the run must actually retransmit"


def test_retransmission_overhead_is_derivable(derived_run):
    derived, sender, _ = derived_run
    assert derived["retransmission_overhead"] == pytest.approx(
        sender["metrics"]["retransmission_overhead"], abs=1e-9)


def test_transmitted_bytes_are_derivable(derived_run):
    derived, sender, _ = derived_run
    assert derived["bytes_transmitted"] == sender["metrics"]["bytes_transmitted"]
    assert derived["unique_data_packets"] == sender["metrics"]["unique_data_packets"]
    assert derived["total_data_transmissions"] == sender["metrics"]["total_data_transmissions"]


def test_delivered_bytes_and_goodput_are_derivable(derived_run):
    derived, sender, receiver = derived_run
    assert derived["delivered_bytes"] == receiver["metrics"]["bytes_written"]
    # Goodput uses the sender's completion time, which starts a little after its
    # log does, so the two agree closely rather than exactly.
    assert derived["goodput_bytes_per_s"] == pytest.approx(
        sender["metrics"]["goodput_bytes_per_s"], rel=0.05)


def test_completion_time_is_derivable(derived_run):
    derived, sender, _ = derived_run
    assert derived["completion_time_s"] == pytest.approx(
        sender["metrics"]["completion_time_s"], abs=0.25)


def test_integrity_success_is_derivable(derived_run):
    derived, sender, _ = derived_run
    assert derived["integrity_success"] is True
    assert derived["integrity_success"] == sender["metrics"]["integrity_success"]


def test_switch_count_and_residence_are_derivable(derived_run):
    derived, sender, _ = derived_run
    assert derived["switch_count"] == sender["metrics"]["switch_count"] >= 1
    assert derived["gbn_residence_s"] > 0 and derived["sr_residence_s"] > 0
    assert derived["gbn_residence_s"] == pytest.approx(
        sender["metrics"]["gbn_residence_s"], rel=0.10, abs=0.2)
    assert derived["sr_residence_s"] == pytest.approx(
        sender["metrics"]["sr_residence_s"], rel=0.10, abs=0.2)


def test_rtt_statistics_are_derivable(derived_run):
    derived, sender, _ = derived_run
    assert derived["rtt_samples"] > 0
    assert derived["rtt_mean_ms"] is not None
    assert derived["rtt_median_ms"] is not None and derived["rtt_p95_ms"] is not None
    # Fewer samples than the sender kept: a cumulative GBN ACK does not name
    # every segment it covers, so only the named ones can be paired in the log.
    assert derived["rtt_samples"] <= sender["metrics"]["rtt_samples"]


def test_latency_is_derivable_where_defined(derived_run):
    derived, _, _ = derived_run
    assert derived["first_delivery_s"] is not None
    assert derived["last_delivery_s"] >= derived["first_delivery_s"]


def test_no_spec_20_metric_is_missing_from_a_complete_run(derived_run):
    """CC-06 in one assertion: nothing in §20 is left uncomputable."""
    derived, _, _ = derived_run
    for name in ("completion_time_s", "goodput_bytes_per_s", "delivered_bytes",
                 "retransmission_count", "retransmission_overhead",
                 "integrity_success", "switch_count", "gbn_residence_s",
                 "sr_residence_s", "rtt_mean_ms", "first_delivery_s"):
        assert derived[name] is not None, f"§20 metric {name} is not derivable"


def test_a_fixed_hybrid_noop_is_not_counted_as_a_switch(tmp_path):
    """T5.6's control drains and handshakes without switching; counting those
    rows as transitions would make the control look like the treatment."""
    tx, _, source, output, error, failures = run_transfer(
        tmp_path, mode="fixed-hybrid", size=config.SEGMENT_SIZE * 60,
        run_id="noop", sender_impairment=Impairment(loss_rate=0.10, seed=7),
        hybrid_settings=hybrid.HybridSettings(
            loss_window_size=20, switch_high=0.02, switch_low=0.0,
            hysteresis_count=1, evaluation_interval_segments=5,
            min_mode_residence_s=0.0))
    assert error is None and not failures
    assert sha256_of(output) == sha256_of(source)

    rows = read_events(tx.log.directory)
    assert metrics_mod.handshake_count(rows) >= 1
    assert metrics_mod.switch_count(rows) == 0
    assert metrics_mod.switch_count(rows) == tx.controller.stats.switch_count


def test_a_failed_transfer_reports_no_goodput():
    """A corrupted transfer has no meaningful throughput, so it is reported as
    absent rather than averaged in later (design.md §10, CC-01)."""
    rows = [
        {"endpoint": "receiver", "event": "DELIVER", "timestamp": "1.0",
         "bytes": "1024", "sequence": "0", "ack": "", "mode": "gbn", "reason": ""},
        {"endpoint": "receiver", "event": "FIN_ACK", "timestamp": "2.0",
         "bytes": "", "sequence": "", "ack": "", "mode": "gbn",
         "reason": "HASH_MISMATCH"},
    ]
    assert metrics_mod.integrity_success(rows) is False
    assert metrics_mod.goodput_bytes_per_s(rows) is None


def test_an_unfinished_run_has_no_integrity_verdict():
    """Silence is not success (CC-01)."""
    rows = [{"endpoint": "sender", "event": "SEND", "timestamp": "0.1",
             "bytes": "1045", "sequence": "0", "ack": "", "mode": "gbn", "reason": ""}]
    assert metrics_mod.integrity_success(rows) is None


def test_the_deriver_reads_logs_without_modifying_them(tmp_path):
    """RP-07: aggregation reads logs; it never edits them."""
    tx, _, _, _, error, failures = run_transfer(
        tmp_path, size=config.SEGMENT_SIZE * 8, run_id="readonly")
    assert error is None and not failures

    path = tx.log.directory / "events.csv"
    before = path.read_bytes()
    metrics_mod.derive_from_run(tmp_path / "logs" / "readonly")
    assert path.read_bytes() == before


def test_every_logged_row_has_the_full_column_set(tmp_path):
    """A short row would parse into None silently and quietly break a metric."""
    tx, rx, _, _, error, failures = run_transfer(
        tmp_path, size=config.SEGMENT_SIZE * 8, run_id="columns")
    assert error is None and not failures

    for log in (tx.log, rx.log):
        with open(log.directory / "events.csv", newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        assert rows[0] == EVENT_COLUMNS
        assert all(len(row) == len(EVENT_COLUMNS) for row in rows[1:])


# ---------------------------------------------------------------------------
# T6.3 — summary.json records the run that actually happened
# ---------------------------------------------------------------------------


def test_summary_records_the_seed_and_impairment_actually_used(tmp_path):
    """RP-01, RP-02: a recorded config of module defaults describes a run nobody
    performed, and is worse than useless for reproducing one."""
    impairment = Impairment(loss_rate=0.07, recv_loss_rate=0.03, delay_ms=5.0,
                            jitter_ms=1.0, seed=1234)
    tx, rx, _, _, error, failures = run_transfer(
        tmp_path, size=config.SEGMENT_SIZE * 12, run_id="cfg", rto=0.37,
        sender_impairment=impairment)
    assert error is None and not failures

    summary = json.loads((tx.log.directory / "summary.json").read_text(encoding="utf-8"))
    recorded = summary["config"]
    assert recorded["random_seed"] == 1234
    assert recorded["loss_rate"] == 0.07
    assert recorded["ack_loss_rate"] == 0.03
    assert recorded["rtt_ms"] == pytest.approx(10.0)      # both halves of the trip
    assert recorded["jitter_ms"] == 1.0
    assert recorded["rto_s"] == 0.37
    assert recorded["window_size"] == 8
    assert recorded["transfer_file_size_bytes"] == config.SEGMENT_SIZE * 12
    assert recorded["segment_size"] == config.SEGMENT_SIZE


def test_summary_records_the_hybrid_thresholds_actually_used(tmp_path):
    """T7.1 sweeps these, so the swept value must be what is recorded."""
    settings = hybrid.HybridSettings(loss_window_size=20, switch_high=0.02,
                                     switch_low=0.0, hysteresis_count=1,
                                     evaluation_interval_segments=5,
                                     min_mode_residence_s=0.0)
    tx, _, _, _, error, failures = run_transfer(
        tmp_path, mode="hybrid", size=config.SEGMENT_SIZE * 30, run_id="thresholds",
        sender_impairment=Impairment(loss_rate=0.10, seed=7),
        hybrid_settings=settings)
    assert error is None and not failures

    recorded = json.loads(
        (tx.log.directory / "summary.json").read_text(encoding="utf-8"))["config"]
    assert recorded["switch_high"] == 0.02
    assert recorded["switch_low"] == 0.0
    assert recorded["hysteresis_count"] == 1
    assert recorded["evaluation_interval_segments"] == 5
    assert recorded["loss_window_size"] == 20


def test_summary_records_the_freeze_state_of_every_decision(tmp_path):
    """RP-08: a result is only traceable if the reader can tell which decisions
    were settled when it was produced."""
    tx, rx, _, _, error, failures = run_transfer(
        tmp_path, size=config.SEGMENT_SIZE * 8, run_id="decisions")
    assert error is None and not failures

    for log in (tx.log, rx.log):
        decisions = json.loads(
            (log.directory / "summary.json").read_text(encoding="utf-8"))["decisions"]
        assert set(decisions) == {f"D{n}" for n in range(1, 15)}, "all of design.md §12"
        assert decisions["D8"]["status"] == "frozen"
        assert decisions["D8"]["value"] == config.LOSS_WINDOW_SIZE
        assert decisions["D9"]["status"] == "frozen"       # T7.2 froze it
        assert decisions["D9"]["frozen_by"] == "T7.2"
        assert decisions["D9"]["value"] == config.SWITCH_HIGH
        assert decisions["D3"]["value"] == config.SEGMENT_SIZE
        # T8.1 closed the last three, so every decision in design.md §12 is now
        # frozen and a run records the value it was frozen at.
        assert decisions["D14"]["status"] == "frozen"
        assert decisions["D14"]["frozen_by"] == "T8.1"
        assert decisions["D14"]["value"] == config.TRANSFER_FILE_SIZE_BYTES
        assert {entry["status"] for entry in decisions.values()} == {"frozen"}


def test_summary_records_the_software_that_produced_it(tmp_path):
    tx, _, _, _, error, failures = run_transfer(
        tmp_path, size=config.SEGMENT_SIZE * 4, run_id="software")
    assert error is None and not failures

    software = json.loads(
        (tx.log.directory / "summary.json").read_text(encoding="utf-8"))["software"]
    assert software["commit"], "RP-08 needs the commit that produced the run"
    assert software["python"] and software["platform"]
    assert software["working_tree_dirty"] in (True, False)


def test_the_receiver_summary_records_its_own_run_config(tmp_path):
    tx, rx, _, _, error, failures = run_transfer(
        tmp_path, size=config.SEGMENT_SIZE * 12, run_id="rxcfg")
    assert error is None and not failures

    recorded = json.loads(
        (rx.log.directory / "summary.json").read_text(encoding="utf-8"))["config"]
    assert recorded["transfer_file_size_bytes"] == config.SEGMENT_SIZE * 12
    assert recorded["window_size"] == 8
    assert recorded["port"] == rx.port


def test_the_receiver_summary_records_the_hash_it_computed(tmp_path):
    """Both sides of the integrity comparison have to be readable afterwards.

    The receiver already recorded ``expected_sha256`` — the hash the sender
    claimed — and sent its own back in the FIN_ACK, but never wrote it down, so
    "source hash vs received hash" had only one side on disk (T11.4).
    """
    tx, rx, source, _, error, failures = run_transfer(
        tmp_path, size=config.SEGMENT_SIZE * 5, run_id="rxhash")
    assert error is None and not failures

    recorded = json.loads((rx.log.directory / "summary.json").read_text(encoding="utf-8"))
    assert recorded["received_sha256"] == sha256_of(source)
    assert recorded["received_sha256"] == recorded["expected_sha256"]
    assert recorded["metrics"]["integrity_success"] is True


def test_a_receiver_that_never_finalized_records_no_computed_hash(tmp_path):
    """Absence is recorded as absence, never as a match (CC-01)."""
    logs = tmp_path / "logs"
    rx = Receiver(output=tmp_path / "out.bin", host="127.0.0.1", port=0,
                  run_id="nohash", log_dir=logs, idle_timeout=0.3)
    rx.bind()
    with pytest.raises(ReceiverError):
        rx.run()

    recorded = json.loads((rx.log.directory / "summary.json").read_text(encoding="utf-8"))
    assert recorded["received_sha256"] is None
    assert recorded["expected_sha256"] is None


def test_an_unknown_config_key_is_refused():
    """A typo must not quietly invent a parameter in the recorded config."""
    with pytest.raises(KeyError, match="not a specs.md"):
        config.snapshot({"swich_high": 0.05})


def test_config_overrides_do_not_leak_between_runs():
    """snapshot() must describe the run it was asked about, not the last one."""
    overridden = config.snapshot({"random_seed": 999})
    assert overridden["random_seed"] == 999
    assert config.snapshot()["random_seed"] == config.RANDOM_SEED
