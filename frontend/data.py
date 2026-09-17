"""Read-only access to everything the dashboard shows (T11.1, design.md §13).

Every figure the UI displays already exists on disk. This module finds it and
shapes it for JSON; it derives nothing the protocol did not record.

Three rules from the Phase 11 brief are enforced here rather than in JavaScript,
because that is where they can be tested:

1. **Recorded data only.** ``events.csv`` and ``summary.json`` are the sources.
   No second log is written anywhere — every function below opens files for
   reading and leaves them as it found them (RP-07).
2. **No protocol behaviour is computed.** Metrics come from :mod:`metrics`, the
   same derivation T6.2 asserts against the endpoints' own counters. Switch
   reasons come from the recorded ``SWITCH`` / ``MODE`` rows, never from
   re-evaluating a threshold — a threshold evaluated a second time is a second
   controller, and the two would drift (T11.5).
3. **Absence is reported as absence.** A missing run, an empty results file or a
   capture that does not exist returns an explicit empty state. A plausible
   placeholder would be indistinguishable from a measurement.

Run identity: ``EventLog`` writes ``<log_dir>/<run_id>/<endpoint>/``, and log
directories nest (``logs/experiments/E1_gbn_loss00_trial00/sender/``), so a bare
``run_id`` is not unique across the tree. Each run therefore carries a ``key`` —
its path relative to ``logs/`` — alongside the ``run_id`` it recorded. The key is
an addressing detail; the run id is what the UI shows and what
``experiment_runs.csv`` is joined on (T11.11).
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Iterator

import config
import metrics as metrics_mod

#: Where each artifact lives. Kept together so a relocation is one edit and so
#: the UI can state, in its own error messages, what it went looking for.
LOG_DIR = config.LOG_DIR
PLOT_DIR = config.PLOT_DIR
CAPTURE_DIR = config.CAPTURE_DIR
RESULTS_DIR = config.PROJECT_ROOT / "experiments" / "results"

EXPERIMENT_RUNS_CSV = RESULTS_DIR / "experiment_runs.csv"
AGGREGATE_CSV = RESULTS_DIR / "aggregate.csv"
FIGURES_MD = RESULTS_DIR / "figures.md"
DEMONSTRATION_MD = RESULTS_DIR / "demonstration.md"
CALIBRATION_MD = RESULTS_DIR / "calibration.md"
CAPTURES_MD = CAPTURE_DIR / "README.md"

ENDPOINTS = ("sender", "receiver")

#: How far behind the live view can be, and why. ``eventlog.FLUSH_EVERY`` is 64
#: on purpose — a per-event ``fsync`` would distort the timings the log exists to
#: measure — so a tailing reader legitimately trails the transfer. T11.4 states
#: this in the UI rather than hiding it; the flush policy is not to be changed to
#: make the dashboard smoother.
from eventlog import EVENT_COLUMNS, FLUSH_EVERY  # noqa: E402

LIVE_LAG_NOTE = (
    f"Near-real-time: events.csv is flushed every {FLUSH_EVERY} rows, so this view "
    f"can trail the transfer by up to {FLUSH_EVERY} events. The flush policy is "
    f"deliberate — per-event fsync would distort the timings being measured."
)


class DataError(Exception):
    """A request named something that is not on disk."""


# ---------------------------------------------------------------------------
# Run discovery
# ---------------------------------------------------------------------------


def _is_run_directory(path: Path) -> bool:
    return any((path / endpoint / "events.csv").exists() for endpoint in ENDPOINTS)


def _iter_run_directories(root: Path | None = None) -> Iterator[Path]:
    """Every directory under ``logs/`` that holds an endpoint's log.

    Log directories nest, so this walks rather than listing one level. A run
    directory never contains another (its children are ``sender``/``receiver``),
    so the walk does not descend into one it has already matched.

    ``root`` defaults to :data:`LOG_DIR` at call time rather than in the
    signature: a default argument would bind the module's value at import and
    then ignore a relocated log directory, which is exactly what a test that
    points the layer at a temporary tree needs to be able to do.
    """
    root = LOG_DIR if root is None else root
    if not root.exists():
        return
    stack = [root]
    while stack:
        current = stack.pop()
        if _is_run_directory(current):
            yield current
            continue
        try:
            children = sorted(child for child in current.iterdir() if child.is_dir())
        except OSError:
            continue
        stack.extend(children)


def run_key(directory: Path) -> str:
    """This run's stable address: its path relative to ``logs/``, POSIX-style."""
    return directory.resolve().relative_to(LOG_DIR.resolve()).as_posix()


def resolve_run(key: str) -> Path:
    """The directory a key names, refusing anything outside ``logs/``.

    The key arrives from a URL, so it is treated as untrusted: the resolved path
    must still sit under ``logs/``, which rules out ``..`` and absolute paths.
    """
    if not key:
        raise DataError("no run was named")
    candidate = (LOG_DIR / key).resolve()
    root = LOG_DIR.resolve()
    if root != candidate and root not in candidate.parents:
        raise DataError(f"{key!r} is outside {LOG_DIR.name}/")
    if not _is_run_directory(candidate):
        raise DataError(f"no events.csv under logs/{key}")
    return candidate


def _read_summary(directory: Path, endpoint: str) -> dict[str, Any] | None:
    path = directory / endpoint / "summary.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A run killed mid-write leaves a truncated summary. That is a real
        # state the UI has to show (T11.14), not a reason to hide the run.
        return None


def _first_row(path: Path) -> dict[str, str] | None:
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                return row
    except OSError:
        return None
    return None


def _last_rows(path: Path, count: int = 1) -> list[dict[str, str]]:
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return []
    return rows[-count:]


def run_index_entry(directory: Path) -> dict[str, Any]:
    """One row of the run browser, read cheaply enough to list hundreds."""
    key = run_key(directory)
    sender = _read_summary(directory, "sender")
    receiver = _read_summary(directory, "receiver")
    summary = sender or receiver or {}

    identifier = summary.get("run_id")
    if not identifier:
        first = _first_row(directory / "sender" / "events.csv") or \
            _first_row(directory / "receiver" / "events.csv")
        identifier = (first or {}).get("run_id") or directory.name

    sender_metrics = (sender or {}).get("metrics", {})
    receiver_metrics = (receiver or {}).get("metrics", {})
    sender_config = (sender or {}).get("config", {})

    # Integrity is read from whichever endpoint recorded a verdict, and stays
    # None when neither did. A run with no verdict is not a passing run (CC-01).
    integrity = sender_metrics.get("integrity_success")
    if integrity is None:
        integrity = receiver_metrics.get("integrity_success")

    return {
        "key": key,
        "run_id": identifier,
        "group": str(Path(key).parent.as_posix()) if "/" in key else "",
        "endpoints": [e for e in ENDPOINTS if (directory / e / "events.csv").exists()],
        "mode": sender_metrics.get("final_active_mode") or sender_metrics.get("mode")
        or receiver_metrics.get("final_mode"),
        "requested_mode": (sender or {}).get("mode") or receiver_metrics.get("requested_mode"),
        "started_wall_clock": summary.get("started_wall_clock"),
        "duration_s": summary.get("duration_s"),
        "completion_time_s": sender_metrics.get("completion_time_s"),
        "goodput_bytes_per_s": sender_metrics.get("goodput_bytes_per_s"),
        "retransmission_count": sender_metrics.get("retransmission_count"),
        "switch_count": sender_metrics.get("switch_count"),
        "integrity_success": integrity,
        "loss_rate": sender_config.get("loss_rate"),
        "rtt_ms": sender_config.get("rtt_ms"),
        "seed": sender_config.get("random_seed"),
        "file_bytes": sender_config.get("transfer_file_size_bytes"),
        "error": sender_metrics.get("error") or receiver_metrics.get("error"),
        "complete": bool(sender) and bool(receiver),
        "mtime": directory.stat().st_mtime,
    }


def list_runs() -> list[dict[str, Any]]:
    """Every run on disk, newest first (T11.11)."""
    entries = [run_index_entry(directory) for directory in _iter_run_directories()]
    entries.sort(key=lambda entry: entry["mtime"], reverse=True)
    return entries


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


def read_events(directory: Path, endpoint: str) -> list[dict[str, str]]:
    path = directory / endpoint / "events.csv"
    if not path.exists():
        return []
    try:
        return metrics_mod.read_events(path)
    except OSError:
        return []


def all_events(directory: Path) -> list[dict[str, str]]:
    """Both endpoints' rows, each tagged with its own endpoint.

    They are *not* merged onto a common timeline: the two logs have different
    origins and subtracting one from the other would produce a number that looks
    precise and means nothing (specs.md §21). Ordering is per endpoint, and the
    UI labels which endpoint each row came from.
    """
    rows: list[dict[str, str]] = []
    for endpoint in ENDPOINTS:
        rows.extend(read_events(directory, endpoint))
    return rows


def run_detail(key: str) -> dict[str, Any]:
    """Everything the overview, metrics and integrity panels need for one run."""
    directory = resolve_run(key)
    sender = _read_summary(directory, "sender")
    receiver = _read_summary(directory, "receiver")
    events = all_events(directory)

    derived = metrics_mod.derive_metrics(events) if events else {}

    # Both sides of the integrity comparison, each from the endpoint that owns
    # it: the sender hashed the source, the receiver hashed what it wrote
    # (T11.4). Neither is inferred from the other.
    integrity = {
        "expected_sha256": (sender or {}).get("sha256")
        or (receiver or {}).get("expected_sha256"),
        "received_sha256": (receiver or {}).get("received_sha256"),
        "success": derived.get("integrity_success"),
        "verdict_reasons": [row["reason"] for row in events
                            if row["event"] == "FIN_ACK" and row["reason"]],
    }

    return {
        "key": key,
        "run_id": (sender or receiver or {}).get("run_id", directory.name),
        "index": run_index_entry(directory),
        "sender_summary": sender,
        "receiver_summary": receiver,
        "derived_metrics": derived,
        "integrity": integrity,
        "event_counts": {
            endpoint: (_read_summary(directory, endpoint) or {}).get("event_counts", {})
            for endpoint in ENDPOINTS
        },
        "live_lag_note": LIVE_LAG_NOTE,
    }


def _matches(row: dict[str, str], *, event: str, mode: str, sequence: str) -> bool:
    if event and row["event"] != event:
        return False
    if mode and (row["mode"] or "").lower() != mode.lower():
        return False
    if sequence and row["sequence"] != sequence and row["ack"] != sequence:
        return False
    return True


def run_events(key: str, *, endpoint: str = "", offset: int = 0, limit: int = 500,
               event: str = "", mode: str = "", sequence: str = "") -> dict[str, Any]:
    """A page of the §21 event rows, filtered (T11.7).

    ``offset`` is an index into the *unfiltered* log of the chosen endpoint, so a
    live tail can ask for "everything after row N" and get the same answer
    whether or not a filter is applied afterwards.
    """
    directory = resolve_run(key)
    endpoints = [endpoint] if endpoint in ENDPOINTS else list(ENDPOINTS)

    rows: list[dict[str, str]] = []
    totals: dict[str, int] = {}
    for name in endpoints:
        source = read_events(directory, name)
        totals[name] = len(source)
        rows.extend(source[offset:] if len(endpoints) == 1 else source)

    filtered = [row for row in rows if _matches(row, event=event, mode=mode,
                                                sequence=sequence)]
    limit = max(1, min(limit, 5000))
    return {
        "key": key,
        "columns": EVENT_COLUMNS,
        "totals": totals,
        "matched": len(filtered),
        "offset": offset,
        "rows": filtered[:limit],
        "truncated": len(filtered) > limit,
        "live_lag_note": LIVE_LAG_NOTE,
    }


# ---------------------------------------------------------------------------
# Mode timeline (T11.5)
# ---------------------------------------------------------------------------

#: A SWITCH row carrying this reason is ``--mode fixed-hybrid`` paying the drain
#: cost without changing semantics. It is shown as the control doing its job, not
#: as a mode change, and is excluded from the transition count (metrics.py).
NOOP_MARKER = metrics_mod.NOOP_SWITCH_MARKER


def mode_timeline(key: str) -> dict[str, Any]:
    """Switches, MODE handshakes and residence, taken from the recorded rows.

    Every field below is read out of the log. The reason string is the
    controller's own — ``protocol/hybrid.py`` wrote it at the moment it decided —
    and the loss estimate is the value the estimator held at that instant. The UI
    re-derives neither (T11.5).
    """
    directory = resolve_run(key)
    sender = read_events(directory, "sender")

    transitions = []
    previous = None
    for row in sender:
        if row["event"] != "SWITCH":
            continue
        noop = NOOP_MARKER in row["reason"]
        entry = {
            "timestamp": _as_float(row["timestamp"]),
            "from_mode": previous,
            "to_mode": row["mode"],
            "reason": row["reason"],
            "loss_estimate": _as_float(row["loss_estimate"]),
            "window_size": _as_int(row["window_size"]),
            "noop": noop,
            "epoch": _epoch_from(row["reason"]),
        }
        transitions.append(entry)
        if not noop:
            previous = row["mode"]
        elif previous is None:
            previous = row["mode"]

    handshakes = [{
        "timestamp": _as_float(row["timestamp"]),
        "mode": row["mode"],
        "reason": row["reason"],
        "epoch": _epoch_from(row["reason"]),
        "loss_estimate": _as_float(row["loss_estimate"]),
    } for row in sender if row["event"] == "MODE"]

    residence = metrics_mod.mode_residence(sender)
    span = _as_float(sender[-1]["timestamp"]) if sender else 0.0

    # Where the mode was live, as spans, so the timeline is drawn from the log's
    # own ``mode`` column rather than from the transitions alone — an abandoned
    # handshake leaves the mode unchanged and the two must agree.
    bands = []
    for row in sender:
        if not row["mode"]:
            continue
        moment = _as_float(row["timestamp"])
        if moment is None:
            continue
        if bands and bands[-1]["mode"] == row["mode"]:
            bands[-1]["end"] = moment
        else:
            if bands:
                bands[-1]["end"] = moment
            bands.append({"mode": row["mode"], "start": moment, "end": moment})

    return {
        "key": key,
        "transitions": transitions,
        "handshakes": handshakes,
        "bands": bands,
        "residence_s": residence,
        "span_s": span,
        "switch_count": metrics_mod.switch_count(sender),
        "handshake_count": metrics_mod.handshake_count(sender),
        "noop_count": sum(1 for entry in transitions if entry["noop"]),
        "current_mode": bands[-1]["mode"] if bands else None,
        "previous_mode": next((entry["from_mode"] for entry in reversed(transitions)
                               if not entry["noop"]), None),
    }


def _epoch_from(reason: str) -> int | None:
    """The ``epoch=N`` the endpoint wrote into the reason, if it wrote one."""
    match = re.search(r"epoch=(\d+)", reason or "")
    return int(match.group(1)) if match else None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Network conditions (T11.6) and retransmissions (T11.8)
# ---------------------------------------------------------------------------


def network_view(key: str) -> dict[str, Any]:
    """Configured impairment beside observed measurement — kept apart (T11.6).

    ``configured`` is what the run was *told* to do: the seeded simulator's loss
    probability, RTT and jitter, straight out of ``summary.json``. ``observed``
    is what the transfer measured: the D8 estimator's readings and the RTT
    samples. They are returned under separate keys because conflating a
    simulated condition with a measurement is the specific failure this panel
    exists to avoid.
    """
    directory = resolve_run(key)
    sender = read_events(directory, "sender")
    summary = _read_summary(directory, "sender") or {}
    run_config = summary.get("config", {})

    estimate_series = [
        {"timestamp": _as_float(row["timestamp"]),
         "value": _as_float(row["loss_estimate"]),
         "mode": row["mode"]}
        for row in sender
        if row["loss_estimate"] not in ("", None)
    ]
    rtt_series = [
        {"timestamp": _as_float(row["timestamp"]), "value": _as_float(row["rtt_ms"])}
        for row in sender if row["rtt_ms"] not in ("", None)
    ]
    retx_series = [
        {"timestamp": _as_float(row["timestamp"]), "sequence": _as_int(row["sequence"])}
        for row in sender if row["event"] == "RETX"
    ]
    loss_changes = [
        {"timestamp": _as_float(row["timestamp"]), "reason": row["reason"]}
        for row in sender if row["event"] == "LOSS_CHANGE"
    ]

    return {
        "key": key,
        "configured": {
            "loss_rate": run_config.get("loss_rate"),
            "ack_loss_rate": run_config.get("ack_loss_rate"),
            "rtt_ms": run_config.get("rtt_ms"),
            "jitter_ms": run_config.get("jitter_ms"),
            "loss_schedule": run_config.get("loss_schedule"),
            "seed": run_config.get("random_seed"),
            "rto_s": run_config.get("rto_s"),
            "impairment": summary.get("impairment"),
        },
        "observed": {
            "loss_estimate_series": estimate_series,
            "rtt_series": rtt_series,
            "retransmission_series": retx_series,
            "loss_changes": loss_changes,
            "retransmission_count": metrics_mod.retransmission_count(sender),
            "retransmission_overhead": metrics_mod.retransmission_overhead(sender),
            "rtt_statistics": metrics_mod.rtt_statistics(sender),
            "drop_count": sum(1 for row in sender if row["event"] == "DROP"),
        },
        "estimator": {
            "window": config.LOSS_WINDOW_SIZE,
            "switch_high": config.SWITCH_HIGH,
            "switch_low": config.SWITCH_LOW,
            "hysteresis_count": config.HYSTERESIS_COUNT,
            "caveat": (
                "The loss estimate is a reading of the D8 estimator — the fraction "
                "of the last 50 acknowledged segment outcomes that needed a "
                "retransmission — not a physical loss rate. Under GBN it over-reads "
                "true loss by roughly four to five times, so SWITCH_HIGH = 0.10 "
                "fires at about 2% physical loss."),
        },
    }


def retransmission_view(key: str) -> dict[str, Any]:
    """Sequence-number timeline of what actually went on the wire (T11.8).

    Only ``SEND``, ``RETX`` and ``DROP`` rows are returned, each at the sequence
    and instant the log recorded. Nothing is interpolated, and nothing is drawn
    that did not happen: GBN's range retransmission and SR's single-segment
    retransmission are different shapes because the events are different, not
    because the view styles them differently.
    """
    directory = resolve_run(key)
    sender = read_events(directory, "sender")

    def points(name: str) -> list[dict[str, Any]]:
        return [{"timestamp": _as_float(row["timestamp"]),
                 "sequence": _as_int(row["sequence"]),
                 "mode": row["mode"],
                 "reason": row["reason"]}
                for row in sender
                if row["event"] == name and row["sequence"] not in ("", None)]

    sends, retx, drops = points("SEND"), points("RETX"), points("DROP")
    timeline = mode_timeline(key)
    sequences = [point["sequence"] for point in sends + retx
                 if point["sequence"] is not None]

    return {
        "key": key,
        "sends": sends,
        "retransmissions": retx,
        "drops": drops,
        "mode_bands": timeline["bands"],
        "max_sequence": max(sequences) if sequences else 0,
        "span_s": timeline["span_s"],
        "counts": {"send": len(sends), "retx": len(retx), "drop": len(drops)},
    }


# ---------------------------------------------------------------------------
# Recorded experiment results (T11.10, T11.11)
# ---------------------------------------------------------------------------


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def experiment_runs() -> dict[str, Any]:
    """``experiment_runs.csv`` as recorded — the 195-run matrix index (RP-01)."""
    rows = _read_csv(EXPERIMENT_RUNS_CSV)
    for row in rows:
        row["log_key"] = _log_key_for(row.get("log_dir", ""))
    return {
        "path": _relative(EXPERIMENT_RUNS_CSV),
        "exists": EXPERIMENT_RUNS_CSV.exists(),
        "rows": rows,
        "count": len(rows),
    }


def _log_key_for(recorded: str) -> str | None:
    """Map a recorded absolute ``log_dir`` back to a key under this checkout.

    The matrix recorded absolute paths, and a checkout may live elsewhere, so
    only the tail below ``logs/`` is used. A run whose directory is no longer
    present returns None and the UI says the logs are not on this machine
    rather than linking to nothing (T11.14).
    """
    if not recorded:
        return None
    parts = Path(recorded.replace("\\", "/")).parts
    if "logs" not in parts:
        return None
    key = "/".join(parts[parts.index("logs") + 1:])
    return key if key and (LOG_DIR / key).exists() else None


def aggregate() -> dict[str, Any]:
    """``aggregate.csv``: one row per (experiment, condition, system) (T9.1)."""
    rows = _read_csv(AGGREGATE_CSV)
    return {
        "path": _relative(AGGREGATE_CSV),
        "exists": AGGREGATE_CSV.exists(),
        "rows": rows,
        "systems": sorted({row["system"] for row in rows if row.get("system")}),
        "experiments": sorted({row["experiment"] for row in rows if row.get("experiment")}),
    }


def figures() -> dict[str, Any]:
    """The generated figures, with the reading ``figures.md`` recorded for each.

    The commentary is not rewritten here: the table in ``figures.md`` already
    says what each figure shows and how to read it, including the two that
    reflect badly on the hybrid, and the UI shows that text verbatim (T11.10).
    """
    readings = _figure_readings()
    images = sorted(PLOT_DIR.glob("*.png")) if PLOT_DIR.exists() else []
    return {
        "exists": bool(images),
        "directory": _relative(PLOT_DIR),
        "notes_path": _relative(FIGURES_MD),
        "figures": [{
            "name": image.name,
            "url": f"/plots/{image.name}",
            "shows": readings.get(image.name, {}).get("shows"),
            "reading": readings.get(image.name, {}).get("reading"),
            "required": readings.get(image.name, {}).get("required"),
            "bytes": image.stat().st_size,
        } for image in images],
    }


def _figure_readings() -> dict[str, dict[str, Any]]:
    """Parse the two markdown tables of ``figures.md`` (T9.2/T9.3 commentary)."""
    if not FIGURES_MD.exists():
        return {}
    readings: dict[str, dict[str, Any]] = {}
    required = True
    for line in FIGURES_MD.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("## Optional"):
            required = False
        elif stripped.startswith("## Required"):
            required = True
        if not stripped.startswith("| `"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        name = cells[0].strip("`")
        readings[name] = {"shows": cells[1], "reading": cells[2], "required": required}
    return readings


# ---------------------------------------------------------------------------
# Wireshark companion (T11.12) and demonstration (T11.13)
# ---------------------------------------------------------------------------


def captures() -> dict[str, Any]:
    """The curated capture set, each with the run it was taken from.

    ``captures/`` holds the T9.4 evidence set and the demonstration capture —
    one per *scenario*, not one per transfer. Nothing here implies an arbitrary
    run has a capture, and :func:`capture_for_run` says so explicitly.
    """
    if not CAPTURE_DIR.exists():
        return {"exists": False, "captures": [], "notes": None}
    described = _capture_notes()
    files = sorted(CAPTURE_DIR.glob("*.pcapng"))
    return {
        "exists": bool(files),
        "directory": _relative(CAPTURE_DIR),
        "notes_path": _relative(CAPTURES_MD),
        "filter": f"udp.port == {config.PORT}",
        "dissector": "tools/hybrid_arq.lua",
        "captures": [{
            "name": path.name,
            "bytes": path.stat().st_size,
            "expectation": described.get(path.name, {}).get("expectation"),
            "detail": described.get(path.name, {}).get("detail"),
        } for path in files],
    }


def _capture_notes() -> dict[str, dict[str, str]]:
    """Per-capture expectation and bullet list, from ``captures/README.md``."""
    if not CAPTURES_MD.exists():
        return {}
    notes: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in CAPTURES_MD.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^## `(.+)`\s*$", line)
        if heading:
            current = heading.group(1)
            notes[current] = {"expectation": "", "detail": ""}
            continue
        if current is None:
            continue
        if line.startswith("**Expected observation"):
            notes[current]["expectation"] = line.split(":**", 1)[-1].strip()
        elif line.startswith("- "):
            notes[current]["detail"] += line[2:].strip() + "\n"
    return notes


def capture_for_run(key: str) -> dict[str, Any]:
    """What Wireshark can add to this run, stated honestly (T11.12).

    The dashboard reads logs; Wireshark reads packets. This separates the two so
    the panel can say which facts come from where, and it does **not** claim the
    frontend inspects packets. A run with no capture of its own is told so, with
    the command that would record one.
    """
    detail = run_detail(key)
    run_config = (detail["sender_summary"] or {}).get("config", {})
    port = run_config.get("port") or config.PORT
    timeline = mode_timeline(key)
    directory = resolve_run(key)
    sender = read_events(directory, "sender")

    retx = [row for row in sender if row["event"] == "RETX"]
    return {
        "key": key,
        "run_id": detail["run_id"],
        "port": port,
        "filter": f"udp.port == {port}",
        "dissector": "tools/hybrid_arq.lua",
        "current_mode": timeline["current_mode"],
        "switch_timestamps": [entry["timestamp"] for entry in timeline["transitions"]
                              if not entry["noop"]],
        "first_retransmissions": [{"timestamp": _as_float(row["timestamp"]),
                                   "sequence": _as_int(row["sequence"]),
                                   "mode": row["mode"]} for row in retx[:10]],
        "retransmission_count": len(retx),
        # No capture is claimed for an arbitrary run: captures/ is a curated
        # per-scenario set, so this is a *reference* to the nearest recorded
        # evidence, labelled as being from another run.
        "own_capture": None,
        "reference_captures": captures()["captures"],
        "provenance": {
            "from_logs": ["controller state and switch reasons", "loss estimate",
                          "every metric in specs.md §20", "retransmission counts"],
            "from_capture": ["the packets actually on the wire", "header fields at fixed offsets",
                             "duplicate sequences as the network saw them"],
        },
        "note": (
            "This panel points Wireshark at the run; it does not inspect packets "
            "itself. Captures in captures/ are the curated per-scenario evidence "
            "set, not one capture per transfer."),
    }


#: The thirteen steps of specs.md §28, in order, each named by what the evaluator
#: should be looking at. ``experiments/demonstrate.py`` already performs the
#: sequence headlessly and writes a transcript; the UI presents that same run
#: rather than inventing a parallel script (T11.13).
DEMO_STEPS = (
    (1, "Start receiver", "The receiver binds and waits; its log opens when a START names the run."),
    (2, "Start Wireshark capture", f"Filter udp.port == {config.PORT} on the loopback adapter."),
    (3, "Start a hybrid file-transfer run", "One transfer, controller active from segment 0."),
    (4, "Show initial GBN behaviour under clean conditions",
     "DEFAULT_MODE is GBN, so every transfer begins there and has to earn a change."),
    (5, "Introduce sustained packet loss", "A seeded loss schedule, so the drops replay exactly."),
    (6, "Show the controller detecting the changed condition",
     "The loss estimate climbs; three confirmations are required before it acts."),
    (7, "Show transition to SR", "A SWITCH row with its reason and the estimate at that instant."),
    (8, "Show selective retransmission in Wireshark",
     "Single-segment retransmission instead of a whole range."),
    (9, "Restore low-loss conditions", "The reverse transition needs the dead band crossed."),
    (10, "Show SR→GBN after hysteresis", "Return below SWITCH_LOW, confirmed three times."),
    (11, "Stop capture", "The capture is evidence; it is kept, not regenerated."),
    (12, "Verify source and received file hashes",
     "Both sides shown; a mismatch is a reported failure, never softened (CC-01)."),
    (13, "Display metrics and compare with pure GBN and pure SR",
     "The recorded matrix, including the conditions where the hybrid loses."),
)


def demonstration() -> dict[str, Any]:
    """The §28 script alongside the recorded transcript, if one exists."""
    transcript = None
    if DEMONSTRATION_MD.exists():
        transcript = DEMONSTRATION_MD.read_text(encoding="utf-8")
    demo_runs = [entry for entry in list_runs() if entry["key"].startswith("demo/")]
    return {
        "steps": [{"number": number, "title": title, "detail": detail}
                  for number, title, detail in DEMO_STEPS],
        "transcript": transcript,
        "transcript_path": _relative(DEMONSTRATION_MD),
        "runs": demo_runs,
        "command": "python experiments/demonstrate.py",
        "note": (
            "The steps below are performed by experiments/demonstrate.py, which "
            "writes the transcript and the capture. This screen presents that "
            "recorded run — there is no separate demo script and no animation."),
    }


# ---------------------------------------------------------------------------
# Configuration surface (T11.3)
# ---------------------------------------------------------------------------

#: What a UI-launched run may set, with the bounds the server validates against.
#: Anything absent from this table is not editable from the dashboard — the
#: frozen values below are shown as read-only context, because a threshold typed
#: into a form is a decision made without evidence (T11.3).
EDITABLE_FIELDS = {
    "mode": {"type": "choice", "choices": ["gbn", "sr", "hybrid", "fixed-hybrid"],
             "default": "hybrid", "label": "ARQ mode"},
    "host": {"type": "text", "default": config.HOST, "label": "Receiver host"},
    "port": {"type": "int", "min": 1, "max": 65535, "default": config.PORT,
             "label": "UDP port", "note": "8888 matches the Wireshark filter"},
    "window": {"type": "int", "min": 1, "max": 64, "default": config.WINDOW_SIZE,
               "label": "Window size"},
    "file_kib": {"type": "int", "min": 1, "max": 8192, "default": 256,
                 "label": "Generated file size (KiB)",
                 "note": "a deterministic pseudo-random source file, as the matrix used"},
    "loss": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.0,
             "label": "Forward DATA loss probability"},
    "ack_loss": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.0,
                 "label": "Reverse ACK loss probability"},
    "rtt": {"type": "float", "min": 0.0, "max": 2000.0, "default": 0.0,
            "label": "Emulated RTT (ms)"},
    "jitter": {"type": "float", "min": 0.0, "max": 1000.0, "default": 0.0,
               "label": "Delay jitter (ms)"},
    "seed": {"type": "int", "min": 0, "max": 2**31 - 1, "default": 20260917,
             "label": "Impairment seed",
             "note": "the same seed replays the same drop sequence (RP-03)"},
    "loss_schedule": {"type": "text", "default": "", "label": "Loss schedule",
                      "note": "t:rate,t:rate — a step function for dynamic loss (§17.3)"},
}

#: Shown, never edited. Each one is frozen against recorded evidence, so making
#: it a form field would invite a change that silently invalidates experiments.
READ_ONLY_FIELDS = (
    ("Segment size", f"{config.SEGMENT_SIZE} B", "D3 — frozen at T1.3"),
    ("Switch high", f"{config.SWITCH_HIGH}", "D9 — frozen at T7.2, a reading of the estimator"),
    ("Switch low", f"{config.SWITCH_LOW}", "D9 — frozen at T7.2"),
    ("Hysteresis", f"{config.HYSTERESIS_COUNT} confirmations", "D9 — frozen at T7.2"),
    ("Evaluation cadence", f"{config.EVALUATION_INTERVAL_SEGMENTS} acked segments",
     "D9 — held fixed throughout the calibration sweep"),
    ("Minimum residence", f"{config.MIN_MODE_RESIDENCE_S} s", "D9 — frozen at T7.2"),
    ("Loss window", f"{config.LOSS_WINDOW_SIZE} outcomes", "D8 — frozen at T5.2"),
    ("RTO policy", f"max({config.RTO_RTT_MULTIPLIER:g} x RTT, {config.RTO_FLOOR_S * 1000:g} ms)",
     "D7 — derived per condition, then held fixed"),
    ("Starting mode", config.DEFAULT_MODE, "both endpoints read the same value"),
)


def configuration() -> dict[str, Any]:
    """What the control panel may set, and what it may only display (T11.3)."""
    return {
        "editable": EDITABLE_FIELDS,
        "read_only": [{"name": name, "value": value, "why": why}
                      for name, value, why in READ_ONLY_FIELDS],
        "snapshot": config.snapshot(),
        "decisions": config.frozen_decisions(),
        "rto_note": (
            "RTO is not a form field: D7 derives it from the condition's RTT as "
            f"max({config.RTO_RTT_MULTIPLIER:g} x RTT, {config.RTO_FLOOR_S * 1000:g} ms) "
            "and then holds it fixed for the whole run."),
    }


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(config.PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def artifact_inventory() -> dict[str, Any]:
    """Which recorded artifacts are present, for the empty states (T11.14)."""
    return {
        "logs": {"path": _relative(LOG_DIR), "exists": LOG_DIR.exists(),
                 "runs": sum(1 for _ in _iter_run_directories())},
        "experiment_runs": {"path": _relative(EXPERIMENT_RUNS_CSV),
                            "exists": EXPERIMENT_RUNS_CSV.exists()},
        "aggregate": {"path": _relative(AGGREGATE_CSV), "exists": AGGREGATE_CSV.exists()},
        "plots": {"path": _relative(PLOT_DIR),
                  "exists": PLOT_DIR.exists() and any(PLOT_DIR.glob("*.png"))},
        "captures": {"path": _relative(CAPTURE_DIR),
                     "exists": CAPTURE_DIR.exists() and any(CAPTURE_DIR.glob("*.pcapng"))},
        "demonstration": {"path": _relative(DEMONSTRATION_MD),
                          "exists": DEMONSTRATION_MD.exists()},
        "calibration": {"path": _relative(CALIBRATION_MD), "exists": CALIBRATION_MD.exists()},
    }
