"""Analysis, graph and capture-harness tests (T9.1 … T9.5).

The graphs themselves are judged by looking at them; what is tested here is
everything that decides whether they are *honest*:

* an integrity failure is excluded from every mean and reported separately —
  the one rule in T9.1 that changes a number rather than a layout (CC-01);
* the spread reported is the spread across trials, not a formatting choice;
* the aggregate can be rebuilt from raw event rows, which is what keeps the
  index honest (CC-06);
* the mode timeline is read from the log's own ``mode`` column, so a run that
  never switched draws one band and a run that switched twice draws three;
* the capture scenarios cover the WS-* requirements they claim to, and the two
  lossy ones really do derive the same seed — a capture set whose comparison is
  not controlled proves nothing.

The plotting functions are exercised against a temporary directory, because a
chart that raises is a chart nobody notices is missing.
"""

import csv

import pandas as pd
import pytest

from experiments import analyze_results as analysis
from experiments import capture_evidence as capture
from experiments import run_experiment as runner


# ---------------------------------------------------------------------------
# Fixtures: a small, complete run index
# ---------------------------------------------------------------------------


def index_row(**changes):
    row = {
        "experiment": "E4", "condition": "loss05", "system": "gbn", "trial": 0,
        "run_id": "E4_gbn_loss05_trial00", "loss_rate": 0.05, "ack_loss_rate": 0.0,
        "rtt_ms": 100.0, "jitter_ms": 0.0, "loss_schedule": "", "seed": 1,
        "file_bytes": 1048576, "window_size": 8, "rto_s": 0.4,
        "completion_time_s": 40.0, "goodput_bytes_per_s": 26214.4,
        "delivered_bytes": 1048576, "retransmission_count": 400,
        "retransmission_overhead": 0.3, "switch_count": 0, "handshake_count": 0,
        "gbn_residence_s": 40.0, "sr_residence_s": 0.0, "rtt_mean_ms": 101.0,
        "final_mode": "gbn", "integrity_success": True, "status": "ok",
        "error": "", "wall_clock_s": 42.0, "started_utc": 0.0,
        "log_dir": "logs/experiments/E4_gbn_loss05_trial00",
        "sender_events_sha256": "a", "receiver_events_sha256": "b",
    }
    row.update(changes)
    return row


def write_index(tmp_path, rows):
    path = tmp_path / "runs.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


# ---------------------------------------------------------------------------
# Aggregation (T9.1)
# ---------------------------------------------------------------------------


def test_aggregate_reports_mean_and_spread_across_trials(tmp_path):
    rows = [index_row(trial=index, run_id=f"r{index}",
                      completion_time_s=time, goodput_bytes_per_s=1000.0 * (index + 1))
            for index, time in enumerate((38.0, 40.0, 42.0))]
    summary = analysis.aggregate(analysis.load_runs(write_index(tmp_path, rows)))

    assert len(summary) == 1
    cell = summary.iloc[0]
    assert cell["trials"] == 3 and cell["trials_aggregated"] == 3
    assert cell["completion_time_s_mean"] == pytest.approx(40.0)
    assert cell["completion_time_s_sd"] == pytest.approx(2.0)


def test_an_integrity_failure_is_excluded_from_every_mean(tmp_path):
    """CC-01, and the rule T9.1 states outright: a corrupted transfer has no
    meaningful goodput, so averaging one in would make the cell quietly wrong."""
    rows = [
        index_row(trial=0, run_id="ok0", completion_time_s=40.0),
        index_row(trial=1, run_id="ok1", completion_time_s=42.0),
        index_row(trial=2, run_id="bad", completion_time_s=300.0,
                  goodput_bytes_per_s=0.0, integrity_success=False,
                  status="integrity", error="hash mismatch"),
    ]
    frame = analysis.load_runs(write_index(tmp_path, rows))
    summary = analysis.aggregate(frame)
    cell = summary.iloc[0]

    assert cell["trials"] == 3, "the failed run is still counted as attempted"
    assert cell["trials_aggregated"] == 2
    assert cell["integrity_failures"] == 1
    assert cell["completion_time_s_mean"] == pytest.approx(41.0)


def test_failures_are_reported_rather_than_dropped(tmp_path):
    rows = [index_row(trial=0, run_id="ok0"),
            index_row(trial=1, run_id="bad", status="aborted",
                      integrity_success=False, error="abort timeout")]
    frame = analysis.load_runs(write_index(tmp_path, rows))
    report = analysis.integrity_report(frame)
    assert list(report["run_id"]) == ["bad"]
    assert report.iloc[0]["error"] == "abort timeout"


def test_a_single_trial_reports_zero_spread_rather_than_nan(tmp_path):
    """A cell with one usable run has no spread to report; NaN would propagate
    into the error bars and silently drop the point from a chart."""
    summary = analysis.aggregate(
        analysis.load_runs(write_index(tmp_path, [index_row()])))
    assert summary.iloc[0]["completion_time_s_sd"] == 0.0


def test_sr_residence_is_a_fraction_of_the_transfer(tmp_path):
    """Seconds in SR is not comparable between a 16 s run and a 140 s one."""
    rows = [index_row(system="hybrid", gbn_residence_s=10.0, sr_residence_s=30.0)]
    frame = analysis.load_runs(write_index(tmp_path, rows))
    assert frame.iloc[0]["sr_residence_fraction"] == pytest.approx(0.75)


def test_a_transfer_with_no_recorded_residence_is_not_a_division_by_zero(tmp_path):
    rows = [index_row(gbn_residence_s=0.0, sr_residence_s=0.0)]
    frame = analysis.load_runs(write_index(tmp_path, rows))
    assert frame.iloc[0]["sr_residence_fraction"] == 0.0


# ---------------------------------------------------------------------------
# The index still agrees with the logs (CC-06)
# ---------------------------------------------------------------------------


def write_events(directory, rows):
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "events.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "run_id", "endpoint", "event", "sequence",
                         "ack", "mode", "window_size", "loss_estimate", "rtt_ms",
                         "bytes", "reason"])
        writer.writerows(rows)


def a_small_run(tmp_path, *, run_id="r", mode_sequence=("gbn", "gbn")):
    """A minimal two-endpoint run: one segment sent, delivered and verified."""
    directory = tmp_path / run_id
    write_events(directory / "sender", [
        [0.000000, run_id, "sender", "START", "", "", mode_sequence[0], 8, "", "", "", ""],
        [0.100000, run_id, "sender", "SEND", 0, "", mode_sequence[0], 8, "", "", 1045, ""],
        [0.200000, run_id, "sender", "ACK", "", 0, mode_sequence[-1], 8, "", "", "", ""],
        [1.000000, run_id, "sender", "FIN_ACK", "", "", mode_sequence[-1], 8, "", "", "", "match"],
    ])
    write_events(directory / "receiver", [
        [0.000000, run_id, "receiver", "START", "", "", mode_sequence[0], 8, "", "", "", ""],
        [0.150000, run_id, "receiver", "DELIVER", 0, "", mode_sequence[0], 8, "", "", 1024, ""],
        [1.000000, run_id, "receiver", "FIN_ACK", "", "", mode_sequence[-1], 8, "", "", "", "match"],
    ])
    return directory


def test_the_index_is_checked_against_the_raw_logs(tmp_path):
    directory = a_small_run(tmp_path)
    rows = [index_row(run_id=directory.name, log_dir=str(directory),
                      completion_time_s=1.0, retransmission_count=0,
                      switch_count=0, retransmission_overhead=0.0,
                      goodput_bytes_per_s=1024.0)]
    frame = analysis.load_runs(write_index(tmp_path, rows))
    assert analysis.check_against_logs(frame) == []


def test_an_index_that_disagrees_with_the_logs_is_caught(tmp_path):
    """The whole value of the check: a number in the index that the events no
    longer support is a finding, not a rounding difference."""
    directory = a_small_run(tmp_path)
    rows = [index_row(run_id=directory.name, log_dir=str(directory),
                      completion_time_s=1.0, retransmission_count=99,
                      switch_count=0, retransmission_overhead=0.0,
                      goodput_bytes_per_s=1024.0)]
    frame = analysis.load_runs(write_index(tmp_path, rows))
    mismatches = analysis.check_against_logs(frame)
    assert [row["metric"] for row in mismatches] == ["retransmission_count"]
    assert mismatches[0]["index"] == 99 and mismatches[0]["from_logs"] == 0


# ---------------------------------------------------------------------------
# The mode timeline (T9.2, the §27 graph an aggregate cannot hold)
# ---------------------------------------------------------------------------


def test_mode_spans_come_from_the_logs_own_mode_column(tmp_path):
    directory = a_small_run(tmp_path, run_id="one_mode")
    spans, changes = analysis._mode_spans(directory / "sender" / "events.csv")
    assert [mode for _, _, mode in spans] == ["gbn"]
    assert changes == []


def test_a_switch_splits_the_timeline_into_two_bands(tmp_path):
    directory = tmp_path / "switched"
    write_events(directory / "sender", [
        [0.0, "s", "sender", "START", "", "", "gbn", 8, "", "", "", ""],
        [1.0, "s", "sender", "SEND", 0, "", "gbn", 8, "", "", 1045, ""],
        [2.0, "s", "sender", "SWITCH", 1, "", "sr", 8, 0.4, "", "", "MODE_THRESHOLD"],
        [3.0, "s", "sender", "SEND", 1, "", "sr", 8, "", "", 1045, ""],
    ])
    spans, _ = analysis._mode_spans(directory / "sender" / "events.csv")
    assert [mode for _, _, mode in spans] == ["gbn", "sr"]
    assert spans[0][0] == 0.0 and spans[0][1] == 2.0
    assert spans[1][1] == 3.0


def test_scheduled_loss_changes_are_read_from_the_log_not_the_config(tmp_path):
    """The timeline marks what the run actually met: a step the transfer ended
    before reaching leaves no LOSS_CHANGE row, and must not be drawn."""
    directory = tmp_path / "dynamic"
    write_events(directory / "sender", [
        [0.0, "d", "sender", "START", "", "", "gbn", 8, "", "", "", ""],
        [4.0, "d", "sender", "LOSS_CHANGE", "", "", "gbn", 8, "", "", "", "0.0000->0.1000"],
        [9.0, "d", "sender", "FIN_ACK", "", "", "gbn", 8, "", "", "", "match"],
    ])
    _, changes = analysis._mode_spans(directory / "sender" / "events.csv")
    assert [moment for moment, _ in changes] == [4.0]


# ---------------------------------------------------------------------------
# The graphs render (T9.2, T9.3)
# ---------------------------------------------------------------------------


def a_loss_sweep(tmp_path):
    rows = []
    for experiment, loss in zip(analysis.LOSS_EXPERIMENTS,
                                (0.0, 0.01, 0.02, 0.05, 0.10, 0.20)):
        for system in analysis.SYSTEM_ORDER:
            for trial in range(2):
                rows.append(index_row(
                    experiment=experiment, condition=f"loss{int(loss * 100):02d}",
                    system=system, trial=trial, loss_rate=loss,
                    run_id=f"{experiment}_{system}_{trial}",
                    completion_time_s=20.0 + 100 * loss + trial,
                    goodput_bytes_per_s=60000.0 - 200000 * loss))
    return analysis.load_runs(write_index(tmp_path, rows))


def test_every_required_graph_is_written(tmp_path):
    frame = a_loss_sweep(tmp_path)
    summary = analysis.aggregate(frame)
    plot_dir = tmp_path / "plots"
    for metric in ("goodput_kib_per_s", "completion_time_s",
                   "retransmission_count", "retransmission_overhead"):
        path = analysis.plot_metric_vs_loss(summary, metric, title="t",
                                            plot_dir=plot_dir)
        assert path.is_file() and path.stat().st_size > 0


def test_a_single_series_chart_still_renders(tmp_path):
    """The optional graphs plot the hybrid alone; a legend of one is dropped,
    and dropping it must not take the chart with it."""
    summary = analysis.aggregate(a_loss_sweep(tmp_path))
    path = analysis.plot_metric_vs_loss(summary, "switch_count", title="t",
                                        plot_dir=tmp_path / "plots",
                                        systems=("hybrid",), name="one.png")
    assert path.is_file()


def test_colours_are_assigned_in_a_fixed_order_and_never_shared():
    """A filter that changes which systems appear must not repaint the rest, so
    the colour belongs to the system and not to its position in a chart."""
    assert set(analysis.SERIES_COLOR) == set(analysis.SYSTEM_ORDER)
    assert len(set(analysis.SERIES_COLOR.values())) == len(analysis.SYSTEM_ORDER)
    assert len(set(analysis.SERIES_MARKER.values())) == len(analysis.SYSTEM_ORDER)


def test_the_mode_colours_match_the_pure_systems():
    """Colour follows the entity: the SR band in the timeline is the same hue as
    the SR line in every other figure."""
    assert analysis.MODE_COLOR["gbn"] == analysis.SERIES_COLOR["gbn"]
    assert analysis.MODE_COLOR["sr"] == analysis.SERIES_COLOR["sr"]


def test_a_long_subtitle_is_wrapped_rather_than_widening_the_figure():
    wrapped = analysis._wrap("word " * 60)
    assert max(len(line) for line in wrapped.splitlines()) <= 86


# ---------------------------------------------------------------------------
# The capture set (T9.4) and the dissector (T9.5)
# ---------------------------------------------------------------------------


def test_the_capture_set_covers_every_wireshark_requirement():
    """specs.md §22: WS-01 … WS-09, with WS-09 captured rather than skipped —
    E8 does produce a reverse transition, so "if produced" is satisfied."""
    covered = " ".join(s.requirements for s in capture.SCENARIOS)
    for number in range(1, 10):
        assert f"WS-0{number}" in covered


def test_the_evidence_set_has_a_scenario_for_every_expected_observation():
    """The four rows of specs.md §22.1, plus the reverse transition."""
    names = {s.name for s in capture.SCENARIOS}
    assert names == {"clean_gbn", "lossy_gbn_range_retx",
                     "lossy_sr_individual_retx", "hybrid_gbn_to_sr",
                     "hybrid_sr_to_gbn"}


def test_the_two_lossy_captures_meet_the_same_drop_stream():
    """Otherwise the GBN/SR comparison in the evidence set is two unrelated
    runs rather than one condition handled two ways (RP-04)."""
    gbn = next(s for s in capture.SCENARIOS if s.name == "lossy_gbn_range_retx")
    sr = next(s for s in capture.SCENARIOS if s.name == "lossy_sr_individual_retx")
    assert gbn.key == sr.key and gbn.seed == sr.seed
    assert gbn.file_bytes == sr.file_bytes and gbn.loss_rate == sr.loss_rate
    assert (runner.derive_seed(gbn.seed, "WS", gbn.key, 0)
            == runner.derive_seed(sr.seed, "WS", sr.key, 0))


def test_capture_run_ids_are_distinct_even_when_the_condition_is_shared():
    identifiers = {runner.run_id("WS", s.system, s.key, 0) for s in capture.SCENARIOS}
    assert len(identifiers) == len(capture.SCENARIOS)


def test_the_transition_captures_run_long_enough_to_switch():
    """A transition needs roughly 110 acknowledged segments to be confirmed
    (50-outcome window + 3 confirmations at 20), so a short transfer cannot
    produce the evidence WS-08 asks for however lossy it is."""
    for name in ("hybrid_gbn_to_sr", "hybrid_sr_to_gbn"):
        scenario = next(s for s in capture.SCENARIOS if s.name == name)
        assert scenario.file_bytes // capture.SEGMENT >= 512
        assert scenario.loss_schedule, "a transition needs a condition that changes"


def test_captures_use_the_fixed_development_port():
    """WS-02 and WS-03: `udp.port == 8888` has to be the right filter for every
    file in the set, or the evidence needs a different filter per capture."""
    import config
    assert config.PORT == 8888


def test_the_dissector_covers_every_frozen_header_field():
    """D1 is frozen; a dissector that omitted a field would make the capture
    less readable than the raw bytes it replaces."""
    source = (capture.DISSECTOR).read_text(encoding="utf-8")
    for field in ("hybridarq.magic", "hybridarq.version", "hybridarq.type",
                  "hybridarq.flags", "hybridarq.seq", "hybridarq.ack",
                  "hybridarq.window", "hybridarq.payload_length",
                  "hybridarq.checksum", "hybridarq.payload"):
        assert field in source


def test_the_dissector_names_every_packet_type():
    from protocol.packet import PacketType
    source = capture.DISSECTOR.read_text(encoding="utf-8")
    for member in PacketType:
        assert f'[{member.value}] = "{member.name}"' in source


def test_the_dissector_agrees_with_the_frozen_header_size():
    from protocol.packet import HEADER_SIZE
    source = capture.DISSECTOR.read_text(encoding="utf-8")
    assert f"local HEADER_SIZE = {HEADER_SIZE}" in source


def test_a_capture_summary_counts_wire_retransmissions(tmp_path, monkeypatch):
    """`resent on the wire` is what the capture shows, not what the log claims:
    a dropped segment never reached the wire, so resending it is not a duplicate
    sequence there. The distinction is the whole point of the GBN/SR pair."""
    fake = "DATA\t0\t\nDATA\t1\t\nDATA\t0\t\nACK\t\t0\n"

    class Result:
        stdout = fake

    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: Result())
    path = tmp_path / "x.pcapng"
    path.write_bytes(b"not really a capture")
    summary = capture.summarise(path)
    assert summary["packets"] == 4
    assert summary["unique_data"] == 2
    assert summary["resent_on_the_wire"] == 1
