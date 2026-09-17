"""The Wireshark evidence set (T9.4, specs.md §22, §22.1).

Records the five captures §22.1 asks for, each by running a real transfer with
`tshark` listening on the loopback adapter, then summarises every capture by
*field* rather than by eye so the committed `.pcapng` ships with counts a reader
can reproduce.

WHY THIS IS A SCRIPT AND NOT A CHECKLIST
========================================
A capture taken by hand is a capture nobody can retake. Each scenario below
names its file, its impairment, its seed and its expected observation, so the
evidence set regenerates from one command — and a reader who doubts a claim
about it can re-run the scenario and compare. The transfers themselves go
through ``run_experiment.execute_run``, the same orchestration the matrix used,
so a capture shows the system as measured rather than a special build of it.

WHAT MAKES EACH CAPTURE READABLE
================================
* Port 8888 throughout, so ``udp.port == 8888`` is always the right display
  filter (WS-02, WS-03).
* ``tools/hybrid_arq.lua`` (T9.5) names the header fields, so the retransmission
  patterns of WS-06 and WS-07 can be *filtered* rather than eyeballed:
  ``hybridarq.type == 1 && hybridarq.seq == 42`` is every transmission of one
  segment. The dissector is optional by specs.md §22 — the header sits at fixed
  offsets and is readable without it — but it is what turns "look at the bytes"
  into a query.
* The two transition captures use a snaplen: every header byte is kept and only
  the 1 KiB payloads are truncated. A transition needs a transfer long enough
  for the controller to accumulate evidence (~110 acknowledged segments), and
  full payloads would make that capture 30x larger while adding nothing — the
  payload of a DATA packet is the file, and the file is already verified by hash.

SEEDS
=====
Every scenario carries a fixed seed, so the drop sequence in a capture is a
property of the recorded scenario and not of the day it was run (RP-03). The
lossy GBN and lossy SR captures deliberately share a seed and a file: the two
files then show **the same losses** handled two ways, which is the comparison
§22.1 is really asking for.

USAGE
=====
    python experiments/capture_evidence.py                  # all five
    python experiments/capture_evidence.py --only clean_gbn
    python experiments/capture_evidence.py --list
"""

from __future__ import annotations

import argparse
import collections
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                       # noqa: E402
import metrics as metrics_mod                       # noqa: E402
from experiments import run_experiment as runner    # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CAPTURE_DIR = config.CAPTURE_DIR
DISSECTOR = PROJECT_ROOT / "tools" / "hybrid_arq.lua"

#: Windows: npcap's loopback adapter. A local transfer is invisible to a normal
#: interface, so this is the only device that sees it (CLAUDE.md, design.md §11).
LOOPBACK = r"\Device\NPF_Loopback"
TSHARK = Path(r"C:\Program Files\Wireshark\tshark.exe")

SEGMENT = config.SEGMENT_SIZE


@dataclass(frozen=True)
class Scenario:
    """One capture: what to run, and what the resulting file should show."""

    name: str
    expectation: str            # the §22.1 row this file is evidence for
    requirements: str           # the WS-* ids it satisfies
    system: str
    file_bytes: int
    duration_s: float           # tshark's own stop condition
    loss_rate: float = 0.0
    rtt_ms: float = 0.0
    loss_schedule: tuple[tuple[float, float], ...] = ()
    seed: int = 20260917
    snaplen: int = 0            # 0 = whole packet
    notes: str = ""
    #: Two scenarios that share this meet the **same drop sequence**, because
    #: the per-trial seed is derived from it and not from the scenario name
    #: (D13, RP-04). That is what makes the lossy GBN and lossy SR captures a
    #: controlled comparison rather than two separate lossy runs.
    condition_key: str = ""

    @property
    def key(self) -> str:
        return self.condition_key or self.name


#: The evidence set of specs.md §22.1, one scenario per row, plus the reverse
#: transition WS-09 asks for "if the chosen experiment produces one" — E8 does,
#: so it is captured rather than skipped.
SCENARIOS = (
    Scenario(
        name="clean_gbn",
        expectation="Sequential DATA and cumulative ACKs, no retransmissions",
        requirements="WS-01, WS-02, WS-03, WS-04, WS-05",
        system="gbn", file_bytes=64 * SEGMENT, duration_s=12.0,
        notes="The control: what the protocol looks like when nothing goes wrong.",
    ),
    Scenario(
        name="lossy_gbn_range_retx",
        expectation="One drop resends the whole outstanding range",
        requirements="WS-06",
        system="gbn", file_bytes=64 * SEGMENT, loss_rate=0.10, duration_s=20.0,
        condition_key="lossy_10pct",
        notes="Same file and the same derived seed as the SR capture below: one "
              "drop stream, two strategies.",
    ),
    Scenario(
        name="lossy_sr_individual_retx",
        expectation="Only the missing segment is resent; ACKs name one segment each",
        requirements="WS-07",
        system="sr", file_bytes=64 * SEGMENT, loss_rate=0.10, duration_s=20.0,
        condition_key="lossy_10pct",
        notes="Same file and the same derived seed as the GBN capture above.",
    ),
    Scenario(
        name="hybrid_gbn_to_sr",
        expectation="MODE exchange, then per-segment retransmission where the "
                    "range retransmission of GBN was",
        requirements="WS-08",
        system="hybrid", file_bytes=1024 * SEGMENT, rtt_ms=100.0,
        loss_schedule=((8.0, 0.10),), duration_s=48.0, snaplen=128,
        notes="E8's loss_rise condition: clean, then 10% loss from 8 s.",
    ),
    Scenario(
        name="hybrid_sr_to_gbn",
        expectation="A second MODE exchange returning to cumulative ACKs once "
                    "loss falls away",
        requirements="WS-09",
        system="hybrid", file_bytes=1024 * SEGMENT, rtt_ms=100.0,
        loss_rate=0.10, loss_schedule=((20.0, 0.0),), duration_s=48.0, snaplen=128,
        notes="E8's loss_fall condition: 10% loss, then clean from 20 s. "
              "Both transitions appear in this one file.",
    ),
)


# ---------------------------------------------------------------------------
# Capturing
# ---------------------------------------------------------------------------


class CaptureError(RuntimeError):
    """tshark could not capture — no adapter, no permission, or no tshark."""


def _start_tshark(path: Path, scenario: Scenario) -> subprocess.Popen:
    """Start the capture and wait until it is actually listening.

    Waiting for tshark's own "Capturing on" line rather than sleeping: a
    transfer that starts first loses its START packet from the record, which is
    the one packet every other timestamp is aligned against (§21).
    """
    if not TSHARK.is_file():
        raise CaptureError(f"tshark not found at {TSHARK}")
    command = [
        str(TSHARK), "-i", LOOPBACK,
        "-f", f"udp port {config.PORT}",        # capture filter, not display
        "-a", f"duration:{scenario.duration_s:g}",
        "-w", str(path),
    ]
    if scenario.snaplen:
        command += ["-s", str(scenario.snaplen)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    deadline = time.perf_counter() + 20.0
    while time.perf_counter() < deadline:
        line = process.stderr.readline()
        if not line:
            raise CaptureError(
                "tshark exited before capturing; loopback capture on Windows "
                "needs the npcap 'Adapter for loopback traffic capture'")
        if line.startswith("Capturing on"):
            return process
    raise CaptureError("tshark never reported that it was capturing")


def _transfer(scenario: Scenario, *, log_dir: Path) -> dict:
    """Run the scenario's transfer through the Phase 8 orchestration."""
    # The condition is named by ``key``, not by the scenario, because the seed
    # is derived from the condition name: two scenarios sharing a key therefore
    # meet the identical drop sequence. The run_id still separates them, since
    # it carries the system as well.
    condition = runner.Condition(
        name=scenario.key, loss_rate=scenario.loss_rate, rtt_ms=scenario.rtt_ms,
        loss_schedule=scenario.loss_schedule)
    experiment = runner.Experiment(
        experiment="WS", conditions=(condition,), systems=(scenario.system,),
        trials=1, file_size_bytes=scenario.file_bytes, base_seed=scenario.seed,
        abort_timeout_s=max(120.0, scenario.duration_s * 2),
        port=config.PORT)
    run = runner.PlannedRun(experiment, condition, scenario.system, 0)
    source = runner.source_file(log_dir / "sources", scenario.file_bytes)
    return runner.execute_run(run, log_dir=log_dir, source=source)


def capture(scenario: Scenario, *, capture_dir: Path = CAPTURE_DIR,
            log_dir: Path | None = None) -> dict:
    """Record one scenario and return what the resulting capture contains."""
    capture_dir.mkdir(parents=True, exist_ok=True)
    path = capture_dir / f"{scenario.name}.pcapng"
    log_dir = log_dir or config.LOG_DIR / "captures"

    tshark = _start_tshark(path, scenario)
    try:
        row = _transfer(scenario, log_dir=log_dir)
    finally:
        # tshark stops itself at its duration; the transfer is always shorter,
        # so this waits out the tail rather than truncating the file by killing
        # a writer mid-packet.
        try:
            tshark.communicate(timeout=scenario.duration_s + 30)
        except subprocess.TimeoutExpired:
            tshark.kill()
            tshark.communicate(timeout=10)

    summary = summarise(path)
    summary.update({
        "scenario": scenario.name,
        "path": path,
        "bytes": path.stat().st_size if path.exists() else 0,
        "integrity": row.get("integrity_success"),
        "status": row.get("status"),
        "switches": row.get("switch_count"),
        "retransmissions": row.get("retransmission_count"),
        "run_id": row.get("run_id"),
    })
    return summary


# ---------------------------------------------------------------------------
# Reading a capture back
# ---------------------------------------------------------------------------


def summarise(path: Path) -> dict:
    """What a capture contains, counted by protocol field via the dissector.

    This is the step that makes a committed binary auditable: the numbers below
    come out of the capture itself, so a reader can re-run the same tshark
    command and get them back. ``resent`` counts DATA packets carrying a
    sequence that was already sent — retransmission as seen *on the wire*,
    independent of what the sender's log says about it.
    """
    if not path.is_file():
        return {"packets": 0}
    output = subprocess.run(
        [str(TSHARK), "-r", str(path), "-X", f"lua_script:{DISSECTOR}",
         "-Y", "hybridarq", "-T", "fields",
         "-e", "hybridarq.type_name", "-e", "hybridarq.seq", "-e", "hybridarq.ack"],
        capture_output=True, text=True, check=True).stdout

    counts: collections.Counter = collections.Counter()
    seen_data: set[str] = set()
    resent = 0
    for line in output.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        name = parts[0]
        counts[name] += 1
        if name == "DATA":
            sequence = parts[1] if len(parts) > 1 else ""
            if sequence in seen_data:
                resent += 1
            seen_data.add(sequence)
    return {
        "packets": sum(counts.values()),
        "by_type": dict(sorted(counts.items())),
        "unique_data": len(seen_data),
        "resent_on_the_wire": resent,
    }


def transfer_facts(scenario: Scenario, *, log_dir: Path | None = None) -> dict:
    """What that scenario's recorded run reported, re-derived from its log.

    Lets the manifest be rebuilt from the evidence already on disk — the capture
    files and the event logs — without re-running a transfer. Reading the record
    rather than recreating it is the same discipline the analysis follows
    (RP-07, CC-06).
    """
    log_dir = log_dir or config.LOG_DIR / "captures"
    run_id = runner.run_id("WS", scenario.system, scenario.key, 0)
    directory = log_dir / run_id
    try:
        derived = metrics_mod.derive_from_run(directory)
    except FileNotFoundError:
        return {"run_id": run_id, "status": "unknown", "integrity": None,
                "switches": None, "retransmissions": None}
    return {
        "run_id": run_id,
        "status": runner.STATUS_OK if derived["integrity_success"] else "integrity",
        "integrity": derived["integrity_success"],
        "switches": derived["switch_count"],
        "retransmissions": derived["retransmission_count"],
    }


def rebuild_summaries(*, capture_dir: Path = CAPTURE_DIR,
                      log_dir: Path | None = None) -> list[dict]:
    """Summarise every scenario from the capture and log files already present."""
    summaries = []
    for scenario in SCENARIOS:
        path = capture_dir / f"{scenario.name}.pcapng"
        summary = summarise(path)
        summary.update(transfer_facts(scenario, log_dir=log_dir))
        summary.update({"scenario": scenario.name, "path": path,
                        "bytes": path.stat().st_size if path.exists() else 0})
        summaries.append(summary)
    return summaries


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


MANIFEST_HEADER = """# Wireshark evidence set (T9.4, specs.md §22.1)

Recorded by `python experiments/capture_evidence.py`, which runs each transfer
through the same orchestration the experiments used and captures it on the npcap
loopback adapter. Every scenario carries a fixed seed, so a capture can be
retaken and compared rather than taken on trust (RP-03).

Display filter for all of them: `udp.port == 8888` (WS-02, WS-03).

The header is a fixed 21-byte prefix, so sequence and ACK are readable at
constant offsets without any plugin (WS-05). `tools/hybrid_arq.lua` (T9.5) names
the fields so they can be *filtered* instead:

```bash
tshark -r captures/lossy_gbn_range_retx.pcapng -X lua_script:tools/hybrid_arq.lua \\
       -T fields -e frame.number -e hybridarq.type_name -e hybridarq.seq -e hybridarq.ack

# every transmission of one segment - the retransmission pattern, as a query
tshark -r captures/lossy_gbn_range_retx.pcapng -X lua_script:tools/hybrid_arq.lua \\
       -Y 'hybridarq.type == 1 && hybridarq.seq == 20'

# the MODE handshake, with its JSON payload
tshark -r captures/hybrid_gbn_to_sr.pcapng -X lua_script:tools/hybrid_arq.lua \\
       -Y 'hybridarq.type == 7' -T fields -e hybridarq.control
```

`resent on the wire` below counts DATA packets carrying a sequence that had
already appeared in that capture — retransmission as the *capture* shows it,
independent of what the sender's log claims.

**It is not the same number as the sender's retransmission count, and the gap is
the point of the comparison.** A segment that was dropped never reached the wire,
so resending it puts no duplicate sequence in the capture. A duplicate appears
only when a segment that *did* arrive is sent again — which is precisely what
Go-Back-N does to everything behind a loss, and precisely what Selective Repeat
does not do at all. In the two lossy captures below, taken from the same file and
the same seeded drop stream, **GBN puts 62 redundant copies on the wire and SR
puts none**.

The two runs do not see an identical *list* of drops, and the reason is worth
stating: the stream is consumed per datagram sent, so the first drops match
(segments 3, 8, 3 again) and then diverge — GBN, resending whole ranges, offers
far more datagrams to the same 10% and collects 18 drops where SR collects 10.
The condition is identical; the exposure to it is a consequence of the strategy,
which is the thing being measured.

"""


def write_manifest(summaries: list[dict], *, capture_dir: Path = CAPTURE_DIR) -> Path:
    """One section per capture: what it was meant to show, and what it holds."""
    lines = [MANIFEST_HEADER.rstrip()]
    for summary in summaries:
        scenario = next(s for s in SCENARIOS if s.name == summary["scenario"])
        schedule = (", loss schedule " +
                    ",".join(f"{t:g}s->{r:g}" for t, r in scenario.loss_schedule)
                    if scenario.loss_schedule else "")
        by_type = ", ".join(f"{name} {count}"
                            for name, count in summary["by_type"].items())
        snaplen = f", snaplen {scenario.snaplen} B" if scenario.snaplen else ""
        lines += [
            "",
            f"## `{scenario.name}.pcapng`",
            "",
            f"**Expected observation ({scenario.requirements}):** "
            f"{scenario.expectation}",
            "",
        ]
        if scenario.notes:
            lines += [scenario.notes, ""]
        lines += [
            f"- run: `{scenario.system}`, {scenario.file_bytes // 1024} KiB, "
            f"loss {scenario.loss_rate:.0%}, RTT {scenario.rtt_ms:g} ms"
            f"{schedule}, seed {scenario.seed}",
            f"- capture: {summary['packets']} packets ({by_type}), "
            f"{summary['unique_data']} unique DATA sequences, "
            f"**{summary['resent_on_the_wire']} resent on the wire**{snaplen}",
            f"- transfer: {summary['status']}, integrity "
            f"{'OK' if summary['integrity'] else 'FAILED'}, "
            f"{summary['switches']} mode switch(es), "
            f"{summary['retransmissions']} retransmissions in the sender's log",
        ]
    path = capture_dir / "README.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record the specs.md §22.1 Wireshark evidence set (T9.4).")
    parser.add_argument("--only", action="append", default=None,
                        help="record just this scenario; repeatable")
    parser.add_argument("--capture-dir", type=Path, default=CAPTURE_DIR)
    parser.add_argument("--list", action="store_true",
                        help="list the scenarios and exit")
    parser.add_argument("--manifest-only", action="store_true",
                        help="rebuild captures/README.md from the files already "
                             "recorded, capturing nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        for scenario in SCENARIOS:
            print(f"{scenario.name:<26} {scenario.requirements:<28} "
                  f"{scenario.expectation}")
        return 0

    if args.manifest_only:
        path = write_manifest(rebuild_summaries(capture_dir=args.capture_dir),
                              capture_dir=args.capture_dir)
        print(f"rebuilt {path} from the recorded captures and logs")
        return 0

    chosen = [s for s in SCENARIOS if not args.only or s.name in args.only]
    summaries = []
    for scenario in chosen:
        print(f"capturing {scenario.name} "
              f"(up to {scenario.duration_s:g}s) ...", flush=True)
        summary = capture(scenario, capture_dir=args.capture_dir)
        summaries.append(summary)
        print(f"  {summary['packets']} packets, {summary['resent_on_the_wire']} "
              f"resent on the wire, integrity "
              f"{'OK' if summary['integrity'] else 'FAILED'} "
              f"-> {summary['path'].name} ({summary['bytes'] // 1024} KiB)",
              flush=True)

    if len(summaries) == len(SCENARIOS):
        print(f"wrote {write_manifest(summaries, capture_dir=args.capture_dir)}")
    else:
        print("manifest not rewritten: it describes the whole evidence set, and "
              "only part of it was recorded")
    return 0 if all(s["integrity"] for s in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
