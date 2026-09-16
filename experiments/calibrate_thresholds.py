"""Threshold calibration sweep for D9 (T7.1, T7.2, T7.3).

D9 — ``SWITCH_HIGH``, ``SWITCH_LOW``, ``HYSTERESIS_COUNT`` — is the second
experiment-invalidating decision (specs.md §16.10, §16.11). It is deliberately
the *last* thing frozen, because the only honest way to choose a threshold is to
measure what the estimator actually reads under known loss, and that could not be
done until the controller existed (T5.3) and the logs were sufficient to explain
a run (T6.2).

This script runs the sweep, records one row per run, aggregates per setting, and
applies a selection rule that is written down **before** the data is collected.

THE SELECTION RULE, STATED IN ADVANCE
=====================================
Choosing thresholds after seeing the winner is how a sweep becomes a
justification exercise. The rule below is therefore fixed here, in the code, and
``choose_setting`` implements exactly it:

0. **Which stage decides what.** The entry sweep fixes ``SWITCH_HIGH``; the exit
   sweep fixes ``SWITCH_LOW`` and ``HYSTERESIS_COUNT``. A static condition cannot
   exercise the anti-oscillation machinery at all — there is nothing to oscillate
   between when the right answer never changes — so choosing the hysteresis count
   from static runs would be choosing it against no evidence. It is decided where
   it does work: against a condition that changes.
1. **Hard constraints.** A setting is disqualified if any run under it failed its
   integrity check, or if it switched at all at 0% or 1% loss — where GBN is the
   right answer and switching only costs a drain — or if it averaged more than
   two switches per transfer under a *static* condition, which is oscillation by
   definition since a static condition justifies at most one entry switch.
2. **Primary score: regret against the better fixed baseline.** For each loss
   level, compare the hybrid's mean goodput against the better of pure GBN and
   pure SR *at that same loss level and the same seeds*. A setting's score is the
   mean of those ratios across the grid. Higher is better. Regret, rather than
   raw goodput, because raw goodput is dominated by the loss level rather than by
   the setting.
3. **Tie-break** (within 2% of the best score): fewer mean switches, then the
   wider dead band — both favour stability, which is what hysteresis is for.
4. **The hysteresis count is chosen against the oscillating condition**, by the
   criterion "closest to the number of crossings the condition actually
   contains". Fewer switches is only a virtue when the missing ones were
   spurious: a count that never switches back is not stable, it has stopped
   adapting, and a count that switches more often than the condition changes is
   oscillating. ``choose_hysteresis`` implements exactly that.

   The ``flap`` stage was added *after* the exit sweep, because that sweep made
   the lowest count look best while HY-09 requires rapid repeated transitions to
   be prevented — and a falling-loss schedule crosses the threshold once, so it
   cannot tell an adaptive controller from a twitchy one. The stage was added to
   measure the case rather than argue it, and it reversed the reading: the count
   that looked best before it ran turned out to oscillate.

WHAT IS SWEPT, AND WHAT IS HELD FIXED
======================================
Swept: ``SWITCH_HIGH``, ``SWITCH_LOW``, ``HYSTERESIS_COUNT``, per the task.

Held fixed across every run, and therefore frozen at these values by the same
evidence: ``EVALUATION_INTERVAL_SEGMENTS`` (20) and ``MIN_MODE_RESIDENCE_S``
(1.0). Every comparison below is made at those two values, so a later change to
either invalidates the calibration exactly as a change to a threshold would.
That is stated rather than implied, because a value that was never varied is
easy to mistake for one that was.

Also fixed, for fairness within every cell (RP-04): the same source file, the
same window, the same RTO, and the same per-trial seed. Only the setting varies.

TWO STAGES, BECAUSE THE TWO THRESHOLDS DO DIFFERENT JOBS
=========================================================
``SWITCH_HIGH`` governs entering SR and is exercised by any lossy transfer.
``SWITCH_LOW`` governs the return to GBN, which **only happens when loss falls**
— a static-loss run never tests it at all. Sweeping both against static loss
would calibrate one of them against no evidence, so:

* ``--stage entry`` sweeps ``SWITCH_HIGH`` × ``HYSTERESIS_COUNT`` over the static
  loss grid of specs.md §17.1, with the fixed GBN and SR baselines for reference;
* ``--stage exit`` sweeps ``SWITCH_LOW`` × ``HYSTERESIS_COUNT`` against a
  ``loss_schedule`` that starts high and drops, which is the only condition in
  which returning to GBN is the right answer;
* ``--stage confirm`` re-runs the chosen setting against all three fixed systems
  across the grid, which is the evidence for T7.2 and the source of T7.3's
  "where is the hybrid *worse*" answer.

A KNOWN LIMIT OF THIS CALIBRATION
==================================
The sweep runs over loopback with no emulated RTT, because the grid is several
hundred transfers and a 100 ms RTT would multiply every one of them. The
thresholds are read against the D8 estimator, which is a *ratio of segment
outcomes* and carries no unit of time, so it is expected to be close to
RTT-invariant — but "expected" is not "measured", so ``--stage rtt-check``
re-runs the chosen setting at a non-zero RTT and the report states what it found.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import statistics
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                       # noqa: E402
import metrics as metrics_mod                       # noqa: E402
from network.simulator import Impairment            # noqa: E402
from protocol import hybrid                         # noqa: E402
from receiver import Receiver, ReceiverError        # noqa: E402
from sender import Sender, TransferError            # noqa: E402

#: The loss grid of specs.md §17.1 / §19 (E1-E6).
LOSS_GRID = (0.0, 0.01, 0.02, 0.05, 0.10, 0.20)

#: Entry thresholds to try. They reach well above the nominal loss rates on
#: purpose: D8 over-reads loss while GBN is active, because one drop resends the
#: whole outstanding range, so a threshold that "should" be 0.05 by loss rate may
#: need to be far higher on the estimator's own scale. That bias is documented at
#: the freeze (protocol/hybrid.py, specs.md §16.9) and is precisely what this
#: sweep has to measure rather than assume.
SWITCH_HIGH_GRID = (0.05, 0.10, 0.20, 0.35, 0.50)

#: Exit thresholds to try, against a falling-loss schedule.
SWITCH_LOW_GRID = (0.01, 0.02, 0.05, 0.10)

HYSTERESIS_GRID = (1, 2, 3)

#: Loss levels at which switching is disqualifying: GBN is the right answer and a
#: switch only pays the drain.
QUIET_LOSS_LEVELS = (0.0, 0.01)

#: Mean switches per transfer above which a *static* condition counts as
#: oscillating. A static condition justifies one entry switch, not a sequence.
MAX_STATIC_SWITCHES = 2.0

#: The falling-loss schedule for the exit sweep: high, then clean from 3 s.
EXIT_SCHEDULE = ((3.0, 0.0),)
EXIT_INITIAL_LOSS = 0.15

#: A condition built to provoke oscillation: loss alternating across the
#: crossover the dead band straddles, faster than a mode can settle. This is the
#: one condition under which a low ``HYSTERESIS_COUNT`` would be the wrong
#: choice, so it is measured rather than argued about (HY-09, G-06).
#: The period is 2.5 s — comfortably longer than MIN_MODE_RESIDENCE_S, since a
#: condition that alternates faster than the minimum residence cannot provoke
#: oscillation whatever the hysteresis count, and testing against one would
#: prove nothing. The transfer must also outlast several periods, which is why
#: the flap stage runs a much larger file than the rest of the sweep.
#: Both phases are lossy, on opposite sides of the crossover, rather than
#: alternating with a clean phase. A clean phase on loopback runs roughly fifty
#: times faster than a lossy one, so a file large enough to span several clean
#: periods finishes during the first one — the condition alternates, but the
#: transfer never sees it. Alternating 1% against 6% keeps throughput within one
#: order of magnitude in both phases, so the schedule actually plays out.
FLAP_SCHEDULE = ((2.5, 0.01), (5.0, 0.06), (7.5, 0.01), (10.0, 0.06), (12.5, 0.01))
FLAP_INITIAL_LOSS = 0.06

FIXED_SYSTEMS = ("gbn", "sr")
DEFAULT_SIZE_SEGMENTS = 256
DEFAULT_TRIALS = 3
DEFAULT_BASE_SEED = 20260916

#: Held fixed across the whole sweep; see the module docstring.
EVALUATION_INTERVAL_SEGMENTS = 20
MIN_MODE_RESIDENCE_S = 1.0

#: The setting frozen as D9 by this sweep (T7.2). SWITCH_HIGH comes from the
#: entry stage's score, SWITCH_LOW from the exit stage, and HYSTERESIS_COUNT from
#: the flapping condition — see ``experiments/results/calibration.md`` for the
#: evidence and for what each one cost. config.py must match this exactly; a test
#: asserts it does, so the frozen value cannot drift from the calibration.
CHOSEN = None       # assigned below, once Setting is defined

RESULTS_DIR = Path(__file__).resolve().parent / "results"
PER_RUN_CSV = RESULTS_DIR / "calibration_runs.csv"
SUMMARY_CSV = RESULTS_DIR / "calibration_summary.csv"

ROW_FIELDS = [
    "stage", "run_id", "system", "loss_rate", "loss_schedule", "rtt_ms",
    "switch_high", "switch_low", "hysteresis_count",
    "evaluation_interval_segments", "min_mode_residence_s",
    "trial", "seed", "file_bytes", "window_size", "rto_s",
    "completion_time_s", "goodput_bytes_per_s", "retransmission_count",
    "retransmission_overhead", "switch_count", "handshake_count",
    "abandoned_switches", "gbn_residence_s", "sr_residence_s",
    "final_mode", "integrity_success", "error",
]


@dataclass(frozen=True)
class Setting:
    """One point in the D9 search space."""

    switch_high: float
    switch_low: float
    hysteresis_count: int

    def as_settings(self) -> hybrid.HybridSettings:
        return hybrid.HybridSettings(
            loss_window_size=config.LOSS_WINDOW_SIZE,
            switch_high=self.switch_high,
            switch_low=self.switch_low,
            hysteresis_count=self.hysteresis_count,
            evaluation_interval_segments=EVALUATION_INTERVAL_SEGMENTS,
            min_mode_residence_s=MIN_MODE_RESIDENCE_S,
        )

    def label(self) -> str:
        return f"h{self.switch_high:g}_l{self.switch_low:g}_n{self.hysteresis_count}"


#: Frozen by T7.2 from the recorded sweep; see the module docstring for the rule.
CHOSEN = Setting(switch_high=0.10, switch_low=0.02, hysteresis_count=3)


def entry_settings(switch_low: float = 0.02) -> list[Setting]:
    """Entry sweep: SWITCH_HIGH × HYSTERESIS_COUNT at one fixed exit threshold."""
    return [Setting(high, min(switch_low, high), count)
            for high in SWITCH_HIGH_GRID
            for count in HYSTERESIS_GRID]


def exit_settings(switch_high: float) -> list[Setting]:
    """Exit sweep: SWITCH_LOW × HYSTERESIS_COUNT at the chosen entry threshold."""
    return [Setting(switch_high, low, count)
            for low in SWITCH_LOW_GRID if low <= switch_high
            for count in HYSTERESIS_GRID]


def derive_seed(base_seed: int, condition: str, trial: int) -> int:
    """A per-trial seed derived from one recorded base seed (RP-03).

    Derived rather than sequential so that two conditions never share a stream by
    accident, and stable across runs of this script so the whole sweep replays
    from the base seed alone.
    """
    digest = hashlib.sha256(f"{base_seed}/{condition}/{trial}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def source_file(directory: Path, size_bytes: int) -> Path:
    """One deterministic source file, shared by every run (RP-04).

    Pseudo-random rather than compressible: a truncated transfer has to be
    obvious in the hash, not hidden by a file of zeros.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"calibration_{size_bytes}.bin"
    if path.exists() and path.stat().st_size == size_bytes:
        return path
    blob = hashlib.sha256(b"calibration").digest()
    while len(blob) < size_bytes:
        blob += hashlib.sha256(blob).digest()
    path.write_bytes(blob[:size_bytes])
    return path


# ---------------------------------------------------------------------------
# Running one transfer
# ---------------------------------------------------------------------------


def run_once(*, run_id: str, system: str, source: Path, log_dir: Path,
             loss_rate: float, seed: int, setting: Setting | None,
             loss_schedule: tuple = (), rtt_ms: float = 0.0,
             idle_timeout: float = 30.0) -> dict:
    """One transfer, in process, returning the row recorded for it.

    In process rather than by subprocess: this sweep is several hundred
    transfers and process startup would dominate it. The general experiment
    runner (T8.2) orchestrates subprocesses, because a published experiment
    should exercise the CLI the way a reader would.

    Every metric is re-derived from the event log rather than read out of the
    sender's memory — the same derivation an outside reader would perform
    (T6.2, CC-06), so the calibration evidence rests on the record, not on the
    process that produced it.
    """
    output = log_dir / f"{run_id}.out"
    impairment = Impairment(loss_rate=loss_rate, seed=seed,
                            delay_ms=rtt_ms / 2.0, loss_schedule=loss_schedule)
    receiver = Receiver(output=output, host="127.0.0.1", port=0, run_id=run_id,
                        log_dir=log_dir, idle_timeout=idle_timeout, linger=0.2,
                        impairment=Impairment(delay_ms=rtt_ms / 2.0, seed=seed))
    port = receiver.bind()

    receiver_failures: list[Exception] = []

    def receive() -> None:
        try:
            receiver.run()
        except ReceiverError as exc:
            receiver_failures.append(exc)

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()

    sender = Sender(path=source, host="127.0.0.1", port=port, mode=system,
                    window=config.WINDOW_SIZE, run_id=run_id, log_dir=log_dir,
                    rto=config.baseline_rto(rtt_ms), impairment=impairment,
                    hybrid_settings=setting.as_settings() if setting else None)

    error = None
    try:
        sender.run()
    except TransferError as exc:
        error = str(exc)
    finally:
        thread.join(timeout=120)

    if thread.is_alive():
        error = (error or "") + ";receiver did not finish"
    if receiver_failures and not error:
        error = str(receiver_failures[0])

    derived = metrics_mod.derive_from_run(log_dir / run_id)
    controller = sender.controller
    return {
        "run_id": run_id,
        "system": system,
        "loss_rate": loss_rate,
        "loss_schedule": ";".join(f"{t:g}:{r:g}" for t, r in loss_schedule),
        "rtt_ms": rtt_ms,
        "switch_high": setting.switch_high if setting else "",
        "switch_low": setting.switch_low if setting else "",
        "hysteresis_count": setting.hysteresis_count if setting else "",
        "evaluation_interval_segments": EVALUATION_INTERVAL_SEGMENTS if setting else "",
        "min_mode_residence_s": MIN_MODE_RESIDENCE_S if setting else "",
        "seed": seed,
        "file_bytes": source.stat().st_size,
        "window_size": config.WINDOW_SIZE,
        "rto_s": config.baseline_rto(rtt_ms),
        "completion_time_s": derived["completion_time_s"],
        "goodput_bytes_per_s": derived["goodput_bytes_per_s"],
        "retransmission_count": derived["retransmission_count"],
        "retransmission_overhead": derived["retransmission_overhead"],
        "switch_count": derived["switch_count"],
        "handshake_count": derived["handshake_count"],
        "abandoned_switches": (controller.stats.abandoned_switches
                               if controller else 0),
        "gbn_residence_s": derived["gbn_residence_s"],
        "sr_residence_s": derived["sr_residence_s"],
        "final_mode": sender.active_mode,
        "integrity_success": bool(derived["integrity_success"]),
        "error": error or "",
    }


class RowWriter:
    """Appends rows to the per-run CSV as they complete.

    Appending as it goes, rather than at the end, so an interrupted sweep still
    leaves behind the runs it did finish — several hundred transfers is long
    enough that losing them all to one failure would matter.
    """

    def __init__(self, path: Path, append: bool):
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists() and path.stat().st_size > 0
        self._file = open(path, "a" if append and exists else "w",
                          newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=ROW_FIELDS,
                                      extrasaction="ignore")
        if not (append and exists):
            self._writer.writeheader()

    def write(self, row: dict) -> None:
        self._writer.writerow(row)
        self._file.flush()

    def close(self) -> None:
        self._file.close()


# ---------------------------------------------------------------------------
# The stages
# ---------------------------------------------------------------------------


def stage_entry(writer: RowWriter, *, source: Path, log_dir: Path, trials: int,
                base_seed: int, settings: list[Setting],
                loss_grid=LOSS_GRID) -> None:
    """SWITCH_HIGH × HYSTERESIS_COUNT over the static loss grid, plus baselines."""
    for loss in loss_grid:
        for trial in range(trials):
            seed = derive_seed(base_seed, f"static/{loss:g}", trial)
            for system in FIXED_SYSTEMS:
                run_id = f"entry_{system}_L{loss:g}_t{trial}"
                row = run_once(run_id=run_id, system=system, source=source,
                               log_dir=log_dir, loss_rate=loss, seed=seed,
                               setting=None)
                writer.write(dict(row, stage="entry", trial=trial))
                _progress(row)
            for setting in settings:
                run_id = f"entry_hybrid_{setting.label()}_L{loss:g}_t{trial}"
                row = run_once(run_id=run_id, system="hybrid", source=source,
                               log_dir=log_dir, loss_rate=loss, seed=seed,
                               setting=setting)
                writer.write(dict(row, stage="entry", trial=trial))
                _progress(row)


def stage_exit(writer: RowWriter, *, source: Path, log_dir: Path, trials: int,
               base_seed: int, settings: list[Setting]) -> None:
    """SWITCH_LOW × HYSTERESIS_COUNT against a falling-loss schedule."""
    for trial in range(trials):
        seed = derive_seed(base_seed, "falling", trial)
        for system in FIXED_SYSTEMS:
            run_id = f"exit_{system}_t{trial}"
            row = run_once(run_id=run_id, system=system, source=source,
                           log_dir=log_dir, loss_rate=EXIT_INITIAL_LOSS,
                           seed=seed, setting=None, loss_schedule=EXIT_SCHEDULE)
            writer.write(dict(row, stage="exit", trial=trial))
            _progress(row)
        for setting in settings:
            run_id = f"exit_hybrid_{setting.label()}_t{trial}"
            row = run_once(run_id=run_id, system="hybrid", source=source,
                           log_dir=log_dir, loss_rate=EXIT_INITIAL_LOSS,
                           seed=seed, setting=setting, loss_schedule=EXIT_SCHEDULE)
            writer.write(dict(row, stage="exit", trial=trial))
            _progress(row)


def stage_flap(writer: RowWriter, *, source: Path, log_dir: Path, trials: int,
               base_seed: int, settings: list[Setting]) -> None:
    """HYSTERESIS_COUNT against a condition designed to provoke oscillation.

    The exit sweep drops loss to zero, which every candidate exit threshold
    clears at the same moment and which no controller should hesitate over. This
    one alternates across the crossover instead, which is the only place a
    confirmation counter earns its cost — and therefore the only evidence on
    which its value can honestly be chosen (HY-09).
    """
    for trial in range(trials):
        seed = derive_seed(base_seed, "flapping", trial)
        for system in FIXED_SYSTEMS:
            run_id = f"flap_{system}_t{trial}"
            row = run_once(run_id=run_id, system=system, source=source,
                           log_dir=log_dir, loss_rate=FLAP_INITIAL_LOSS, seed=seed,
                           setting=None, loss_schedule=FLAP_SCHEDULE)
            writer.write(dict(row, stage="flap", trial=trial))
            _progress(row)
        for setting in settings:
            run_id = f"flap_hybrid_{setting.label()}_t{trial}"
            row = run_once(run_id=run_id, system="hybrid", source=source,
                           log_dir=log_dir, loss_rate=FLAP_INITIAL_LOSS, seed=seed,
                           setting=setting, loss_schedule=FLAP_SCHEDULE)
            writer.write(dict(row, stage="flap", trial=trial))
            _progress(row)


def stage_confirm(writer: RowWriter, *, source: Path, log_dir: Path, trials: int,
                  base_seed: int, setting: Setting, loss_grid=LOSS_GRID) -> None:
    """The chosen setting against every fixed system, across the grid (T7.2, T7.3)."""
    for loss in loss_grid:
        for trial in range(trials):
            seed = derive_seed(base_seed, f"static/{loss:g}", trial)
            for system in ("gbn", "sr", "fixed-hybrid", "hybrid"):
                run_id = f"confirm_{system}_L{loss:g}_t{trial}"
                row = run_once(
                    run_id=run_id, system=system, source=source, log_dir=log_dir,
                    loss_rate=loss, seed=seed,
                    setting=setting if system in hybrid.HYBRID_MODES else None)
                writer.write(dict(row, stage="confirm", trial=trial))
                _progress(row)


def stage_rtt_check(writer: RowWriter, *, source: Path, log_dir: Path, trials: int,
                    base_seed: int, setting: Setting, rtt_ms: float,
                    loss_rate: float = 0.10) -> None:
    """Does the chosen threshold still behave at a non-zero RTT?

    The sweep runs at loopback speed; this checks the assumption that the D8
    estimator, being a ratio of outcomes rather than a time, reads much the same
    when a round trip takes milliseconds instead of microseconds.
    """
    for trial in range(trials):
        seed = derive_seed(base_seed, f"rtt/{rtt_ms:g}", trial)
        for system in ("gbn", "sr", "hybrid"):
            run_id = f"rtt{rtt_ms:g}_{system}_t{trial}"
            row = run_once(run_id=run_id, system=system, source=source,
                           log_dir=log_dir, loss_rate=loss_rate, seed=seed,
                           setting=setting if system == "hybrid" else None,
                           rtt_ms=rtt_ms, idle_timeout=60.0)
            writer.write(dict(row, stage="rtt-check", trial=trial))
            _progress(row)


def _progress(row: dict) -> None:
    print(f"  {row['run_id']:<44} {row['completion_time_s'] or 0:6.2f}s "
          f"retx={row['retransmission_count']:<5} sw={row['switch_count']} "
          f"{'ok' if row['integrity_success'] else 'FAILED'}", flush=True)


# ---------------------------------------------------------------------------
# Aggregation and the selection rule
# ---------------------------------------------------------------------------


def _float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_rows(path: Path = PER_RUN_CSV) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def aggregate(rows: list[dict]) -> list[dict]:
    """Mean and spread per (stage, system, setting, loss), across trials.

    Aggregating over trials rather than reporting a single run, because one
    seed's luck is not a result (design.md §10).
    """
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["stage"], row["system"], row["switch_high"], row["switch_low"],
               row["hysteresis_count"], row["loss_rate"], row["rtt_ms"])
        groups.setdefault(key, []).append(row)

    summary = []
    for key, members in sorted(groups.items()):
        stage, system, high, low, count, loss, rtt = key
        goodputs = [_float(row["goodput_bytes_per_s"]) for row in members
                    if row["integrity_success"] == "True"]
        summary.append({
            "stage": stage,
            "system": system,
            "switch_high": high,
            "switch_low": low,
            "hysteresis_count": count,
            "loss_rate": loss,
            "rtt_ms": rtt,
            "trials": len(members),
            "integrity_failures": sum(1 for row in members
                                      if row["integrity_success"] != "True"),
            "goodput_mean": statistics.fmean(goodputs) if goodputs else 0.0,
            "goodput_stdev": (statistics.stdev(goodputs) if len(goodputs) > 1 else 0.0),
            "completion_mean_s": statistics.fmean(
                [_float(row["completion_time_s"]) for row in members]),
            "retransmissions_mean": statistics.fmean(
                [_float(row["retransmission_count"]) for row in members]),
            "overhead_mean": statistics.fmean(
                [_float(row["retransmission_overhead"]) for row in members]),
            "switches_mean": statistics.fmean(
                [_float(row["switch_count"]) for row in members]),
            "gbn_residence_mean_s": statistics.fmean(
                [_float(row["gbn_residence_s"]) for row in members]),
            "sr_residence_mean_s": statistics.fmean(
                [_float(row["sr_residence_s"]) for row in members]),
        })
    return summary


def baseline_goodput(summary: list[dict], stage: str) -> dict[str, dict[str, float]]:
    """Per loss level, the mean goodput of each fixed system in that stage."""
    baselines: dict[str, dict[str, float]] = {}
    for row in summary:
        if row["stage"] == stage and row["system"] in FIXED_SYSTEMS:
            baselines.setdefault(row["loss_rate"], {})[row["system"]] = row["goodput_mean"]
    return baselines


def score_settings(summary: list[dict], stage: str = "entry") -> list[dict]:
    """Score every swept setting by the rule stated in the module docstring.

    ``score`` is the mean, across loss levels, of the hybrid's goodput divided by
    the better fixed baseline at that level. 1.0 means "as good as whichever
    fixed strategy was right for this condition, everywhere".
    """
    baselines = baseline_goodput(summary, stage)
    scored: dict[tuple, dict] = {}

    for row in summary:
        if row["stage"] != stage or row["system"] != "hybrid":
            continue
        key = (row["switch_high"], row["switch_low"], row["hysteresis_count"])
        entry = scored.setdefault(key, {
            "switch_high": float(row["switch_high"]),
            "switch_low": float(row["switch_low"]),
            "hysteresis_count": int(row["hysteresis_count"]),
            "ratios": [], "switches": [], "integrity_failures": 0,
            "switched_when_quiet": False, "oscillated": False,
            "per_loss": {},
        })
        best_fixed = max(baselines.get(row["loss_rate"], {}).values(), default=0.0)
        ratio = (row["goodput_mean"] / best_fixed) if best_fixed else 0.0
        entry["ratios"].append(ratio)
        entry["switches"].append(row["switches_mean"])
        entry["integrity_failures"] += row["integrity_failures"]
        entry["per_loss"][row["loss_rate"]] = {
            "ratio": ratio,
            "goodput_mean": row["goodput_mean"],
            "switches_mean": row["switches_mean"],
            "best_fixed_goodput": best_fixed,
        }
        if (_float(row["loss_rate"]) in QUIET_LOSS_LEVELS
                and row["switches_mean"] > 0):
            entry["switched_when_quiet"] = True
        if row["switches_mean"] > MAX_STATIC_SWITCHES:
            entry["oscillated"] = True

    results = []
    for key, entry in scored.items():
        disqualified = []
        if entry["integrity_failures"]:
            disqualified.append(f"{entry['integrity_failures']} integrity failure(s)")
        if entry["switched_when_quiet"]:
            disqualified.append("switched at 0-1% loss")
        if entry["oscillated"]:
            disqualified.append(f"more than {MAX_STATIC_SWITCHES:g} switches under static loss")
        results.append({
            **{k: entry[k] for k in ("switch_high", "switch_low", "hysteresis_count",
                                     "per_loss", "integrity_failures")},
            "score": statistics.fmean(entry["ratios"]) if entry["ratios"] else 0.0,
            "switches_mean": statistics.fmean(entry["switches"]) if entry["switches"] else 0.0,
            "dead_band": entry["switch_high"] - entry["switch_low"],
            "eligible": not disqualified,
            "disqualified_for": "; ".join(disqualified),
        })
    results.sort(key=lambda item: (-item["score"], item["switches_mean"]))
    return results


def flap_crossings(schedule=FLAP_SCHEDULE) -> int:
    """How many mode transitions the flapping condition actually justifies.

    One per step of the schedule that moves the loss across the crossover, plus
    the initial entry into SR, since the run starts lossy in GBN. A controller
    doing its job lands near this number: well above it is oscillation, well
    below it is a controller that has stopped tracking the condition.
    """
    return len(schedule) + 1


def choose_hysteresis(summary: list[dict], crossings: int | None = None) -> int | None:
    """The count whose switching most closely tracks the oscillating condition.

    Deliberately *not* "fewest switches": under a condition that changes, the
    count with the fewest switches is the one that stopped following it. The
    criterion is proximity to the number of crossings, which is robust to the
    exact bookkeeping of whether the final crossing had time to take effect.
    """
    justified = crossings if crossings is not None else flap_crossings()
    by_count: dict[int, list[float]] = {}
    for row in summary:
        if row["stage"] == "flap" and row["system"] == "hybrid":
            by_count.setdefault(int(row["hysteresis_count"]), []).append(row["switches_mean"])
    if not by_count:
        return None
    means = {count: statistics.fmean(values) for count, values in by_count.items()}
    return min(means, key=lambda count: (abs(means[count] - justified), count))


def choose_setting(scored: list[dict], tolerance: float = 0.02) -> dict | None:
    """Apply the stated rule: best score, then fewer switches, then wider band."""
    eligible = [item for item in scored if item["eligible"]]
    if not eligible:
        return None
    best = max(item["score"] for item in eligible)
    contenders = [item for item in eligible if item["score"] >= best - tolerance]
    contenders.sort(key=lambda item: (item["switches_mean"], -item["dead_band"]))
    return contenders[0]


def write_summary_csv(summary: list[dict], path: Path = SUMMARY_CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stage", required=True,
                        choices=("entry", "exit", "flap", "confirm", "rtt-check",
                                 "report"))
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument("--base-seed", type=int, default=DEFAULT_BASE_SEED)
    parser.add_argument("--segments", type=int, default=DEFAULT_SIZE_SEGMENTS,
                        help="source file size, in segments")
    parser.add_argument("--switch-high", type=float, default=None,
                        help="fixed entry threshold for the exit/confirm stages")
    parser.add_argument("--switch-low", type=float, default=0.02,
                        help="fixed exit threshold for the entry stage")
    parser.add_argument("--hysteresis", type=int, default=None,
                        help="fixed hysteresis count for the confirm stage")
    parser.add_argument("--rtt", type=float, default=50.0,
                        help="emulated RTT in ms for the rtt-check stage")
    parser.add_argument("--loss", type=float, nargs="*", default=None,
                        help="override the loss grid")
    parser.add_argument("--append", action="store_true",
                        help="append to the existing per-run CSV instead of replacing it")
    parser.add_argument("--out", type=Path, default=PER_RUN_CSV)
    parser.add_argument("--log-dir", type=Path, default=config.LOG_DIR / "calibration")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    loss_grid = tuple(args.loss) if args.loss is not None else LOSS_GRID

    if args.stage == "report":
        rows = load_rows(args.out)
        summary = aggregate(rows)
        write_summary_csv(summary)
        scored = score_settings(summary, stage="entry")
        print(f"{len(rows)} runs, {len(summary)} conditions")
        print(f"\n{'high':>6} {'low':>6} {'n':>2} {'score':>7} {'switches':>9}  verdict")
        for item in scored:
            verdict = "eligible" if item["eligible"] else item["disqualified_for"]
            print(f"{item['switch_high']:6.2f} {item['switch_low']:6.2f} "
                  f"{item['hysteresis_count']:2d} {item['score']:7.3f} "
                  f"{item['switches_mean']:9.2f}  {verdict}")
        chosen = choose_setting(scored)
        print(f"\nchosen by the stated rule: {chosen and (chosen['switch_high'], chosen['switch_low'], chosen['hysteresis_count'])}")
        return 0

    source = source_file(args.log_dir, args.segments * config.SEGMENT_SIZE)
    writer = RowWriter(args.out, append=args.append)
    started = time.perf_counter()
    print(f"stage {args.stage}: {args.trials} trials, "
          f"{args.segments} segments, base seed {args.base_seed}", flush=True)

    try:
        if args.stage == "entry":
            stage_entry(writer, source=source, log_dir=args.log_dir,
                        trials=args.trials, base_seed=args.base_seed,
                        settings=entry_settings(args.switch_low),
                        loss_grid=loss_grid)
        elif args.stage == "exit":
            if args.switch_high is None:
                raise SystemExit("--switch-high is required for the exit stage")
            stage_exit(writer, source=source, log_dir=args.log_dir,
                       trials=args.trials, base_seed=args.base_seed,
                       settings=exit_settings(args.switch_high))
        elif args.stage == "flap":
            if args.switch_high is None:
                raise SystemExit("--switch-high is required for the flap stage")
            settings = [Setting(args.switch_high, args.switch_low, count)
                        for count in HYSTERESIS_GRID]
            stage_flap(writer, source=source, log_dir=args.log_dir,
                       trials=args.trials, base_seed=args.base_seed,
                       settings=settings)
        else:
            if args.switch_high is None or args.hysteresis is None:
                raise SystemExit("--switch-high and --hysteresis are required")
            setting = Setting(args.switch_high, args.switch_low, args.hysteresis)
            if args.stage == "confirm":
                stage_confirm(writer, source=source, log_dir=args.log_dir,
                              trials=args.trials, base_seed=args.base_seed,
                              setting=setting, loss_grid=loss_grid)
            else:
                stage_rtt_check(writer, source=source, log_dir=args.log_dir,
                                trials=args.trials, base_seed=args.base_seed,
                                setting=setting, rtt_ms=args.rtt)
    finally:
        writer.close()

    print(f"stage {args.stage} finished in {time.perf_counter() - started:.1f}s "
          f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
