"""Experiment harness tests (T8.1, T8.2, T8.3, T8.7).

The matrix itself is hours of transfers and is not run here. What is tested is
everything that decides what the matrix *means*:

* the fairness invariants of specs.md §18 and RP-04 — one source file, one
  window, one derived RTO and **one seed** per cell, shared by every system in
  it. That is the property that makes the comparison controlled rather than
  three unrelated sets of runs, so it is asserted rather than assumed;
* the seed derivation that makes the whole matrix replayable from one recorded
  base seed (D13, RP-03);
* config validation, because a silently ignored key means the run did not use
  the condition its file describes;
* the shipped E1-E8 configs, against the matrix in specs.md §19;
* the T8.7 audit: an integrity failure is never an ``ok`` row (CC-01), and an
  edited raw log is detected rather than averaged into a result (RP-07).

One end-to-end run is included — small, clean, over loopback — because the
orchestration itself (bind, read the port back, start the sender, collect two
logs) is the part no unit test can stand in for.
"""

import csv
import json
import pathlib

import pytest

import config
from experiments import run_experiment as runner


# ---------------------------------------------------------------------------
# Config loading and validation
# ---------------------------------------------------------------------------


def write_config(tmp_path, document, name="X1.json"):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


MINIMAL = {
    "experiment": "X1",
    "systems": ["gbn", "sr", "hybrid"],
    "trials": 2,
    "file_size_bytes": 4096,
    "base_seed": 5,
    "conditions": [{"name": "loss05", "loss_rate": 0.05, "rtt_ms": 100.0}],
}


def test_a_config_round_trips_into_conditions(tmp_path):
    experiment = runner.load_experiment(write_config(tmp_path, MINIMAL))
    assert experiment.experiment == "X1"
    assert experiment.trials == 2
    assert [c.name for c in experiment.conditions] == ["loss05"]
    assert experiment.conditions[0].loss_rate == 0.05
    assert experiment.conditions[0].rtt_ms == 100.0


def test_an_unknown_config_key_is_refused(tmp_path):
    path = write_config(tmp_path, {**MINIMAL, "trial_count": 3})
    with pytest.raises(ValueError, match="unknown config key"):
        runner.load_experiment(path)


def test_an_unknown_condition_key_is_refused(tmp_path):
    document = {**MINIMAL, "conditions": [{"name": "c", "loss": 0.05}]}
    with pytest.raises(ValueError, match="unknown condition key"):
        runner.load_experiment(write_config(tmp_path, document))


def test_the_phase_2_placeholder_is_not_a_system_under_evaluation(tmp_path):
    """`--mode saw` moved data before either real mode existed; it is not a
    baseline being measured (specs.md §18)."""
    document = {**MINIMAL, "systems": ["gbn", "saw"]}
    with pytest.raises(ValueError, match="not a system under evaluation"):
        runner.load_experiment(write_config(tmp_path, document))


def test_a_repeated_condition_name_is_refused(tmp_path):
    """Two conditions with one name would generate one run_id, and the second
    run would overwrite the first's logs."""
    document = {**MINIMAL, "conditions": [{"name": "c"}, {"name": "c"}]}
    with pytest.raises(ValueError, match="repeats a condition name"):
        runner.load_experiment(write_config(tmp_path, document))


def test_a_condition_without_a_name_is_refused(tmp_path):
    document = {**MINIMAL, "conditions": [{"loss_rate": 0.1}]}
    with pytest.raises(ValueError, match="no name"):
        runner.load_experiment(write_config(tmp_path, document))


def test_the_loss_schedule_is_sorted_and_rendered_for_the_cli(tmp_path):
    document = {**MINIMAL, "conditions": [
        {"name": "dyn", "loss_rate": 0.0, "loss_schedule": [[9.0, 0.02], [4.0, 0.1]]}]}
    condition = runner.load_experiment(write_config(tmp_path, document)).conditions[0]
    assert condition.loss_schedule == ((4.0, 0.1), (9.0, 0.02))
    assert condition.schedule_text() == "4:0.1,9:0.02"


def test_the_rto_is_derived_from_the_conditions_rtt(tmp_path):
    """D7: one RTO per condition, not one constant for the matrix."""
    document = {**MINIMAL, "conditions": [
        {"name": "fast", "rtt_ms": 10.0}, {"name": "slow", "rtt_ms": 500.0}]}
    fast, slow = runner.load_experiment(write_config(tmp_path, document)).conditions
    assert fast.rto_s == config.baseline_rto(10.0) == 0.200
    assert slow.rto_s == config.baseline_rto(500.0) == 2.0


# ---------------------------------------------------------------------------
# Identity and reproducibility (D13, RP-03, RP-04)
# ---------------------------------------------------------------------------


def test_seed_derivation_is_deterministic():
    assert runner.derive_seed(7, "E4", "loss05", 2) == runner.derive_seed(7, "E4", "loss05", 2)


def test_seeds_differ_across_experiments_conditions_and_trials():
    seeds = {runner.derive_seed(7, experiment, condition, trial)
             for experiment in ("E4", "E7")
             for condition in ("loss05", "rtt100")
             for trial in range(5)}
    assert len(seeds) == 20, "two cells would share an impairment stream"


def test_a_different_base_seed_gives_a_different_matrix():
    assert runner.derive_seed(1, "E4", "loss05", 0) != runner.derive_seed(2, "E4", "loss05", 0)


def test_every_system_in_a_cell_meets_the_same_impairment_stream(tmp_path):
    """RP-04, and the single property that makes the matrix a controlled
    comparison: the system name is not an input to the seed."""
    experiment = runner.load_experiment(write_config(tmp_path, MINIMAL))
    runs = runner.plan([experiment])
    per_cell = {}
    for run in runs:
        per_cell.setdefault((run.condition.name, run.trial), set()).add(run.seed)
    assert all(len(seeds) == 1 for seeds in per_cell.values())


def test_run_ids_are_unique_across_the_whole_matrix():
    runs = runner.plan(runner.load_all())
    identifiers = [run.run_id for run in runs]
    assert len(set(identifiers)) == len(identifiers), "two runs would share a log directory"


def test_a_run_id_names_experiment_system_condition_and_trial():
    assert runner.run_id("E4", "hybrid", "loss05", 3) == "E4_hybrid_loss05_trial03"


def test_the_plan_completes_whole_cells_before_moving_on(tmp_path):
    """Interrupting the matrix must leave comparable cells behind, so the
    systems of one trial run together rather than one system at a time."""
    experiment = runner.load_experiment(write_config(tmp_path, MINIMAL))
    systems = [run.system for run in runner.plan([experiment])]
    assert systems == ["gbn", "sr", "hybrid"] * 2


def test_the_source_file_is_deterministic_and_shared(tmp_path):
    first = runner.source_file(tmp_path, 8192)
    content = first.read_bytes()
    assert runner.source_file(tmp_path, 8192) == first
    assert first.read_bytes() == content and len(content) == 8192


def test_the_source_file_is_not_compressible_padding(tmp_path):
    """A file of zeros would hide a truncated transfer from the hash check."""
    content = runner.source_file(tmp_path, 4096).read_bytes()
    assert len(set(content)) > 128


# ---------------------------------------------------------------------------
# The shipped configs (T8.3) against specs.md §19
# ---------------------------------------------------------------------------


def test_every_experiment_in_the_matrix_has_a_config():
    names = {experiment.experiment for experiment in runner.load_all()}
    assert names == {f"E{number}" for number in range(1, 9)}


def test_the_loss_sweep_covers_the_grid_of_specs_17_1():
    losses = {}
    for experiment in runner.load_all():
        if experiment.experiment in {f"E{n}" for n in range(1, 7)}:
            assert len(experiment.conditions) == 1
            losses[experiment.experiment] = experiment.conditions[0].loss_rate
    assert sorted(losses.values()) == [0.0, 0.01, 0.02, 0.05, 0.10, 0.20]


def test_the_loss_sweep_holds_rtt_fixed():
    """E1-E6 vary loss at one RTT; varying both at once would confound them."""
    rtts = {condition.rtt_ms
            for experiment in runner.load_all()
            if experiment.experiment in {f"E{n}" for n in range(1, 7)}
            for condition in experiment.conditions}
    assert rtts == {100.0}


def test_e7_sweeps_rtt_at_one_fixed_loss():
    experiment = next(e for e in runner.load_all() if e.experiment == "E7")
    assert {c.rtt_ms for c in experiment.conditions} == {10.0, 50.0, 100.0, 500.0}
    assert len({c.loss_rate for c in experiment.conditions}) == 1


def test_e8_is_dynamic_and_covers_rising_falling_and_changing_loss():
    experiment = next(e for e in runner.load_all() if e.experiment == "E8")
    assert all(c.loss_schedule for c in experiment.conditions)
    assert len(experiment.conditions) >= 3
    rises = [c for c in experiment.conditions
             if max(rate for _, rate in c.loss_schedule) > c.loss_rate]
    falls = [c for c in experiment.conditions
             if min(rate for _, rate in c.loss_schedule) < c.loss_rate]
    assert rises and falls, "specs.md §17.3 asks for both directions"
    assert any(len(c.loss_schedule) > 1 for c in experiment.conditions)


def test_e8_measures_the_adaptive_system():
    experiment = next(e for e in runner.load_all() if e.experiment == "E8")
    assert "hybrid" in experiment.systems


def test_every_cell_compares_the_three_systems_of_specs_18():
    for experiment in runner.load_all():
        if experiment.experiment == "E8":
            continue
        assert {"gbn", "sr", "hybrid"} <= set(experiment.systems)


def test_every_config_holds_file_window_and_scale_identical_across_systems():
    """specs.md §18: the comparison is only fair if the cell differs by system
    and nothing else. The config format has no per-system override at all, which
    is the structural version of that rule — this pins the values it does hold."""
    for experiment in runner.load_all():
        assert experiment.window_size == config.WINDOW_SIZE
        assert experiment.file_size_bytes == config.TRANSFER_FILE_SIZE_BYTES
        assert experiment.trials == config.TRIAL_COUNT


def test_the_configs_record_the_frozen_scale_decisions():
    """D12 and D14 are frozen in config.py; the configs must not quietly differ
    from the value recorded in every summary.json."""
    for experiment in runner.load_all():
        assert experiment.trials == config.TRIAL_COUNT, experiment.experiment
        assert experiment.file_size_bytes == config.TRANSFER_FILE_SIZE_BYTES


def test_one_base_seed_covers_the_whole_matrix():
    seeds = {experiment.base_seed for experiment in runner.load_all()}
    assert len(seeds) == 1, "D13: one recorded base seed replays every experiment"


def test_the_abort_timeout_is_recorded_in_every_config():
    for experiment in runner.load_all():
        assert experiment.abort_timeout_s == config.EXPERIMENT_ABORT_TIMEOUT_S


# ---------------------------------------------------------------------------
# Verification (T8.7)
# ---------------------------------------------------------------------------


def index_row(tmp_path, **changes):
    directory = tmp_path / changes.get("run_id", "r1")
    for endpoint in ("sender", "receiver"):
        (directory / endpoint).mkdir(parents=True, exist_ok=True)
        (directory / endpoint / "events.csv").write_text(
            f"timestamp,run_id,endpoint\n0.1,r1,{endpoint}\n", encoding="utf-8")
    row = {
        "run_id": "r1", "status": runner.STATUS_OK, "error": "",
        "log_dir": str(directory),
        "sender_events_sha256": runner._sha256(directory / "sender" / "events.csv"),
        "receiver_events_sha256": runner._sha256(directory / "receiver" / "events.csv"),
    }
    row.update(changes)
    return row


def test_the_recorded_log_path_is_portable(tmp_path):
    """The index is committed, so a path inside the repo is recorded relative to
    it — an absolute path would name one machine's home directory."""
    inside = runner.PROJECT_ROOT / "logs" / "experiments" / "E1_gbn_loss00_trial00"
    assert runner._portable(inside) == str(
        pathlib.Path("logs") / "experiments" / "E1_gbn_loss00_trial00")
    assert runner._resolve(runner._portable(inside)) == inside


def test_an_absolute_log_path_still_resolves(tmp_path):
    """Rows recorded before paths were made relative must still verify."""
    assert runner._resolve(str(tmp_path)) == tmp_path


def test_verification_passes_on_untouched_logs(tmp_path):
    report = runner.verify([index_row(tmp_path)])
    assert report["ok"] == 1 and not report["altered_logs"] and not report["missing_logs"]


def test_an_edited_log_is_detected(tmp_path):
    row = index_row(tmp_path)
    path = tmp_path / "r1" / "sender" / "events.csv"
    path.write_text(path.read_text(encoding="utf-8") + "0.2,r1,sender\n", encoding="utf-8")
    report = runner.verify([row])
    assert report["altered_logs"] == ["r1/sender"], "RP-07: raw logs are never edited"


def test_a_deleted_log_is_detected(tmp_path):
    row = index_row(tmp_path)
    (tmp_path / "r1" / "receiver" / "events.csv").unlink()
    assert runner.verify([row])["missing_logs"] == ["r1/receiver"]


def test_a_failed_run_is_reported_rather_than_counted_as_ok(tmp_path):
    rows = [index_row(tmp_path), index_row(tmp_path, run_id="r2",
                                           status=runner.STATUS_INTEGRITY)]
    report = runner.verify(rows)
    assert report["ok"] == 1 and len(report["failures"]) == 1


def test_an_integrity_failure_is_never_an_ok_row(tmp_path):
    """CC-01: a hash mismatch is always a reported failure. The sender exiting
    zero is not enough on its own to call a run successful."""
    experiment = runner.load_experiment(write_config(tmp_path, MINIMAL))
    run = runner.plan([experiment])[0]
    logs = tmp_path / "logs"
    (logs / run.run_id / "sender").mkdir(parents=True)
    (logs / run.run_id / "sender" / "events.csv").write_text(
        "timestamp,run_id,endpoint,event,sequence,ack,mode,window_size,"
        "loss_estimate,rtt_ms,bytes,reason\n"
        "0.100000,r,sender,SEND,0,,gbn,8,,,1045,\n", encoding="utf-8")
    source = runner.source_file(tmp_path, 4096)
    row = runner._record(run, log_dir=logs, source=source, status=runner.STATUS_OK,
                         error="", wall_clock=1.0, started_utc=0.0)
    assert row["status"] == runner.STATUS_INTEGRITY
    assert row["integrity_success"] is not True


def test_a_run_that_wrote_no_log_is_still_recorded(tmp_path):
    experiment = runner.load_experiment(write_config(tmp_path, MINIMAL))
    run = runner.plan([experiment])[0]
    row = runner._record(run, log_dir=tmp_path / "logs",
                         source=runner.source_file(tmp_path, 4096),
                         status=runner.STATUS_OK, error="", wall_clock=1.0,
                         started_utc=0.0)
    assert row["status"] == runner.STATUS_FAILED and row["error"]


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


def test_the_index_appends_and_can_be_resumed(tmp_path):
    index = runner.RunIndex(tmp_path / "runs.csv")
    index.open()
    index.write({"run_id": "a", "status": "ok"})
    index.close()

    again = runner.RunIndex(tmp_path / "runs.csv")
    assert again.existing_ids() == {"a"}
    again.open()
    again.write({"run_id": "b", "status": "ok"})
    again.close()

    with open(tmp_path / "runs.csv", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["run_id"] for row in rows] == ["a", "b"]


def test_the_index_records_every_column_the_analysis_needs():
    """T9.1 aggregates per condition across trials; the columns it groups by
    have to be in the index rather than only in each run's summary.json."""
    for column in ("experiment", "condition", "system", "trial", "loss_rate",
                   "rtt_ms", "goodput_bytes_per_s", "completion_time_s",
                   "retransmission_count", "retransmission_overhead",
                   "switch_count", "integrity_success", "status"):
        assert column in runner.ROW_FIELDS


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_a_clean_transfer_runs_end_to_end_through_the_cli(tmp_path):
    """The orchestration itself: bind, read the port back, run the sender, and
    derive the row from the two logs the processes wrote."""
    document = {
        "experiment": "T8", "systems": ["gbn"], "trials": 1,
        "file_size_bytes": 16384, "base_seed": 3, "abort_timeout_s": 60,
        "port": 0,
        "conditions": [{"name": "clean"}],
    }
    experiment = runner.load_experiment(write_config(tmp_path, document))
    run = runner.plan([experiment])[0]
    source = runner.source_file(tmp_path / "sources", experiment.file_size_bytes)

    row = runner.execute_run(run, log_dir=tmp_path / "logs", source=source)

    assert row["status"] == runner.STATUS_OK
    assert row["integrity_success"] is True
    assert row["delivered_bytes"] == 16384
    assert row["sender_events_sha256"] and row["receiver_events_sha256"]
    assert (tmp_path / "logs" / run.run_id / "receiver" / "summary.json").is_file()
