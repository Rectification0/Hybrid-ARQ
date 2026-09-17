# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A UDP file-transfer protocol implementing both Go-Back-N and Selective Repeat ARQ, plus a
hybrid controller that switches between them at runtime based on observed loss — and an
experimental evaluation of whether that switching actually beats either fixed strategy.
It is an implementation-and-evaluation project; it claims no global novelty (`specs.md` §30).

The deliverable is as much the *recorded evidence* (event logs, graphs, Wireshark captures)
as the code. That shapes most of the rules below.

## Commands

```bash
python -m pytest -q                                      # full suite
python -m pytest test/test_packet.py::test_name -q       # one test
python -m pytest -q -k checksum                          # by keyword
```

`pytest.ini` pins `testpaths = test`, so a bare `pytest` collects only `test/`.
`smoke_udp.py` is a runnable script, not a test module — it was renamed from `test_udp.py`
precisely because pytest imported it during collection.

Environment / wire-format smoke test, two terminals:

```bash
python test/spike_receiver.py      # terminal 1 (M1 spike listener, NOT the real receiver)
python smoke_udp.py                # terminal 2
```

Wireshark filter `udp.port == 8888`. Loopback capture on Windows needs the npcap
"Adapter for loopback traffic capture":

```bash
"/c/Program Files/Wireshark/tshark" -i '\Device\NPF_Loopback' -f "udp port 8888" \
    -a duration:12 -w captures/name.pcapng
"/c/Program Files/Wireshark/tshark" -r captures/name.pcapng -T fields -e data.data
```

Dashboard (Phase 11), one command and no build step:

```bash
python -m frontend                 # http://127.0.0.1:8080
python -m frontend --port 9000 --no-browser
```

Analysis dependencies (`pip install -r requirements.txt`) are only for `experiments/`; the
protocol **and the dashboard** are standard library only. There is no npm, no bundler and no
generated asset — `frontend/static/` is served exactly as written.

## The document hierarchy drives the work

This repo is spec-driven, and the four documents are not interchangeable:

| Document | Role |
| --- | --- |
| `Hybrid_GBN_SR_..._Specification.docx` | Source of truth. Everything else derives from it. |
| `specs.md` | Requirements with stable IDs (FR-\*, GBN-\*, SR-\*, HY-\*, IN-\*, SEQ-\*, TO-\*, RP-\*, CC-\*, WS-\*, H-\*) |
| `design.md` | How it is realized: module boundaries, state machines, decisions D1–D14 |
| `tasks.md` | Task IDs T0.1 … T10.7, each with requirement IDs and an explicit **Done when** |

**Work is driven by `tasks.md`, phase by phase.** Task IDs are stable — cite them in commits.
Each task's "Done when" is the acceptance criterion; treat it as checkable, not as a
judgement call. Update the checkbox, the Status snapshot table, and the milestone map when
a phase completes.

Code comments and docstrings cite these IDs (`FR-04`, `IN-02`, `D3`, `T2.2`) throughout.
Keep doing that — it is how the code stays traceable to the requirement it satisfies.

## The freeze discipline

`design.md` §12 lists 14 decisions (D1–D14). A decision is frozen only when all three are
true: the value is in the code, `specs.md` §16 records it **with its rationale**, and
`design.md` §12 says **Frozen** instead of Proposed.

**All fourteen are frozen.** D1 (byte layout), D2 (checksum coverage), D3 (`SEGMENT_SIZE`),
D4 (sequence convention), D5 (GBN ACK = highest in-order received), D6 (SR ACK = exactly the
segment named), D7 (RTO = max(4xRTT, 200 ms) per condition), D8 (loss estimator: 50 segment
outcomes, recorded at ACK), D9 (thresholds 0.10/0.02, hysteresis 3), D10 (MODE handshake at a
quiescent window), D11 (window 8), and the experiment-scale trio D12 (5 trials per cell),
D13 (seed policy) and D14 (1 MiB for the whole matrix) — the last three settled at T8.1
against a measured pilot rather than against the recommendation they had carried since
`design.md` was written. `config._OPEN_DECISIONS` is empty and no `# NOT FROZEN` marker
remains on a value; the phrase survives only in the module docstring that explains the
convention.

Nothing here is open, so a change to any of these is an *unfreeze*: it needs a recorded
reason and it invalidates whatever evidence depended on the old value.

Record the rationale at freeze time, not retroactively at T10.2 — reconstructing it later
from memory is how a frozen value becomes an unexplained one.

Two freezes are experiment-invalidating and must not be touched casually: **D8** (loss
estimator, T5.2) and **D9** (thresholds and hysteresis, T7.2). Changing either means
re-running every experiment — and for D9 that now includes re-running the 444-transfer
calibration sweep, since `EVALUATION_INTERVAL_SEGMENTS` and `MIN_MODE_RESIDENCE_S` were held
fixed throughout it and are frozen by the same evidence. `test/test_calibration.py` asserts
`config.py` still matches what the sweep chose, so the frozen value cannot drift from the
evidence.

**The thresholds are readings of the estimator, not loss rates.** D8 over-reads loss under
GBN by 4–5×, so `SWITCH_HIGH = 0.10` fires at roughly 2% physical loss. Never describe it as
"switch at 10% loss". `experiments/results/calibration.md` §5 lists every condition under
which the hybrid is *worse* than a fixed strategy; that section is the honest core of the
evaluation, not an appendix.

The wire format is likewise load-bearing: changing MAGIC, the struct layout or the
`PacketType` numbering invalidates every capture and log already recorded.

## Architecture

### Layering rule (`design.md` §1.1)

Each layer is forbidden from knowing about the one above it:

| Layer | Knows about | Must not know about |
| --- | --- | --- |
| `protocol/packet.py` | bytes, checksum, field packing | windows, modes, timers |
| `protocol/gbn.py`, `sr.py` | windows, timers, ACK rules | which mode is "better", thresholds |
| `protocol/hybrid.py` | statistics, thresholds, hysteresis | byte layout, file I/O |
| `sender.py`, `receiver.py` | lifecycle, file I/O, CLI, logging | internal ARQ bookkeeping |
| `frontend/` | recorded artifacts, HTTP, subprocess lifecycle | everything below it — and nothing below it may import *it* |

The consequence is structural: GBN and SR implement one common **strategy interface**
(`design.md` §4), and the hybrid controller only ever swaps which implementation is active.
It never reaches inside one.

### Build order — non-negotiable

From the source specification §41: **both ARQ baselines must be correct before the hybrid
controller is written.** Do not start Phase 5 with Phase 3 or 4 failing. The controller must
never be debugged at the same time as the mechanism underneath it.

Phase 4 deliberately lands the loss simulator *before* SR, so SR has a loss-capable test bed
from its first line.

### Mode switching is the correctness-critical part

GBN and SR disagree about what an ACK *means* (D5 vs D6), so a switch happens only at a
quiescent boundary: the sender drains its window (`send_base == next_seq`), exchanges MODE
with an `epoch` and `effective_from_seq`, then both sides rebuild strategies from exported
transfer state. Segment payloads live in mode-independent transfer state, *not* inside the
strategy object — that is what makes "no unacked segment is lost across a switch" true by
construction. A handshake that exceeds the retry budget abandons the switch and continues in
the current mode; a half-switched transfer is never acceptable. See `design.md` §6.3.

### Configuration

Every tunable lives in `config.py`; no protocol module may hard-code one. The exception is
deliberate: `HEADER_SIZE` and `MAX_DATAGRAM_SIZE` live in `protocol/packet.py` because they
are *derived* from the frozen struct format rather than tunable — duplicating the literal 21
across two files is how a wire format drifts.

`baseline_rto(rtt_ms)` implements D7: RTO is derived per experimental condition and then held
fixed for the whole run. A single absolute constant cannot serve both the 10 ms and 500 ms
RTT conditions of E7 while keeping the three systems comparable within each cell.

### Logging is the product

Every metric in `specs.md` §20 must be computable from `events.csv` alone — nothing
in-memory-only and printed (CC-06). `metrics.py` is that rule made checkable: it derives §20
from event rows and nothing else, and T6.2 asserts it agrees with the endpoints' own
counters. If a metric ever became in-memory-only, the derivation would have nothing to read.
Logs are append-only and never edited; aggregation reads them and does not modify them
(RP-07). `SEND` and `RETX` are distinct events. The `bytes` column carries the datagram size
on `SEND`/`RETX` and the payload written on `DELIVER` — without it, retransmission overhead
and goodput are not derivable at all.

Timestamps come from **`time.perf_counter`, never `time.monotonic`** — on Windows the latter
is `GetTickCount64` at 15.6 ms resolution, which quantises every RTT sample and residence
time to a tick and makes E7's 10 ms RTT cell unmeasurable. A test enforces that no endpoint,
the emitter or the simulator calls `time.monotonic()`; mixing the two would also compare
timestamps from different origins. Each log's origin is **that endpoint's** start — the
sender's when its log opened, the receiver's when it bound — so interleave by aligning on the
shared `START` row, and never subtract one endpoint's timestamp from the other's. The receiver holds events seen before START (its log cannot open
until START names the run) and replays them onto its timeline; a receiver that never hears
from anyone still writes `events.csv` and `summary.json`.

`summary.json` records the configuration the run *actually* used, not the module defaults —
`config.snapshot(overrides)` takes the seed, impairment, derived RTO, file size and any
swept thresholds, and rejects an unknown key rather than silently recording a typo.

Integrity failures must stay distinguishable from simulated loss (IN-04): that is why
`decode()` raises a specific `PacketError` subclass per failure mode, and why `DROP` and
`CHECKSUM_FAIL` are separate events. A hash mismatch is always a reported failure, never
silent corruption and never presented as success (CC-01).

## Repo-specific gotchas

- **`conftest.py` at the root is load-bearing.** Pytest prepends the directory containing the
  rootmost `conftest.py` to `sys.path`, which is what lets `protocol/packet.py` do
  `import config` under test the same way it does when run from the project root.
- **`ConnectionResetError` on a UDP socket is normal on Windows** and must never be treated
  as fatal. Sending to a closed port provokes an ICMP port-unreachable that surfaces on the
  next `recvfrom` as WinError 10054. There is no way to suppress it from Python
  (`SIO_UDP_CONNRESET` is not exposed and `sock.ioctl` rejects the raw code), so every
  receive loop handles it by continuing to wait. See `network/udp.py`.
- **`--mode saw` is a placeholder, not a system under evaluation.** The baselines being
  measured are `gbn` and `sr` (`specs.md` §18). Stop-and-wait exists because Phase 2 needed
  to move data before either real mode was written.
- **Impairment is a wrapper, never a branch in protocol code.** `Impairment.wrap()`
  returns the *raw socket unchanged* for a no-op condition, deliberately: the clean
  baseline must not traverse code the impaired runs skip, or the comparison measures the
  wrapper too. Each direction draws from its own seeded RNG so the forward drop sequence
  is a pure function of the seed, independent of ACK timing (RP-03).
- **Test corruption and loss must not be phase-locked to retransmissions.** A fixed
  every-Nth corruption counter lines up with GBN's deterministic retransmission pattern,
  re-corrupting the same segment on every attempt until the retry budget dies. That is a
  test artifact, not a protocol bug — use a seeded probability instead.
- **A strategy owns its timers but never logs.** Transitions are queued via `TimerTracker`
  and drained by the endpoint, which keeps logging on the endpoint side of the layering
  rule while still satisfying TO-02. `next_timeout()` exists so the send loop sizes its
  socket wait to the nearest timer — blocking a full RTO regardless would make a
  retransmission up to two RTOs late and inflate completion times under loss.
- **The M1 spike listener** lives at `test/spike_receiver.py`; nothing should import it.
  The real receiver is `receiver.py`.
- **No placeholder modules remain.** The `NOT YET IMPLEMENTED — scaffold only` convention
  served Phases 0–9 and no file carries it now; every module under `protocol/`, `network/`,
  `experiments/` and `frontend/` is implemented. Reintroduce the marker, naming the task that
  will implement it, if a new scaffold is ever added.
- `logs/`, `plots/` and `captures/` are git-ignored; curated Wireshark evidence is committed
  deliberately with `git add -f`.

## Current state

**All phases complete (M1–M12).** 667 tests pass. The protocol, both ARQ baselines and the
hybrid controller are implemented and tested; the D9 thresholds are calibrated and frozen
against a recorded sweep; the §19 matrix has been run, aggregated, graphed and written up;
the Wireshark evidence set and the §28 demonstration are recorded; and a browser dashboard
sits over the finished system without changing any of it.

The findings are in `RESULTS.md` — hypotheses H-01…H-05, limitations, positioning. The
headline: at 20% loss the hybrid completes in half GBN's time, and at 1–2% loss it
oscillates and loses to both fixed strategies. Both belong in any summary of the result.

### What each layer is for, once it exists

The controller lives in `protocol/hybrid.py` and only ever *decides*: the MODE handshake
itself is endpoint lifecycle, in `sender.py` / `receiver.py`. `SWITCH` means the mode
changed; the `MODE` event (added in T5.4) means the handshake did something — including
being refused, repeated or abandoned. A `SWITCH` row whose reason is `FIXED_HYBRID_NOOP` is
the `--mode fixed-hybrid` control paying the drain cost without changing semantics, so a
count of real transitions excludes those rows.

`design.md` §9.1 is the T6.1 event audit table, enforced by `test/test_logging.py`. That
audit found four real gaps rather than confirming the log was already sufficient — the
receiver did not log its idle timeout, a receiver that never saw a START wrote no log at
all, packets rejected before START were dropped, and overhead and goodput were not derivable
because nothing recorded a byte count. Expect an audit to find something; one that confirms
everything was already fine has probably not been performed.

### The frontend (Phase 11, M12)

`python -m frontend` serves a dashboard at `127.0.0.1:8080` over the recorded artifacts and
the existing CLI. It is a **presentation and control layer**: it adds no capability to the
protocol, and deleting `frontend/` leaves the protocol's correctness and every recorded
result unchanged — `test/test_frontend.py` demonstrates that rather than asserting it.

Standard library only, like the protocol: `http.server` on the back, hand-written ES modules
and inline SVG on the front, no build step and nothing added to `requirements.txt`.
Architecture and rationale in `design.md` §13, recorded as F1–F6 — **not** as D-numbers and
**not** in `specs.md` §16, because a framework choice cannot invalidate a transfer that
already happened. Phase 11 introduced no new requirement IDs.

There is no database, deliberately. `events.csv` and `summary.json` already are the durable
record, and copying them into a second store would give one number two sources that can
disagree — which is what CC-06 exists to prevent. `frontend/data.py` opens them read-only
and shapes them for JSON; a test asserts no frontend module ever constructs an `EventLog` or
opens a file for writing.

Three rules govern anything added to it, each enforced by a test rather than by discipline:

1. **It reads recorded data.** No second logging path.
2. **It never computes protocol behaviour.** Switch reasons, the estimate at a transition and
   the epoch are read from the recorded rows verbatim; metrics come from `metrics.py`. A test
   parses `frontend/data.py` and fails if it compares anything against `SWITCH_HIGH`,
   `SWITCH_LOW` or `HYSTERESIS_COUNT` — a threshold evaluated a second time is a second
   controller, and the two would drift.
3. **It never fabricates.** A missing run, results file or capture produces an explicit empty
   state naming the path it looked for. A value that was not recorded renders as a dash,
   never as zero, and a run with no FIN_ACK verdict is never shown as passing (CC-01).

Frozen values appear as read-only context, never as form fields. RTO is not a field at all —
D7 derives it from the condition's RTT. Runs the dashboard starts land in `logs/ui/`, via the
project's own `sender.py` / `receiver.py` command lines, which the control panel displays.

Two limits worth knowing before changing anything there: the live view trails a transfer by
up to `FLUSH_EVERY = 64` rows and **the flush policy is not to be loosened to make it
smoother** — that would trade measurement fidelity for animation; and `captures/` holds a
curated per-*scenario* evidence set, not one capture per transfer, so nothing may imply an
arbitrary run has one. The Wireshark panel points at Wireshark and states plainly that the
dashboard does not inspect packets.
