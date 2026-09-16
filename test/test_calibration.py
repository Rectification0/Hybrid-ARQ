"""Calibration harness tests (T7.1, T7.2).

The sweep itself is several hundred transfers and is not run here. What is
tested is everything that decides what the sweep *means*: the grids, the seed
derivation that makes it replayable, the aggregation, and above all the
selection rule — because a rule stated in a docstring and not enforced by code
is a rule that quietly bends toward whichever setting won.

The scoring tests use synthetic rows rather than measured ones, so they pin the
rule's behaviour independently of what the real data happened to say.
"""

import pytest

import config
from experiments import calibrate_thresholds as cal


# ---------------------------------------------------------------------------
# Grids and settings
# ---------------------------------------------------------------------------


def test_entry_grid_covers_high_and_hysteresis_only():
    settings = cal.entry_settings(switch_low=0.02)
    assert len(settings) == len(cal.SWITCH_HIGH_GRID) * len(cal.HYSTERESIS_GRID)
    assert {s.switch_high for s in settings} == set(cal.SWITCH_HIGH_GRID)
    assert {s.hysteresis_count for s in settings} == set(cal.HYSTERESIS_GRID)


def test_entry_grid_never_inverts_the_dead_band():
    """SWITCH_LOW must stay at or below SWITCH_HIGH even when the fixed exit
    threshold is above the entry threshold being tried."""
    for setting in cal.entry_settings(switch_low=0.10):
        assert setting.switch_low <= setting.switch_high
        setting.as_settings()        # constructs, so the pair is valid


def test_exit_grid_is_anchored_to_the_chosen_entry_threshold():
    settings = cal.exit_settings(switch_high=0.20)
    assert {s.switch_high for s in settings} == {0.20}
    assert all(s.switch_low <= 0.20 for s in settings)
    assert {s.switch_low for s in settings} == {
        low for low in cal.SWITCH_LOW_GRID if low <= 0.20}


def test_a_setting_carries_the_values_held_fixed_across_the_sweep():
    """The cadence and the minimum residence are not swept, so every run must
    use the same ones — that is what makes them frozen by the same evidence."""
    settings = cal.Setting(0.2, 0.05, 2).as_settings()
    assert settings.evaluation_interval_segments == cal.EVALUATION_INTERVAL_SEGMENTS
    assert settings.min_mode_residence_s == cal.MIN_MODE_RESIDENCE_S
    assert settings.loss_window_size == config.LOSS_WINDOW_SIZE
    assert (settings.switch_high, settings.switch_low, settings.hysteresis_count) \
        == (0.2, 0.05, 2)


def test_setting_labels_are_unique_across_the_entry_grid():
    labels = [s.label() for s in cal.entry_settings()]
    assert len(set(labels)) == len(labels), "two settings would share a run_id"


# ---------------------------------------------------------------------------
# Reproducibility (RP-03)
# ---------------------------------------------------------------------------


def test_seed_derivation_is_deterministic():
    first = cal.derive_seed(20260916, "static/0.1", 2)
    assert first == cal.derive_seed(20260916, "static/0.1", 2)


def test_seeds_differ_across_conditions_and_trials():
    seeds = {cal.derive_seed(7, f"static/{loss}", trial)
             for loss in (0.0, 0.01, 0.05, 0.1, 0.2) for trial in range(3)}
    assert len(seeds) == 15, "two conditions would share an impairment stream"


def test_a_different_base_seed_gives_a_different_sweep():
    assert cal.derive_seed(1, "static/0.1", 0) != cal.derive_seed(2, "static/0.1", 0)


def test_the_same_seed_is_used_for_every_system_in_a_cell():
    """Fairness within a cell (RP-04): the comparison is only controlled if every
    system meets the same impairment stream."""
    assert cal.derive_seed(3, "static/0.05", 1) == cal.derive_seed(3, "static/0.05", 1)


def test_the_source_file_is_deterministic_and_reused(tmp_path):
    first = cal.source_file(tmp_path, 4096)
    content = first.read_bytes()
    again = cal.source_file(tmp_path, 4096)
    assert again == first and again.read_bytes() == content
    assert len(content) == 4096


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def row(**changes):
    base = {
        "stage": "entry", "run_id": "r", "system": "hybrid", "loss_rate": "0.05",
        "loss_schedule": "", "rtt_ms": "0.0", "switch_high": "0.2",
        "switch_low": "0.02", "hysteresis_count": "2",
        "evaluation_interval_segments": "20", "min_mode_residence_s": "1.0",
        "trial": "0", "seed": "1", "file_bytes": "262144", "window_size": "8",
        "rto_s": "0.2", "completion_time_s": "4.0", "goodput_bytes_per_s": "65536",
        "retransmission_count": "100", "retransmission_overhead": "0.3",
        "switch_count": "1", "handshake_count": "1", "abandoned_switches": "0",
        "gbn_residence_s": "1.5", "sr_residence_s": "2.5", "final_mode": "sr",
        "integrity_success": "True", "error": "",
    }
    base.update({key: str(value) for key, value in changes.items()})
    return base


def test_aggregate_means_across_trials():
    rows = [row(trial=0, goodput_bytes_per_s=100), row(trial=1, goodput_bytes_per_s=200)]
    summary = cal.aggregate(rows)
    assert len(summary) == 1
    assert summary[0]["goodput_mean"] == pytest.approx(150.0)
    assert summary[0]["trials"] == 2


def test_aggregate_keeps_settings_apart():
    rows = [row(switch_high=0.2), row(switch_high=0.35)]
    assert len(cal.aggregate(rows)) == 2


def test_a_failed_run_is_counted_and_left_out_of_goodput():
    """A corrupted transfer has no meaningful throughput; averaging it in would
    quietly reward a setting for failing fast (design.md §10, CC-01)."""
    rows = [row(trial=0, goodput_bytes_per_s=100),
            row(trial=1, goodput_bytes_per_s=0, integrity_success="False",
                error="boom")]
    summary = cal.aggregate(rows)[0]
    assert summary["integrity_failures"] == 1
    assert summary["goodput_mean"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# The selection rule (T7.2)
# ---------------------------------------------------------------------------


def synthetic_summary(settings_goodput, *, baselines, losses=("0.0", "0.05", "0.2"),
                      switches=None, failures=None):
    """Build an aggregate table directly, so the rule is tested on known input."""
    summary = []
    for loss in losses:
        for system, goodput in baselines.items():
            summary.append({
                "stage": "entry", "system": system, "switch_high": "",
                "switch_low": "", "hysteresis_count": "", "loss_rate": loss,
                "rtt_ms": "0.0", "trials": 3, "integrity_failures": 0,
                "goodput_mean": goodput[loss], "goodput_stdev": 0.0,
                "completion_mean_s": 1.0, "retransmissions_mean": 0.0,
                "overhead_mean": 0.0, "switches_mean": 0.0,
                "gbn_residence_mean_s": 0.0, "sr_residence_mean_s": 0.0,
            })
    for key, goodput in settings_goodput.items():
        high, low, count = key
        for loss in losses:
            summary.append({
                "stage": "entry", "system": "hybrid", "switch_high": str(high),
                "switch_low": str(low), "hysteresis_count": str(count),
                "loss_rate": loss, "rtt_ms": "0.0", "trials": 3,
                "integrity_failures": (failures or {}).get(key, {}).get(loss, 0),
                "goodput_mean": goodput[loss], "goodput_stdev": 0.0,
                "completion_mean_s": 1.0, "retransmissions_mean": 0.0,
                "overhead_mean": 0.0,
                "switches_mean": (switches or {}).get(key, {}).get(loss, 1.0),
                "gbn_residence_mean_s": 0.0, "sr_residence_mean_s": 0.0,
            })
    return summary


BASELINES = {
    "gbn": {"0.0": 100.0, "0.05": 50.0, "0.2": 20.0},
    "sr":  {"0.0": 90.0, "0.05": 70.0, "0.2": 60.0},
}


def test_score_is_the_ratio_to_the_better_fixed_baseline():
    """1.0 means "as good as whichever fixed strategy was right here, everywhere"."""
    summary = synthetic_summary(
        {(0.2, 0.02, 2): {"0.0": 100.0, "0.05": 70.0, "0.2": 60.0}},
        baselines=BASELINES,
        switches={(0.2, 0.02, 2): {"0.0": 0.0, "0.05": 1.0, "0.2": 1.0}})
    scored = cal.score_settings(summary)
    assert len(scored) == 1
    assert scored[0]["score"] == pytest.approx(1.0)
    assert scored[0]["eligible"]


def test_a_setting_that_beats_neither_baseline_scores_below_one():
    summary = synthetic_summary(
        {(0.05, 0.02, 1): {"0.0": 80.0, "0.05": 40.0, "0.2": 15.0}},
        baselines=BASELINES,
        switches={(0.05, 0.02, 1): {"0.0": 0.0, "0.05": 1.0, "0.2": 1.0}})
    assert cal.score_settings(summary)[0]["score"] < 1.0


def test_switching_under_quiet_conditions_is_disqualifying():
    """At 0-1% loss GBN is the right answer and a switch only pays the drain."""
    summary = synthetic_summary(
        {(0.05, 0.02, 1): {"0.0": 100.0, "0.05": 70.0, "0.2": 60.0}},
        baselines=BASELINES,
        switches={(0.05, 0.02, 1): {"0.0": 1.0, "0.05": 1.0, "0.2": 1.0}})
    scored = cal.score_settings(summary)
    assert not scored[0]["eligible"]
    assert "0-1%" in scored[0]["disqualified_for"]


def test_oscillation_under_a_static_condition_is_disqualifying():
    """A static condition justifies one entry switch, not a sequence of them."""
    summary = synthetic_summary(
        {(0.2, 0.15, 1): {"0.0": 100.0, "0.05": 70.0, "0.2": 60.0}},
        baselines=BASELINES,
        switches={(0.2, 0.15, 1): {"0.0": 0.0, "0.05": 4.0, "0.2": 5.0}})
    scored = cal.score_settings(summary)
    assert not scored[0]["eligible"]
    assert "switches" in scored[0]["disqualified_for"]


def test_an_integrity_failure_is_disqualifying():
    """No goodput number buys back a corrupted transfer (CC-01)."""
    summary = synthetic_summary(
        {(0.2, 0.02, 1): {"0.0": 100.0, "0.05": 999.0, "0.2": 999.0}},
        baselines=BASELINES,
        switches={(0.2, 0.02, 1): {"0.0": 0.0, "0.05": 1.0, "0.2": 1.0}},
        failures={(0.2, 0.02, 1): {"0.2": 1}})
    scored = cal.score_settings(summary)
    assert not scored[0]["eligible"]
    assert "integrity" in scored[0]["disqualified_for"]


def test_choose_prefers_the_best_score():
    summary = synthetic_summary(
        {(0.2, 0.02, 2): {"0.0": 100.0, "0.05": 70.0, "0.2": 60.0},
         (0.5, 0.02, 2): {"0.0": 100.0, "0.05": 50.0, "0.2": 25.0}},
        baselines=BASELINES,
        switches={(0.2, 0.02, 2): {"0.0": 0.0, "0.05": 1.0, "0.2": 1.0},
                  (0.5, 0.02, 2): {"0.0": 0.0, "0.05": 0.0, "0.2": 1.0}})
    chosen = cal.choose_setting(cal.score_settings(summary))
    assert (chosen["switch_high"], chosen["hysteresis_count"]) == (0.2, 2)


def test_a_near_tie_is_broken_by_fewer_switches():
    """Within the tolerance the two settings perform the same, so the tie-break
    is stability: the one that disturbed the transfer less."""
    steady = {"0.0": 100.0, "0.05": 70.0, "0.2": 60.0}
    nearly = {"0.0": 100.0, "0.05": 69.5, "0.2": 59.5}
    summary = synthetic_summary(
        {(0.2, 0.02, 3): nearly, (0.35, 0.02, 1): steady},
        baselines=BASELINES,
        switches={(0.2, 0.02, 3): {"0.0": 0.0, "0.05": 1.0, "0.2": 1.0},
                  (0.35, 0.02, 1): {"0.0": 0.0, "0.05": 2.0, "0.2": 2.0}})
    chosen = cal.choose_setting(cal.score_settings(summary))
    assert chosen["switch_high"] == 0.2, "fewer switches wins a near tie"


def test_a_tie_on_switches_is_broken_by_the_wider_dead_band():
    identical = {"0.0": 100.0, "0.05": 70.0, "0.2": 60.0}
    summary = synthetic_summary(
        {(0.2, 0.10, 2): identical, (0.2, 0.02, 2): identical},
        baselines=BASELINES,
        switches={(0.2, 0.10, 2): {"0.0": 0.0, "0.05": 1.0, "0.2": 1.0},
                  (0.2, 0.02, 2): {"0.0": 0.0, "0.05": 1.0, "0.2": 1.0}})
    chosen = cal.choose_setting(cal.score_settings(summary))
    assert chosen["switch_low"] == 0.02, "the wider dead band wins"


def test_choose_returns_nothing_when_every_setting_is_disqualified():
    """Better to report that nothing qualified than to freeze a bad threshold."""
    summary = synthetic_summary(
        {(0.05, 0.02, 1): {"0.0": 100.0, "0.05": 70.0, "0.2": 60.0}},
        baselines=BASELINES,
        switches={(0.05, 0.02, 1): {"0.0": 3.0, "0.05": 3.0, "0.2": 3.0}})
    assert cal.choose_setting(cal.score_settings(summary)) is None


# ---------------------------------------------------------------------------
# The frozen values match the recorded evidence (T7.2)
# ---------------------------------------------------------------------------


def test_config_thresholds_are_the_ones_the_sweep_chose():
    """D9 is frozen: config.py must hold exactly the setting the recorded
    calibration selected, and the run CSV must still be in the repository to
    justify it. Changing either without re-running the sweep fails here."""
    assert config.SWITCH_HIGH == cal.CHOSEN.switch_high
    assert config.SWITCH_LOW == cal.CHOSEN.switch_low
    assert config.HYSTERESIS_COUNT == cal.CHOSEN.hysteresis_count
    assert config.EVALUATION_INTERVAL_SEGMENTS == cal.EVALUATION_INTERVAL_SEGMENTS
    assert config.MIN_MODE_RESIDENCE_S == cal.MIN_MODE_RESIDENCE_S


def test_the_calibration_evidence_is_committed():
    assert cal.PER_RUN_CSV.exists(), "the sweep's per-run record must be kept"
    assert cal.SUMMARY_CSV.exists(), "the aggregate the freeze cites must be kept"
    rows = cal.load_rows(cal.PER_RUN_CSV)
    assert len(rows) > 100, "a freeze needs more evidence than a handful of runs"
    assert {r["stage"] for r in rows} >= {"entry", "exit", "confirm"}


def test_the_chosen_setting_is_eligible_under_the_stated_rule():
    """Re-applies the rule to the recorded data, so the freeze cannot drift from
    the evidence that justified it."""
    summary = cal.aggregate(cal.load_rows(cal.PER_RUN_CSV))
    scored = cal.score_settings(summary, stage="entry")
    chosen = next(item for item in scored
                  if item["switch_high"] == cal.CHOSEN.switch_high
                  and item["hysteresis_count"] == cal.CHOSEN.hysteresis_count)
    assert chosen["eligible"], chosen["disqualified_for"]
