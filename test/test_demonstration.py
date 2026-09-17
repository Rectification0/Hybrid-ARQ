"""Demonstration-rehearsal tests (T10.6, specs.md §28).

The rehearsal itself runs three 1 MiB transfers and is not run here. What is
tested is what makes it a *rehearsal* rather than an audition: that the condition
is fixed in the source rather than chosen per run, that it actually exercises
both transitions, and that the transcript reports whatever happened — including
a run where the reverse transition did not occur, which is the case a
demonstration is most tempted to quietly drop.
"""

import pytest

import config
from experiments import demonstrate


def event(**changes):
    row = {"timestamp": "1.0", "run_id": "d", "endpoint": "sender", "event": "SEND",
           "sequence": "", "ack": "", "mode": "gbn", "window_size": "8",
           "loss_estimate": "", "rtt_ms": "", "bytes": "", "reason": ""}
    row.update({key: str(value) for key, value in changes.items()})
    return row


def outcome(events, **row_changes):
    row = {"completion_time_s": 25.0, "goodput_bytes_per_s": 42000.0,
           "retransmission_count": 88, "retransmission_overhead": 0.08,
           "switch_count": 2, "integrity_success": True, "delivered_bytes": 1048576,
           "log_dir": "logs/demo/x"}
    row.update(row_changes)
    return demonstrate.Outcome(system="hybrid", row=row, events=events)


# ---------------------------------------------------------------------------
# The condition is fixed, and exercises both directions
# ---------------------------------------------------------------------------


def test_the_schedule_goes_up_and_then_back_down():
    """§28 asks for a transition *and* a reverse transition. A schedule that only
    rises can demonstrate half the controller."""
    rates = [rate for _, rate in demonstrate.LOSS_SCHEDULE]
    assert demonstrate.INITIAL_LOSS == 0.0
    assert max(rates) > demonstrate.INITIAL_LOSS
    assert rates[-1] == 0.0


def test_the_lossy_phase_is_long_enough_to_be_detected():
    """The controller needs roughly 110 acknowledged segments to confirm a
    change; a schedule that reverts before that would demonstrate nothing."""
    start, _ = demonstrate.LOSS_SCHEDULE[0]
    end, _ = demonstrate.LOSS_SCHEDULE[1]
    assert end - start >= 10.0


def test_the_transfer_is_large_enough_to_outlast_the_schedule():
    """A 1 MiB transfer at 100 ms RTT runs ~16 s clean and longer under loss, so
    the last step has to fall well inside it."""
    segments = demonstrate.FILE_BYTES // config.SEGMENT_SIZE
    assert segments >= 1024
    assert demonstrate.LOSS_SCHEDULE[-1][0] < 25.0


def test_the_seed_is_fixed_in_the_source():
    """A rehearsal that picks a fresh seed each time is not a rehearsal of the
    run that will be demonstrated."""
    assert isinstance(demonstrate.SEED, int)
    first = demonstrate._condition("demo")
    assert first == demonstrate._condition("demo")
    assert first.loss_schedule == demonstrate.LOSS_SCHEDULE


def test_the_rto_comes_from_the_frozen_policy():
    assert config.baseline_rto(demonstrate.RTT_MS) == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Reading the run back
# ---------------------------------------------------------------------------


def test_switches_exclude_the_fixed_hybrid_no_op():
    """A SWITCH row whose reason is FIXED_HYBRID_NOOP is the control paying the
    drain cost without changing mode; counting it as a transition would make the
    demonstration claim something that did not happen."""
    events = [
        event(event="SWITCH", mode="sr", reason="MODE_THRESHOLD;from=gbn;epoch=1"),
        event(event="SWITCH", mode="gbn", reason="FIXED_HYBRID_NOOP;epoch=2"),
    ]
    assert len(outcome(events).switches) == 1


def test_loss_changes_are_read_from_the_log(tmp_path):
    events = [event(event="LOSS_CHANGE", timestamp=6.09, reason="0.0000->0.1000"),
              event(event="LOSS_CHANGE", timestamp=18.08, reason="0.1000->0.0000")]
    assert len(outcome(events).loss_changes) == 2


# ---------------------------------------------------------------------------
# The transcript
# ---------------------------------------------------------------------------


def a_result(steps=13):
    hybrid = outcome([event(event="SWITCH", mode="sr", reason="MODE_THRESHOLD")])
    baselines = {
        "gbn": outcome([], completion_time_s=25.6, retransmission_count=168,
                       switch_count=0),
        "sr": outcome([], completion_time_s=23.7, retransmission_count=33,
                      switch_count=0),
    }
    return {
        "steps": [f"| {n} | step {n} | detail |" for n in range(1, steps + 1)],
        "hybrid": hybrid,
        "baselines": baselines,
        "comparison": demonstrate._comparison_table(hybrid, baselines),
        "capture": None,
    }


def test_the_transcript_records_all_thirteen_steps(tmp_path):
    path = demonstrate.write_transcript(a_result(), tmp_path / "demo.md")
    text = path.read_text(encoding="utf-8")
    for number in range(1, 14):
        assert f"| {number} | step {number} |" in text


def test_the_transcript_compares_all_three_systems(tmp_path):
    text = demonstrate.write_transcript(
        a_result(), tmp_path / "demo.md").read_text(encoding="utf-8")
    for system in ("pure GBN", "pure SR", "hybrid"):
        assert system in text


def test_the_transcript_says_where_the_margin_comes_from(tmp_path):
    """The demonstration condition is lossy for only part of the run, so the
    separation is small. Saying so is the difference between a rehearsal and a
    sales pitch — and the matrix, not the demo, carries the performance claim."""
    text = demonstrate.write_transcript(
        a_result(), tmp_path / "demo.md").read_text(encoding="utf-8")
    assert "margin here is small" in text
    assert "1.56x and 2.00x pure GBN" in text


def test_a_rehearsal_without_a_capture_says_so(tmp_path):
    text = demonstrate.write_transcript(
        a_result(), tmp_path / "demo.md").read_text(encoding="utf-8")
    assert "No capture was taken" in text


def test_the_comparison_table_carries_the_integrity_result(tmp_path):
    """CC-01 applies to the demonstration too: a transfer whose hash did not
    match must not be presented as a completed run."""
    rows = a_result()["comparison"]
    assert all("integrity" in row for row in rows)
    assert {row["system"] for row in rows} == {"pure GBN", "pure SR", "hybrid"}
