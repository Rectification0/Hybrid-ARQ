"""Event logging: the single emitter behind both endpoints (T2.5, design.md §9).

Two artifacts per run, written under ``logs/<run_id>/<endpoint>/``:

* ``events.csv``  — one row per protocol event, columns exactly as specs.md §21
* ``summary.json`` — the run configuration plus the final metrics of specs.md §20

Sender and receiver are separate processes, so each gets its own subdirectory;
that is the only extension to design.md §9, which specifies the two filenames
but assumes one writer. The columns and the event vocabulary are shared, which
is what lets the two logs be concatenated and interleaved during analysis.

Why this module exists rather than a ``print`` in each endpoint:

* **Every metric in specs.md §20 must be derivable from events.csv alone**
  (CC-06, FR-14). Nothing may be computed in memory and only printed, because
  the raw logs have to be sufficient to explain any result after the fact.
* ``timestamp`` is seconds since the emitter was created, from a *monotonic*
  clock, so sender and receiver logs interleave correctly without the two
  processes agreeing on wall-clock time.
* Logs are append-only and never rewritten (RP-07). Aggregation reads them.
* Writes are buffered and flushed periodically. Per-event ``fsync`` would
  distort the very timings the log exists to measure.
"""

from __future__ import annotations

import csv
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import config

#: specs.md §21, in order. The header of every events.csv.
EVENT_COLUMNS = [
    "timestamp", "run_id", "endpoint", "event", "sequence", "ack",
    "mode", "window_size", "loss_estimate", "rtt_ms", "reason",
]

#: design.md §9. Emitting a name outside this set raises, so a typo cannot
#: silently produce an event that the T6.1 audit would never find.
#:
#: ``MODE`` was added in T5.4: every other control packet type already had an
#: event of its own, and the MODE *exchange* has to stay distinguishable from
#: the ``SWITCH`` it may or may not produce — a handshake that is refused,
#: repeated or abandoned is exactly the case a reader needs to see.
EVENT_NAMES = frozenset({
    "SEND", "RETX", "ACK", "TIMEOUT", "TIMER_START", "TIMER_STOP", "SWITCH",
    "DROP", "CHECKSUM_FAIL", "MALFORMED", "DUPLICATE", "DELIVER", "LOSS_CHANGE",
    "START", "START_ACK", "FIN", "FIN_ACK", "MODE", "ERROR",
})

ENDPOINTS = frozenset({"sender", "receiver"})

#: Rows buffered before a flush. Small enough that a crashed run still leaves
#: most of its log on disk, large enough not to perturb timing.
FLUSH_EVERY = 64


def software_version() -> dict[str, Any]:
    """Identify the code that produced a run (RP-08).

    The commit hash is what lets a result be traced back to the exact protocol
    behavior that produced it, which matters most when a decision is later
    unfrozen and experiments are re-run.
    """
    commit, dirty = None, None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(config.PROJECT_ROOT),
        )
        if result.returncode == 0:
            commit = result.stdout.strip()
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=5,
                cwd=str(config.PROJECT_ROOT),
            )
            dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        pass  # git absent or not a repo: record what we can, never fail a run
    return {
        "commit": commit,
        "working_tree_dirty": dirty,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }


class EventLog:
    """Append-only event writer for one endpoint of one run."""

    def __init__(self, run_id: str, endpoint: str, log_dir: Path | str | None = None):
        if endpoint not in ENDPOINTS:
            raise ValueError(f"endpoint must be one of {sorted(ENDPOINTS)}, got {endpoint!r}")

        self.run_id = run_id
        self.endpoint = endpoint
        self.directory = Path(log_dir or config.LOG_DIR) / run_id / endpoint
        self.directory.mkdir(parents=True, exist_ok=True)

        self.events_path = self.directory / "events.csv"
        self.summary_path = self.directory / "summary.json"

        self._file = open(self.events_path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(EVENT_COLUMNS)
        self._pending = 0
        self._closed = False

        # Monotonic so the timeline cannot jump if the system clock is adjusted
        # mid-transfer; wall-clock start is recorded separately in the summary.
        self._t0 = time.monotonic()
        self.started_wall = time.time()

        self.counts: dict[str, int] = {}

    # -- emission ----------------------------------------------------------

    @property
    def elapsed(self) -> float:
        """Seconds since this log was created — the transfer's own clock."""
        return time.monotonic() - self._t0

    def emit(self, event: str, *, sequence: int | None = None, ack: int | None = None,
             mode: str | None = None, window_size: int | None = None,
             loss_estimate: float | None = None, rtt_ms: float | None = None,
             reason: str | None = None) -> None:
        if event not in EVENT_NAMES:
            raise ValueError(f"{event!r} is not in the design.md §9 event vocabulary")
        if self._closed:
            raise RuntimeError("cannot emit after close()")

        self.counts[event] = self.counts.get(event, 0) + 1
        self._writer.writerow([
            f"{self.elapsed:.6f}", self.run_id, self.endpoint, event,
            "" if sequence is None else sequence,
            "" if ack is None else ack,
            "" if mode is None else mode,
            "" if window_size is None else window_size,
            "" if loss_estimate is None else f"{loss_estimate:.6f}",
            "" if rtt_ms is None else f"{rtt_ms:.3f}",
            "" if reason is None else reason,
        ])

        self._pending += 1
        if self._pending >= FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        if not self._closed:
            self._file.flush()
            self._pending = 0

    # -- summary -----------------------------------------------------------

    def write_summary(self, metrics: dict[str, Any], *, extra: dict[str, Any] | None = None) -> Path:
        """Write summary.json: what was run, on what code, with what result."""
        document = {
            "run_id": self.run_id,
            "endpoint": self.endpoint,
            "started_wall_clock": self.started_wall,
            "duration_s": self.elapsed,
            "config": config.snapshot(),
            "software": software_version(),
            "event_counts": dict(sorted(self.counts.items())),
            "metrics": metrics,
        }
        if extra:
            document.update(extra)
        with open(self.summary_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return self.summary_path

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if not self._closed:
            self._file.flush()
            os.fsync(self._file.fileno())  # once, at the end, not per event
            self._file.close()
            self._closed = True

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
