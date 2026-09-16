"""Derive the specs.md §20 metrics from ``events.csv`` alone (T6.2).

CC-06 requires that every metric in specs.md §20 be computable from the event
log, with nothing existing only in memory and printed. That requirement is easy
to *state* and easy to quietly violate, so this module is the check: it takes
event rows and nothing else, and produces the metrics. The endpoints compute the
same numbers as they run, and a test asserts the two agree — if a metric were
only ever computed in memory, the derivation here would have nothing to read.

It is deliberately standard library only, like the protocol itself. The pandas
aggregation of T9.1 is a different job: it spans runs, averages trials and draws
graphs. This spans one log and answers "what does this run say about itself".

Reading rule (RP-07): logs are opened read-only and never modified.

WHAT NEEDS WHICH ENDPOINT
=========================
The two endpoints see different things, and no metric is invented from the
wrong side:

* the **sender's** log carries SEND / RETX and their byte counts, so
  retransmission count, retransmission overhead and the RTT samples come from
  there, as do mode residence and switch count;
* the **receiver's** log carries DELIVER, so delivered application bytes — and
  therefore goodput — come from there.

Passing both logs fills in everything. Passing one fills in what that endpoint
can honestly answer and leaves the rest as None, rather than guessing.

TIMESTAMPS
==========
Each log's timestamps are seconds from *that endpoint's* start, on the
``time.perf_counter`` clock, so the two are not on a common origin. Every figure
below is computed within a single log, and the two are never subtracted from one
another. To interleave them for reading, align on the START row the two share.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path
from typing import Any, Iterable, Sequence

#: A SWITCH row with this in its reason is the ``fixed-hybrid`` control paying
#: the drain cost without changing mode (T5.6). It is a handshake, not a
#: transition, and must not be counted as one.
NOOP_SWITCH_MARKER = "FIXED_HYBRID_NOOP"

#: Modes a transfer can reside in. "saw" is the Phase 2 placeholder; it is
#: reported if it appears, but it is not a system under evaluation.
RESIDENCE_MODES = ("gbn", "sr", "saw")


def read_events(path: Path | str) -> list[dict[str, str]]:
    """Read one ``events.csv``. Accepts the file or the directory holding it."""
    path = Path(path)
    if path.is_dir():
        path = path / "events.csv"
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _number(row: dict[str, str], column: str) -> float | None:
    value = row.get(column, "")
    return float(value) if value not in ("", None) else None


def _rows(events: Iterable[dict[str, str]], name: str) -> list[dict[str, str]]:
    return [row for row in events if row["event"] == name]


def _endpoint_rows(events: Iterable[dict[str, str]], endpoint: str) -> list[dict[str, str]]:
    return [row for row in events if row.get("endpoint") == endpoint]


# ---------------------------------------------------------------------------
# Individual metrics
# ---------------------------------------------------------------------------


def retransmission_count(events: Sequence[dict[str, str]]) -> int:
    """specs.md §20: total DATA retransmission events — a count of RETX rows.

    SEND and RETX are distinct events precisely so this is a count and not a
    reconstruction (design.md §9, TO-05).
    """
    return len(_rows(events, "RETX"))


def retransmission_overhead(events: Sequence[dict[str, str]]) -> float | None:
    """Retransmitted bytes over total transmitted bytes.

    Both sums come from the ``bytes`` column of SEND and RETX, which is the
    datagram size as it went on the wire — headers included, because that is
    what "transmitted" means on a network.
    """
    sent = sum(_number(row, "bytes") or 0.0 for row in _rows(events, "SEND"))
    resent = sum(_number(row, "bytes") or 0.0 for row in _rows(events, "RETX"))
    total = sent + resent
    return (resent / total) if total else None


def delivered_bytes(events: Sequence[dict[str, str]]) -> int | None:
    """Application bytes written to the output file, from the receiver's DELIVER rows.

    The receiver's writer refuses a duplicate, and only a segment it actually
    wrote produces a DELIVER — so this cannot double count a retransmission
    (T2.3, SEQ-05).
    """
    rows = _rows(events, "DELIVER")
    if not rows:
        return None
    return int(sum(_number(row, "bytes") or 0.0 for row in rows))


def integrity_success(events: Sequence[dict[str, str]]) -> bool | None:
    """Whether source and received hashes matched, from the FIN_ACK verdict.

    A hash mismatch is a reported failure, never silence (CC-01), so its absence
    from the log is *not* read as success: a run with no verdict returns None.
    """
    verdicts = [row["reason"] for row in _rows(events, "FIN_ACK") if row["reason"]]
    if not verdicts:
        return None
    return any(reason.startswith("MATCH") for reason in verdicts)


def completion_time_s(events: Sequence[dict[str, str]]) -> float | None:
    """Time from the start of the log to verified completion.

    The end is the FIN_ACK that carried the verdict; a run that never got one
    ends at its last event, which is the honest reading of when it stopped.
    """
    if not events:
        return None
    verdicts = _rows(events, "FIN_ACK")
    last = verdicts[0] if verdicts else events[-1]
    return _number(last, "timestamp")


def goodput_bytes_per_s(events: Sequence[dict[str, str]],
                        elapsed_s: float | None = None) -> float | None:
    """Delivered application bytes per second (specs.md §20).

    A transfer whose integrity check failed has no meaningful throughput, so it
    is reported as None rather than averaged in later (design.md §10).
    """
    delivered = delivered_bytes(events)
    elapsed = elapsed_s if elapsed_s is not None else completion_time_s(events)
    if delivered is None or not elapsed:
        return None
    if integrity_success(events) is False:
        return None
    return delivered / elapsed


def switch_count(events: Sequence[dict[str, str]]) -> int:
    """GBN<->SR transitions: SWITCH rows, excluding the fixed-hybrid no-ops."""
    return len([row for row in _rows(events, "SWITCH")
                if NOOP_SWITCH_MARKER not in row["reason"]])


def handshake_count(events: Sequence[dict[str, str]]) -> int:
    """Completed MODE exchanges, whether or not they changed the mode."""
    return len(_rows(events, "SWITCH"))


def mode_residence(events: Sequence[dict[str, str]]) -> dict[str, float]:
    """Seconds spent in each mode, from the ``mode`` column over time.

    Every row carries the mode that was live when it was written, so residence
    is the time between the first and last row of each contiguous run of a mode
    — no separate bookkeeping, and nothing that exists only in memory.
    """
    timed = [(_number(row, "timestamp"), row["mode"]) for row in events if row["mode"]]
    timed = [(moment, mode) for moment, mode in timed if moment is not None]
    residence: dict[str, float] = {}
    for (start, mode), (end, _) in zip(timed, timed[1:]):
        residence[mode] = residence.get(mode, 0.0) + max(0.0, end - start)
    return residence


def rtt_samples_ms(events: Sequence[dict[str, str]]) -> list[float]:
    """Per-segment round trips, in milliseconds, from SEND and ACK rows.

    Karn's rule (design.md §5.4): a segment that was retransmitted has an
    ambiguous ACK and is excluded. Only an ACK naming the segment exactly is
    used, which under SR means every segment and under GBN means the subset
    whose cumulative ACK happens to name them — fewer samples, each still
    honest. That is why §20 calls these statistics optional.
    """
    sent_at: dict[str, float] = {}
    for row in _rows(events, "SEND"):
        moment = _number(row, "timestamp")
        if moment is not None:
            sent_at.setdefault(row["sequence"], moment)
    for row in _rows(events, "RETX"):
        sent_at.pop(row["sequence"], None)      # ambiguous; excluded

    samples = []
    seen: set[str] = set()
    for row in _rows(events, "ACK"):
        key = row["ack"]
        if key in sent_at and key not in seen:
            moment = _number(row, "timestamp")
            if moment is not None:
                samples.append((moment - sent_at[key]) * 1000.0)
                seen.add(key)
    return samples


def rtt_statistics(events: Sequence[dict[str, str]]) -> dict[str, float | None]:
    """Mean, median and tail RTT (specs.md §20, optional statistics)."""
    samples = rtt_samples_ms(events)
    if not samples:
        return {"rtt_samples": 0, "rtt_mean_ms": None, "rtt_median_ms": None,
                "rtt_p95_ms": None, "rtt_max_ms": None}
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "rtt_samples": len(samples),
        "rtt_mean_ms": statistics.fmean(samples),
        "rtt_median_ms": statistics.median(samples),
        "rtt_p95_ms": ordered[index],
        "rtt_max_ms": ordered[-1],
    }


def delivery_latency_s(events: Sequence[dict[str, str]]) -> dict[str, float | None]:
    """Latency "where defined" (specs.md §20): time to first and last delivery.

    Measured inside the receiver's log, from its first event to the DELIVER of
    the first and last segment. Per-segment sender-to-receiver latency is *not*
    reported: the two logs have different origins, and subtracting across them
    would produce a number that looks precise and means nothing.
    """
    delivered = _rows(events, "DELIVER")
    if not delivered:
        return {"first_delivery_s": None, "last_delivery_s": None}
    return {
        "first_delivery_s": _number(delivered[0], "timestamp"),
        "last_delivery_s": _number(delivered[-1], "timestamp"),
    }


# ---------------------------------------------------------------------------
# The whole of specs.md §20
# ---------------------------------------------------------------------------


def derive_metrics(events: Sequence[dict[str, str]]) -> dict[str, Any]:
    """Every specs.md §20 metric this log can answer, and None for the rest.

    Rows from both endpoints may be passed together; each metric is taken from
    the endpoint that actually observed it.
    """
    sender = _endpoint_rows(events, "sender") or list(events)
    receiver = _endpoint_rows(events, "receiver") or list(events)

    elapsed = completion_time_s(sender)
    residence = mode_residence(sender)
    metrics: dict[str, Any] = {
        "completion_time_s": elapsed,
        "goodput_bytes_per_s": goodput_bytes_per_s(receiver, elapsed),
        "delivered_bytes": delivered_bytes(receiver),
        "retransmission_count": retransmission_count(sender),
        "retransmission_overhead": retransmission_overhead(sender),
        "unique_data_packets": len(_rows(sender, "SEND")),
        "total_data_transmissions": len(_rows(sender, "SEND")) + len(_rows(sender, "RETX")),
        "bytes_transmitted": int(sum(
            (_number(row, "bytes") or 0.0)
            for row in _rows(sender, "SEND") + _rows(sender, "RETX"))),
        "integrity_success": integrity_success(events),
        "switch_count": switch_count(sender),
        "handshake_count": handshake_count(sender),
        "gbn_residence_s": residence.get("gbn", 0.0),
        "sr_residence_s": residence.get("sr", 0.0),
    }
    metrics.update(rtt_statistics(sender))
    metrics.update(delivery_latency_s(receiver))
    return metrics


def derive_from_run(run_directory: Path | str) -> dict[str, Any]:
    """Derive the metrics for one run from ``logs/<run_id>/``, both endpoints.

    Opens the logs read-only and leaves them exactly as they were (RP-07).
    """
    run_directory = Path(run_directory)
    events: list[dict[str, str]] = []
    for endpoint in ("sender", "receiver"):
        path = run_directory / endpoint / "events.csv"
        if path.exists():
            events.extend(read_events(path))
    if not events:
        raise FileNotFoundError(f"no events.csv under {run_directory}")
    return derive_metrics(events)
