"""Automated experiment execution (T8.2, FR-13, specs.md §19).

Runs the experimental matrix of specs.md §19 from a config file, one transfer at
a time, and records one row per run beside the untouched raw logs each run wrote.

WHY THIS IS A SUBPROCESS RUNNER
===============================
``calibrate_thresholds.py`` runs its transfers in process, because that sweep is
several hundred short transfers and process startup would have dominated it.
This runner deliberately does the opposite and drives ``receiver.py`` and
``sender.py`` through their command lines:

* the published experiments should exercise the program a reader would run, not
  a library call that happens to share its internals;
* sender and receiver are separate processes in every real use, and running them
  in one interpreter hides any way they could interfere — a shared module
  global, a socket left open, an exception in one thread stalling the other;
* an abort timeout can only be *enforced* against a process. A hung in-process
  transfer cannot be interrupted without killing the runner with it, and
  specs.md §13 requires a run that will not finish to be abandoned and reported
  rather than waited on (RP-06).

The cost is a few hundred milliseconds of interpreter startup per run, which is
noise beside transfers measured in tens of seconds at the RTTs of §19.

WHAT IS HELD IDENTICAL WITHIN A CELL (RP-04, specs.md §18)
==========================================================
Within one cell of the matrix — one condition — GBN, SR and Hybrid meet the same
source file, the same segment size, the same window, the same derived RTO and
**the same impairment seed**, so the comparison isolates the retransmission
strategy and nothing else. The seed is derived from the condition and the trial
index only; the system name is deliberately *not* an input to it. A test asserts
that, because it is the single property that makes the whole matrix a controlled
comparison rather than three unrelated sets of runs.

THE DECISIONS THIS PHASE FREEZES (T8.1)
=======================================
``D12`` repetition count, ``D13`` seed policy and ``D14`` file size live in
``config.py`` with their rationale in specs.md §16.13-§16.15. They are experiment
*scale* decisions: nothing already measured changes when they are settled, which
is why they could wait until there was a harness to measure against.

D13 is implemented by ``derive_seed`` below: one base seed per config file,
recorded in the config and in every ``summary.json``, with each trial's seed
derived from it by SHA-256 over ``base/experiment/condition/trial``. Derived
rather than sequential, so two conditions cannot accidentally share an
impairment stream; deterministic, so the whole matrix replays from the base seed
alone (RP-03).

RAW LOGS ARE EVIDENCE, AND ARE NEVER EDITED (RP-07, T8.7)
=========================================================
Each run's ``events.csv`` and ``summary.json`` are written by the endpoints and
never touched again. This runner records the SHA-256 of both event logs in its
index at the moment the run finished, and ``--verify`` recomputes them: an
edited, truncated or regenerated log is then a reported mismatch rather than a
silent one. Every metric in the index is re-derived from ``events.csv`` by
``metrics.py`` — the derivation an outside reader would perform (CC-06) — rather
than read out of the sender's own memory.

USAGE
=====
    python experiments/run_experiment.py --config experiments/configs/E4.json
    python experiments/run_experiment.py --all --resume
    python experiments/run_experiment.py --all --dry-run       # print the plan
    python experiments/run_experiment.py --verify              # T8.7 audit
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                       # noqa: E402
import metrics as metrics_mod                       # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = config.EXPERIMENT_CONFIG_DIR
RESULTS_DIR = Path(__file__).resolve().parent / "results"
RUNS_CSV = RESULTS_DIR / "experiment_runs.csv"

#: The systems of specs.md §18. ``fixed-hybrid`` is the switching-overhead
#: control (T5.6); ``saw`` is the Phase 2 placeholder and not a system under
#: evaluation, so it is not accepted here.
SYSTEMS = ("gbn", "sr", "hybrid", "fixed-hybrid")

#: Recognised config keys. An unknown one raises rather than being ignored, for
#: the reason ``config.snapshot`` gives: a typo that invents a parameter leaves
#: the recorded configuration quietly describing a run nobody performed.
CONFIG_KEYS = frozenset({
    "experiment", "description", "systems", "trials", "file_size_bytes",
    "base_seed", "abort_timeout_s", "window_size", "port", "conditions",
})
CONDITION_KEYS = frozenset({
    "name", "description", "loss_rate", "ack_loss_rate", "rtt_ms", "jitter_ms",
    "loss_schedule",
})

ROW_FIELDS = [
    "experiment", "condition", "system", "trial", "run_id",
    "loss_rate", "ack_loss_rate", "rtt_ms", "jitter_ms", "loss_schedule",
    "seed", "file_bytes", "window_size", "rto_s",
    "completion_time_s", "goodput_bytes_per_s", "delivered_bytes",
    "retransmission_count", "retransmission_overhead",
    "switch_count", "handshake_count", "gbn_residence_s", "sr_residence_s",
    "rtt_mean_ms", "final_mode", "integrity_success", "status", "error",
    "wall_clock_s", "started_utc", "log_dir",
    "sender_events_sha256", "receiver_events_sha256",
]

#: The line receiver.py prints once its socket is bound. The runner waits for
#: it rather than sleeping, so the sender never starts against a socket that
#: does not exist yet.
LISTENING_PREFIX = "listening on "

#: Run statuses. ``ok`` means the transfer completed *and* the hashes matched;
#: an integrity failure is ``integrity`` and is never recorded as ``ok``
#: (CC-01). ``aborted`` is a run that hit the abort timeout (specs.md §13).
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_INTEGRITY = "integrity"
STATUS_ABORTED = "aborted"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Condition:
    """One cell's impairment condition (specs.md §17)."""

    name: str
    loss_rate: float = 0.0
    ack_loss_rate: float = 0.0
    rtt_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_schedule: tuple[tuple[float, float], ...] = ()
    description: str = ""

    @property
    def rto_s(self) -> float:
        """D7: derived from this condition's RTT, then fixed for the whole run."""
        return config.baseline_rto(self.rtt_ms)

    def schedule_text(self) -> str:
        """``t:rate,t:rate`` — the form ``sender.py --loss-schedule`` parses."""
        return ",".join(f"{moment:g}:{rate:g}" for moment, rate in self.loss_schedule)


@dataclass(frozen=True)
class Experiment:
    """One config file: a matrix of conditions x systems x trials."""

    experiment: str
    conditions: tuple[Condition, ...]
    systems: tuple[str, ...]
    trials: int
    file_size_bytes: int
    base_seed: int
    abort_timeout_s: float = config.EXPERIMENT_ABORT_TIMEOUT_S
    window_size: int = config.WINDOW_SIZE
    port: int = config.PORT
    description: str = ""
    path: Path | None = None


def load_experiment(path: Path | str) -> Experiment:
    """Read and validate one config file.

    Validation is strict on purpose. A silently ignored key would mean the run
    did not use the condition the file describes, and the whole point of a
    config-driven runner is that the file *is* the record of what was run
    (RP-01, RP-02).
    """
    path = Path(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    return parse_experiment(document, path=path)


def parse_experiment(document: dict, *, path: Path | None = None) -> Experiment:
    unknown = set(document) - CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown config key(s) {sorted(unknown)} in {path}; "
                         f"known keys are {sorted(CONFIG_KEYS)}")
    for required in ("experiment", "conditions"):
        if required not in document:
            raise ValueError(f"config {path} has no {required!r}")

    systems = tuple(document.get("systems", ("gbn", "sr", "hybrid")))
    for system in systems:
        if system not in SYSTEMS:
            raise ValueError(f"{system!r} is not a system under evaluation "
                             f"(specs.md §18); expected one of {sorted(SYSTEMS)}")

    conditions = tuple(_parse_condition(entry, path)
                       for entry in document["conditions"])
    if not conditions:
        raise ValueError(f"config {path} declares no conditions")
    names = [condition.name for condition in conditions]
    if len(set(names)) != len(names):
        raise ValueError(f"config {path} repeats a condition name: {names}")

    trials = int(document.get("trials", config.TRIAL_COUNT))
    if trials < 1:
        raise ValueError(f"config {path} asks for {trials} trials")

    return Experiment(
        experiment=document["experiment"],
        description=document.get("description", ""),
        conditions=conditions,
        systems=systems,
        trials=trials,
        file_size_bytes=int(document.get("file_size_bytes",
                                         config.TRANSFER_FILE_SIZE_BYTES)),
        base_seed=int(document.get("base_seed", config.RANDOM_SEED)),
        abort_timeout_s=float(document.get("abort_timeout_s",
                                           config.EXPERIMENT_ABORT_TIMEOUT_S)),
        window_size=int(document.get("window_size", config.WINDOW_SIZE)),
        port=int(document.get("port", config.PORT)),
        path=path,
    )


def _parse_condition(entry: dict, path: Path | None) -> Condition:
    unknown = set(entry) - CONDITION_KEYS
    if unknown:
        raise ValueError(f"unknown condition key(s) {sorted(unknown)} in {path}; "
                         f"known keys are {sorted(CONDITION_KEYS)}")
    if "name" not in entry:
        raise ValueError(f"a condition in {path} has no name")
    schedule = tuple(sorted((float(moment), float(rate))
                            for moment, rate in entry.get("loss_schedule", ())))
    return Condition(
        name=entry["name"],
        description=entry.get("description", ""),
        loss_rate=float(entry.get("loss_rate", 0.0)),
        ack_loss_rate=float(entry.get("ack_loss_rate", 0.0)),
        rtt_ms=float(entry.get("rtt_ms", 0.0)),
        jitter_ms=float(entry.get("jitter_ms", 0.0)),
        loss_schedule=schedule,
    )


def load_all(directory: Path | str = CONFIG_DIR) -> list[Experiment]:
    """Every config in ``experiments/configs/``, in file-name order (E1 … E8)."""
    return [load_experiment(path) for path in sorted(Path(directory).glob("*.json"))]


# ---------------------------------------------------------------------------
# Identity: run ids and seeds (D13)
# ---------------------------------------------------------------------------


def run_id(experiment: str, system: str, condition: str, trial: int) -> str:
    """``E4_hybrid_loss05_trial03`` (design.md §10).

    The condition name is part of the id because E7 and E8 carry several
    conditions in one experiment, and two runs sharing an id would share a log
    directory — the second would overwrite the first's evidence.
    """
    return f"{experiment}_{system}_{condition}_trial{trial:02d}"


def derive_seed(base_seed: int, experiment: str, condition: str, trial: int) -> int:
    """The per-trial impairment seed (D13, RP-03).

    Derived by SHA-256 from one recorded base seed, so the matrix replays from
    that seed alone and no two conditions share a stream by accident. **The
    system is not an input**: within a cell, GBN, SR and Hybrid must meet the
    identical drop sequence or the comparison is not controlled (RP-04).
    """
    key = f"{base_seed}/{experiment}/{condition}/{trial}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(key).digest()[:4], "big")


def source_file(directory: Path, size_bytes: int) -> Path:
    """The one source file every run of this size shares (RP-04).

    Pseudo-random rather than compressible, and identical byte for byte across
    systems: a truncated or corrupted transfer has to be obvious in the hash
    rather than hidden by a file of zeros.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"source_{size_bytes}.bin"
    if path.exists() and path.stat().st_size == size_bytes:
        return path
    blob = hashlib.sha256(b"hybrid-arq experiment source").digest()
    while len(blob) < size_bytes:
        blob += hashlib.sha256(blob).digest()
    path.write_bytes(blob[:size_bytes])
    return path


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedRun:
    """One transfer: a cell of the matrix, a system, and a trial index."""

    experiment: Experiment
    condition: Condition
    system: str
    trial: int

    @property
    def run_id(self) -> str:
        return run_id(self.experiment.experiment, self.system,
                      self.condition.name, self.trial)

    @property
    def seed(self) -> int:
        return derive_seed(self.experiment.base_seed, self.experiment.experiment,
                           self.condition.name, self.trial)


def plan(experiments: list[Experiment]) -> list[PlannedRun]:
    """Expand configs into runs, trial-major within a condition.

    Ordered condition -> trial -> system rather than condition -> system ->
    trial so that an interrupted matrix still holds *complete cells*: three
    systems at one trial index are comparable, where a half-finished system
    sweep is not.
    """
    runs = []
    for experiment in experiments:
        for condition in experiment.conditions:
            for trial in range(experiment.trials):
                for system in experiment.systems:
                    runs.append(PlannedRun(experiment, condition, system, trial))
    return runs


# ---------------------------------------------------------------------------
# Running one transfer
# ---------------------------------------------------------------------------


class OrchestrationError(RuntimeError):
    """The harness could not get the two processes started or stopped."""


def _python() -> list[str]:
    # -u: unbuffered, so the receiver's "listening on ..." line arrives before
    # it blocks on recvfrom. Writing to a pipe, Python would otherwise buffer
    # the line and the runner would wait for something already written.
    return [sys.executable, "-u"]


def _await_listening(process: subprocess.Popen, timeout: float) -> int:
    """Read the receiver's bind line and return the port it actually bound.

    Waiting for the line rather than sleeping a fixed interval: a sender that
    starts before the socket exists spends its START retry budget on a receiver
    that was merely slow, and on Windows collects a ``ConnectionResetError`` for
    each attempt (network/udp.py).
    """
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        line = process.stdout.readline()
        if not line:
            # The receiver exited instead of binding — almost always a port
            # still held by an earlier run. Its own stderr says which, so it is
            # reported rather than guessed at.
            detail = (process.stderr.read() or "").strip().splitlines()
            raise OrchestrationError(
                "receiver exited before binding: "
                + (detail[-1] if detail else f"exit code {process.poll()}"))
        if line.startswith(LISTENING_PREFIX):
            # "listening on 127.0.0.1:8888, writing <path>" — the path may
            # itself contain a colon (a Windows drive letter), so the address
            # is taken from before the comma, not from the end of the line.
            address = line[len(LISTENING_PREFIX):].split(",", 1)[0]
            return int(address.rsplit(":", 1)[1].strip())
    raise OrchestrationError("receiver never reported a bound socket")


def _terminate(process: subprocess.Popen, grace: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=grace)


def _sha256(path: Path) -> str:
    """Fingerprint of a raw log, recorded so a later edit is detectable (RP-07)."""
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def final_mode(run_directory: Path) -> str:
    """The mode the sender was in when it stopped, read from its log.

    From the ``mode`` column of the last sender row that carries one, rather
    than from the sender's exit message: the mode a run *ended* in is part of
    the evidence, and evidence lives in events.csv (CC-06).
    """
    path = Path(run_directory) / "sender" / "events.csv"
    if not path.is_file():
        return ""
    modes = [row["mode"] for row in metrics_mod.read_events(path) if row["mode"]]
    return modes[-1] if modes else ""


def _portable(path: Path) -> str:
    """A run's log directory, relative to the project root where it sits under it.

    The index is committed; an absolute path in it would name one machine's home
    directory and would be wrong on every other one. ``_resolve`` reverses this,
    and accepts an absolute path too, so rows written before this existed still
    verify.
    """
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _resolve(recorded: str) -> Path:
    """The directory a recorded ``log_dir`` names, relative or absolute."""
    path = Path(recorded)
    return path if path.is_absolute() else PROJECT_ROOT / path


def execute_run(run: PlannedRun, *, log_dir: Path, source: Path,
                keep_output: bool = False) -> dict:
    """Run one transfer end to end and return the row recorded for it.

    The receiver is started first and its bound port read back, then the sender;
    both carry the same ``run_id``, so their two logs land side by side under
    ``logs/<run_id>/`` and can be interleaved on the shared START row (§21).
    """
    experiment, condition = run.experiment, run.condition
    identifier = run.run_id
    output = log_dir / "output" / f"{identifier}.bin"
    output.parent.mkdir(parents=True, exist_ok=True)

    receiver_cmd = _python() + [
        str(PROJECT_ROOT / "receiver.py"),
        "--port", str(experiment.port),
        "--output", str(output),
        "--run-id", identifier,
        "--log-dir", str(log_dir),
        "--rtt", str(condition.rtt_ms),
        "--jitter", str(condition.jitter_ms),
        "--seed", str(run.seed),
    ]
    started_utc = time.time()
    started = time.perf_counter()
    receiver = subprocess.Popen(receiver_cmd, cwd=str(PROJECT_ROOT), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    status, error = STATUS_OK, ""
    try:
        port = _await_listening(receiver, timeout=30.0)
    except BaseException as exc:
        # Including KeyboardInterrupt: a receiver left holding the port would
        # make every subsequent run of the matrix fail to bind.
        _terminate(receiver)
        if isinstance(exc, OrchestrationError):
            raise OrchestrationError(f"{identifier}: {exc}") from exc
        raise

    sender_cmd = _python() + [
        str(PROJECT_ROOT / "sender.py"),
        "--file", str(source),
        "--host", config.HOST,
        "--port", str(port),
        "--mode", run.system,
        "--window", str(experiment.window_size),
        "--run-id", identifier,
        "--log-dir", str(log_dir),
        # D7: one RTO per condition, derived from that condition's RTT and then
        # held fixed — identical for all three systems in the cell (§11, TO-01).
        "--rto", str(condition.rto_s),
        "--loss", str(condition.loss_rate),
        "--ack-loss", str(condition.ack_loss_rate),
        "--rtt", str(condition.rtt_ms),
        "--jitter", str(condition.jitter_ms),
        "--seed", str(run.seed),
    ]
    if condition.loss_schedule:
        sender_cmd += ["--loss-schedule", condition.schedule_text()]

    sender = subprocess.Popen(sender_cmd, cwd=str(PROJECT_ROOT), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _, sender_err = sender.communicate(timeout=experiment.abort_timeout_s)
    except subprocess.TimeoutExpired:
        # specs.md §13: a transfer that will not finish is abandoned and
        # reported, never waited on indefinitely. The logs it wrote up to that
        # point are kept — an aborted run still has to be explainable.
        _terminate(sender)
        sender.communicate(timeout=30)
        status, error = STATUS_ABORTED, (
            f"abort timeout after {experiment.abort_timeout_s:g}s")
    else:
        if sender.returncode != 0:
            status = STATUS_FAILED
            first_line = (sender_err or "").strip().splitlines()
            error = first_line[0] if first_line else f"sender exited {sender.returncode}"

    # The receiver exits on its own once the transfer finishes and its linger
    # expires; it is killed only if it does not.
    try:
        receiver.communicate(timeout=max(30.0, config.RECEIVER_LINGER_S * 5))
    except subprocess.TimeoutExpired:
        _terminate(receiver)
        error = error or "receiver did not exit"
        status = status if status != STATUS_OK else STATUS_FAILED
    wall_clock = time.perf_counter() - started

    if not keep_output:
        output.unlink(missing_ok=True)

    return _record(run, log_dir=log_dir, source=source, status=status,
                   error=error, wall_clock=wall_clock, started_utc=started_utc)


def _record(run: PlannedRun, *, log_dir: Path, source: Path, status: str,
            error: str, wall_clock: float, started_utc: float) -> dict:
    """Build the index row, deriving every metric from ``events.csv`` (CC-06).

    Nothing here is read out of a process's memory: the row is what an outside
    reader could reconstruct from the logs alone, which is the claim T6.2 made
    checkable and this phase leans on.
    """
    run_directory = log_dir / run.run_id
    try:
        derived = metrics_mod.derive_from_run(run_directory)
    except FileNotFoundError as exc:
        # A run that wrote no log at all is still recorded, as a failure with
        # its reason: a missing row would look like a run nobody attempted.
        derived = {}
        status, error = STATUS_FAILED, error or str(exc)
    integrity = derived.get("integrity_success")

    if status == STATUS_OK and integrity is not True:
        # A hash mismatch is always a reported failure, never presented as a
        # successful transfer (CC-01, IN-05).
        status = STATUS_INTEGRITY
        error = error or "integrity check did not pass"

    return {
        "experiment": run.experiment.experiment,
        "condition": run.condition.name,
        "system": run.system,
        "trial": run.trial,
        "run_id": run.run_id,
        "loss_rate": run.condition.loss_rate,
        "ack_loss_rate": run.condition.ack_loss_rate,
        "rtt_ms": run.condition.rtt_ms,
        "jitter_ms": run.condition.jitter_ms,
        "loss_schedule": run.condition.schedule_text(),
        "seed": run.seed,
        "file_bytes": source.stat().st_size,
        "window_size": run.experiment.window_size,
        "rto_s": run.condition.rto_s,
        "completion_time_s": derived.get("completion_time_s"),
        "goodput_bytes_per_s": derived.get("goodput_bytes_per_s"),
        "delivered_bytes": derived.get("delivered_bytes"),
        "retransmission_count": derived.get("retransmission_count"),
        "retransmission_overhead": derived.get("retransmission_overhead"),
        "switch_count": derived.get("switch_count"),
        "handshake_count": derived.get("handshake_count"),
        "gbn_residence_s": derived.get("gbn_residence_s"),
        "sr_residence_s": derived.get("sr_residence_s"),
        "rtt_mean_ms": derived.get("rtt_mean_ms"),
        "final_mode": final_mode(run_directory),
        "integrity_success": integrity,
        "status": status,
        "error": error,
        "wall_clock_s": round(wall_clock, 3),
        "started_utc": round(started_utc, 3),
        "log_dir": _portable(run_directory),
        "sender_events_sha256": _sha256(run_directory / "sender" / "events.csv"),
        "receiver_events_sha256": _sha256(run_directory / "receiver" / "events.csv"),
    }


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


class RunIndex:
    """``experiments/results/experiment_runs.csv``, appended as runs complete.

    Appended run by run rather than written at the end: the matrix is hours of
    transfers, and an interrupted execution must leave behind every run it did
    finish — which is also what makes ``--resume`` possible.
    """

    def __init__(self, path: Path = RUNS_CSV):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = None
        self._writer = None

    def existing_ids(self) -> set[str]:
        return {row["run_id"] for row in self.rows()}

    def open(self) -> None:
        exists = self.path.exists() and self.path.stat().st_size > 0
        self._file = open(self.path, "a" if exists else "w", newline="",
                          encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=ROW_FIELDS,
                                      extrasaction="ignore")
        if not exists:
            self._writer.writeheader()

    def write(self, row: dict) -> None:
        self._writer.writerow(row)
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------
# Verification (T8.7)
# ---------------------------------------------------------------------------


def verify(rows: list[dict]) -> dict:
    """Audit the recorded runs: integrity results, and logs still untouched.

    Two distinct questions, kept distinct:

    * did each transfer pass its own hash check (IN-05, CC-01)?
    * is each raw log still byte-identical to what the run wrote (RP-07)?

    The second is why the index records a fingerprint at all. Aggregation reads
    the logs and must never modify them, and "must never" is worth checking
    rather than asserting — a regenerated log otherwise looks exactly like an
    original one.
    """
    failures, missing, altered = [], [], []
    for row in rows:
        if row.get("status") != STATUS_OK:
            failures.append(row)
        for endpoint in ("sender", "receiver"):
            recorded = row.get(f"{endpoint}_events_sha256") or ""
            if not recorded:
                continue
            path = _resolve(row["log_dir"]) / endpoint / "events.csv"
            if not path.is_file():
                missing.append(f"{row['run_id']}/{endpoint}")
            elif _sha256(path) != recorded:
                altered.append(f"{row['run_id']}/{endpoint}")
    return {
        "runs": len(rows),
        "ok": len(rows) - len(failures),
        "failures": failures,
        "missing_logs": missing,
        "altered_logs": altered,
    }


def _report_verification(report: dict) -> int:
    print(f"{report['ok']}/{report['runs']} runs completed with matching hashes")
    for row in report["failures"]:
        print(f"  {row['status']:<9} {row['run_id']}: {row.get('error', '')}")
    for name in report["missing_logs"]:
        print(f"  MISSING LOG  {name}")
    for name in report["altered_logs"]:
        print(f"  ALTERED LOG  {name}  (RP-07: raw logs are never edited)")
    return 1 if report["missing_logs"] or report["altered_logs"] else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the specs.md §19 experimental matrix (T8.2).")
    parser.add_argument("--config", type=Path, action="append", default=None,
                        help="a config in experiments/configs/; repeatable")
    parser.add_argument("--all", action="store_true",
                        help="run every config in experiments/configs/")
    parser.add_argument("--log-dir", type=Path,
                        default=config.LOG_DIR / "experiments",
                        help="where each run's raw logs are written")
    parser.add_argument("--results", type=Path, default=RUNS_CSV,
                        help="the run index CSV")
    parser.add_argument("--trials", type=int, default=None,
                        help="override the configured trial count (pilot runs)")
    parser.add_argument("--resume", action="store_true",
                        help="skip runs already present in the index")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan, run nothing")
    parser.add_argument("--keep-output", action="store_true",
                        help="keep each received file instead of deleting it")
    parser.add_argument("--verify", action="store_true",
                        help="audit the recorded runs and exit (T8.7)")
    return parser


def _as_dict(experiment: Experiment) -> dict:
    return {name: getattr(experiment, name)
            for name in experiment.__dataclass_fields__}


def _selected(args) -> list[Experiment]:
    if args.all or not args.config:
        experiments = load_all()
    else:
        experiments = [load_experiment(path) for path in args.config]
    if args.trials is not None:
        experiments = [Experiment(**{**_as_dict(e), "trials": args.trials})
                       for e in experiments]
    return experiments


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    index = RunIndex(args.results)

    if args.verify:
        return _report_verification(verify(index.rows()))

    experiments = _selected(args)
    runs = plan(experiments)
    done = index.existing_ids() if args.resume else set()
    pending = [run for run in runs if run.run_id not in done]

    print(f"{len(runs)} runs planned across {len(experiments)} experiments"
          f"{f', {len(runs) - len(pending)} already recorded' if done else ''}",
          flush=True)
    if args.dry_run:
        for run in pending:
            print(f"  {run.run_id:<44} loss={run.condition.loss_rate:<5g} "
                  f"rtt={run.condition.rtt_ms:<5g} seed={run.seed}")
        return 0

    index.open()
    failures = 0
    try:
        for number, run in enumerate(pending, start=1):
            source = source_file(args.log_dir / "sources",
                                 run.experiment.file_size_bytes)
            row = execute_run(run, log_dir=args.log_dir, source=source,
                              keep_output=args.keep_output)
            index.write(row)
            failures += row["status"] != STATUS_OK
            # Flushed: the matrix takes hours, and progress redirected to a
            # file is invisible until exit if it is left block-buffered.
            print(f"[{number}/{len(pending)}] {row['run_id']:<44} "
                  f"{row['status']:<9} "
                  f"{float(row['completion_time_s'] or 0):6.2f}s "
                  f"retx={row['retransmission_count']} "
                  f"switches={row['switch_count']}", flush=True)
    finally:
        index.close()

    print(f"done: {len(pending) - failures}/{len(pending)} runs ok, "
          f"index {args.results}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
