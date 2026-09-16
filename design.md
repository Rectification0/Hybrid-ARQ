# Design — Hybrid GBN/SR ARQ with Adaptive Switching

**Status:** Technical design
**Source of truth:** `Hybrid_GBN_SR_Functionality_and_Technical_Specification.docx`
**Requirements:** see `specs.md` (requirement IDs referenced throughout)

This document describes *how* the specification is realized: module boundaries, state,
algorithms, state machines, and the mechanism for switching modes safely mid-transfer.

Anything the source document leaves unfixed appears here as **DECISION** with the options
and a recommendation. Recommendations are proposals — they become binding only once
written into `config.py` / `protocol/packet.py` and recorded in `specs.md` §16.

---

## 1. Architecture

High-level data flow (per the source specification):

```
Application
    ↓
Hybrid ARQ Controller        ← observes loss/RTT statistics, selects mode
    ↓
GBN / SR Mode                ← active retransmission strategy
    ↓
UDP Socket
    ↓
Network  (optional impairment: app-level simulator, or tc/netem)
    ↓
UDP Socket
    ↓
Receiver / ARQ Handler       ← validate, ACK, buffer (SR), deliver in order
    ↓
File Reconstruction
```

Division of responsibility:

- **Sender** owns transfer sequencing and all hybrid decision logic. The switching policy
  lives only on the sender; the receiver is told what semantics to use (§6).
- **Receiver** validates, acknowledges, buffers where SR requires it, and reconstructs data.
- **Both endpoints share the packet-format definition** — one module, imported by both, so
  the wire format can never drift between the two sides.

### 1.1 Layering rule

The design is deliberately layered so that the hybrid controller is never debugged at the
same time as the underlying ARQ mechanisms (source spec §41):

| Layer | Knows about | Must not know about |
| --- | --- | --- |
| Packet layer | bytes, checksum, field packing | windows, modes, timers |
| ARQ strategy (GBN, SR) | windows, timers, ACK rules | which mode is "better", thresholds |
| Hybrid controller | statistics, thresholds, hysteresis | byte layout, file I/O |
| Endpoint (sender/receiver) | lifecycle, file I/O, CLI, logging | internal ARQ bookkeeping |

Consequence: GBN and SR implement one common **strategy interface**, and the controller
only ever swaps which implementation is active.

## 2. Components

| Component | Responsibility |
| --- | --- |
| `sender.py` | CLI, file reading, transfer lifecycle, sender orchestration |
| `receiver.py` | CLI, socket setup, receiving, reconstruction |
| `config.py` | All tunable parameters in one place (specs.md §15) |
| `protocol/packet.py` | Packet constants, serialization, parsing, checksum |
| `protocol/gbn.py` | GBN sender/receiver state and retransmission |
| `protocol/sr.py` | SR sender/receiver state, buffering, individual retransmission |
| `protocol/hybrid.py` | Network-condition monitoring and mode selection |
| `network/simulator.py` | Optional controlled loss/delay/jitter |
| `experiments/run_experiment.py` | Automated experiment execution |
| `experiments/analyze_results.py` | Result aggregation and metrics |
| Wireshark | Packet capture and visual verification |

### 2.1 Directory structure

```
Hybrid-ARQ/
├── sender.py
├── receiver.py
├── config.py
├── protocol/
│   ├── __init__.py
│   ├── packet.py
│   ├── gbn.py
│   ├── sr.py
│   └── hybrid.py
├── network/
│   ├── __init__.py
│   └── simulator.py
├── experiments/
│   ├── run_experiment.py
│   ├── analyze_results.py
│   └── configs/
├── logs/
├── captures/
├── plots/
├── test/
└── README.md
```

Note on repo state: the M1 connectivity spike (plain `TEST_PACKET_n` strings) has been
retired. Its listener moved to `test/spike_receiver.py` in Phase 1 so the name `receiver.py`
stays free for the real receiver, and its sender was renamed `smoke_udp.py` and rewritten in
T1.7 to emit real encoded packets. `smoke_udp.py` is retained only as the environment and
wire-format smoke test; it is a script, not part of the pytest suite.

### 2.2 Technology stack

| Technology | Use |
| --- | --- |
| Python 3 | Protocol and experiment implementation |
| `socket` | UDP communication |
| `struct` / `dataclasses` | Binary packet representation |
| `hashlib` | File-integrity verification |
| `logging` / `csv` | Event and experiment logging |
| pandas | Result aggregation |
| Matplotlib | Graphs |
| Scapy | Optional packet construction/inspection |
| Wireshark | Packet capture and protocol demonstration |
| Git | Version control |
| Linux `tc`/netem | Optional system-level network emulation |

## 3. Packet layer (`protocol/packet.py`)

### 3.1 Header

Fixed header, fields in the order given by specs.md §5, total **21 bytes**:

```
offset  size  field
  0      2    MAGIC
  2      1    VERSION
  3      1    TYPE
  4      1    FLAGS
  5      4    SEQUENCE
  9      4    ACK
 13      2    WINDOW
 15      2    PAYLOAD_LENGTH
 17      4    CHECKSUM
 21      -    PAYLOAD (PAYLOAD_LENGTH bytes)
```

**DECISION D1 — endianness and packing.** Recommend network byte order (big-endian) with
no implicit padding: `struct.Struct("!HBBBIIHHI")`. Explicit `!` prevents platform-dependent
alignment, which would otherwise silently break cross-machine runs.

**DECISION D2 — checksum algorithm and coverage.** Recommend CRC-32
(`zlib.crc32`) computed over *the header with the CHECKSUM field zeroed, plus the payload*.
Rationale: covers every field (so a corrupted SEQUENCE is caught, not just corrupted data),
is deterministic across Python versions, and is cheap. Coverage must be documented in the
module docstring since it cannot be inferred from the wire bytes.

**DECISION D3 — maximum payload / `SEGMENT_SIZE`.** Recommend 1024 bytes, keeping
21 + 1024 = 1045 bytes well under a 1500-byte Ethernet MTU so no IP fragmentation confuses
the Wireshark evidence (specs.md §22). PAYLOAD_LENGTH is 2 bytes, so the format permits up
to 65535; the *configured* limit is what must be frozen.

**DECISION D4 — initial sequence number.** Recommend `0` for the first DATA segment, with
sequence numbers being **segment indices**, not byte offsets. Segment indexing makes window
arithmetic, logs, and Wireshark reading directly comparable to textbook GBN/SR. Wraparound
is out of scope (SEQ-04): a 4-byte counter at 1 KiB segments covers ~4 TiB per transfer.

### 3.2 API

```python
class PacketType(IntEnum):          # wire values are frozen once chosen
    DATA = 1; ACK = 2; START = 3; START_ACK = 4
    FIN = 5; FIN_ACK = 6; MODE = 7

@dataclass(frozen=True)
class Packet:
    type: PacketType
    seq: int = 0
    ack: int = 0
    window: int = 0
    flags: int = 0
    payload: bytes = b""

def encode(pkt: Packet) -> bytes: ...
def decode(raw: bytes) -> Packet: ...   # raises PacketError on any validation failure
```

`decode` performs all validation before any caller can see a payload (FR-04, IN-02), in
this order — each failure is a discard-and-log case per specs.md §13:

1. Length ≥ header size.
2. MAGIC matches.
3. VERSION supported.
4. TYPE is a known `PacketType` value.
5. `len(raw) - HEADER == PAYLOAD_LENGTH`.
6. Checksum recomputes to the CHECKSUM field.

Distinct exception subclasses (`MagicError`, `ChecksumError`, `UnknownTypeError`, …) let the
endpoints log *why* a packet was dropped, which keeps integrity failures separable from
simulated loss (IN-04).

### 3.3 Control payloads

START and FIN carry metadata; the header alone is not enough. Recommend a JSON object in
the payload — self-describing, easy to read in a Wireshark payload pane, and cheap since
these packets are rare:

- `START`: `{"filename": ..., "filesize": ..., "hash": ..., "segment_size": ..., "mode": ..., "run_id": ...}`
- `START_ACK`: `{"accepted": true, "segment_size": ...}`
- `FIN`: `{"total_segments": ...}`
- `FIN_ACK`: `{"hash": ..., "match": true|false, "bytes_written": ...}`
- `MODE`: `{"mode": "SR", "effective_from_seq": N, "reason": ..., "epoch": K}`

`hash` is SHA-256 of the source file (IN-05).

## 4. Strategy interface

Both `gbn.py` and `sr.py` provide a sender half and a receiver half behind a common
interface, so `hybrid.py` swaps implementations without touching lifecycle code.

```python
class SenderStrategy(Protocol):
    name: str                                    # "GBN" | "SR"
    def packets_to_send(self, now) -> list[int]: ...   # seqs allowed by the window now
    def on_ack(self, pkt) -> AckResult: ...            # advance/mark; returns newly-acked seqs
    def on_timeout(self, now) -> list[int]: ...        # seqs to retransmit, per mode rule
    def all_acked(self) -> bool: ...
    def export_state(self) -> SenderTransferState: ...  # for mode handover (§6)

class ReceiverStrategy(Protocol):
    name: str
    def on_data(self, pkt) -> ReceiverResult: ...      # bytes to deliver + ACK to send
    def export_state(self) -> ReceiverTransferState: ...
```

`SenderTransferState` is the mode-independent core of the transfer — the set of segments,
which are acknowledged, and the send window — and is precisely what survives a switch (§6).

## 5. GBN and SR

### 5.1 GBN (`protocol/gbn.py`)

Sender state (specs.md §7.1): `base`, `next_seq`, `window_size`, an outstanding-packet
buffer, and a **single timer for the oldest outstanding packet**.

- Send rule: transmit while `next_seq < base + window_size` and segments remain.
- ACK rule: cumulative. **DECISION D5 — GBN ACK semantics.** Recommend
  `ACK = highest in-order sequence received`, so ACK *n* means "0..n received"; an ACK with
  `ack < base` is ignored (GBN-03). The alternative ("next expected") is equally valid — what
  matters is that one is chosen, documented, and used identically on both endpoints (SEQ-03).
- Timeout rule: retransmit the whole outstanding range `base .. next_seq-1` and restart the
  timer (GBN-04). Every retransmitted segment is logged as a `RETX` event with
  `reason=TIMEOUT` and the active mode (TO-03).

Receiver state: a single `expected_seq`. In-order DATA is written straight through;
anything else is discarded and re-ACKed with the last in-order sequence. No buffering — this
is the state-simplicity side of the trade-off, and the reason a single loss forces the
retransmission of correctly-received later packets.

### 5.2 SR (`protocol/sr.py`)

Sender state (specs.md §8.1): `send_base`, `next_seq`, `window_size`, and per-segment
records `{seq: (payload, acked, sent_at, retx_count, timer_deadline)}` — i.e. **one timer
per outstanding segment**, which is the essential structural difference from GBN.

- ACK rule: **DECISION D6 — SR ACK semantics.** Recommend per-segment ACKs where
  `ACK = the sequence being acknowledged` (not cumulative). Duplicate ACKs are idempotent.
  `send_base` advances past the contiguous run of acked segments.
- Timeout rule: retransmit **only** the timed-out segment (SR-10), and restart only that
  timer.

Receiver state: `expected_seq` plus an out-of-order buffer `{seq: payload}` for segments
inside `[expected_seq, expected_seq + window_size)`. On DATA:

1. Outside the receive window or already buffered/delivered → re-ACK, deliver nothing (SR-09).
2. Otherwise buffer it, ACK it individually (SR-07).
3. While `expected_seq` is in the buffer, pop and deliver, advancing `expected_seq` (SR-08).

**Window constraint.** SR correctness requires the send and receive windows to be no larger
than half the sequence space. With a 4-byte sequence number and no wraparound this is not a
practical constraint, but the invariant is asserted so a future wraparound change cannot
silently violate it.

### 5.3 Duplicate-write protection

Independent of mode, the receiver's file writer keeps the highest delivered sequence and
refuses to write a segment at or below it (SEQ-05, CC-03). This is a second line of defense:
a strategy bug becomes a logged anomaly rather than a corrupted output file.

### 5.4 Timeout policy

Per specs.md §11, timeout behavior is kept as similar as practical across all three systems
so the comparison isolates retransmission strategy, not timer tuning:

- Fixed `RTO` from `config.py` for GBN, SR, and Hybrid alike. No adaptive RTO (§3 of specs).
- **DECISION D7 — baseline `RTO`.** Recommend `max(4 × RTT, 200 ms)` computed *per
  experimental condition and then held fixed for the whole run*, and recorded in the run
  config. A single absolute constant cannot serve both the 10 ms and 500 ms RTT conditions
  (E7); deriving it from the condition keeps the three systems comparable within each cell of
  the matrix, which is what fairness requires.
- RTT samples are measured for analysis (from send time to first ACK of a segment that was
  never retransmitted — Karn's rule, so retransmission ambiguity does not pollute samples)
  but are **not** fed back into the RTO.
- Timer start/stop is logged (TO-02).

## 6. Hybrid controller (`protocol/hybrid.py`)

### 6.1 Monitoring

The controller keeps a sliding observation window and evaluates on a fixed cadence
(HY-01).

**DECISION D8 — loss estimator.** Recommend a fixed-size sliding window over the last
`N` DATA *transmission outcomes* (default `N = 50`), where an outcome is "acked without
retransmission" vs. "required retransmission":

```
loss_estimate = retransmitted_segments_in_window / total_segments_in_window
```

Rationale: it is directly observable on the sender, independent of mode (both modes count a
retransmission the same way), and bounded in `[0, 1]` so thresholds are interpretable as
loss rates. The alternative — an EWMA of the same ratio — reacts more smoothly but adds a
second tunable (α) that interacts with hysteresis and makes the dynamic-loss experiment (E8)
harder to explain.

Caveat to state in the report: under GBN a single loss inflates the retransmission count
(the whole window is resent), so the estimator over-reads loss in GBN relative to SR. That
asymmetry is a property of the estimator and must be described, not hidden — it biases the
controller toward entering SR, which is the direction that at least fails safe.

This estimator, once chosen, is **frozen** before final experiments (specs.md §10): changing
it changes every switching decision and invalidates prior runs.

Statistics tracked, per specs.md §10: packets transmitted, unique DATA packets, ACKed
packets, retransmissions, observed loss indicator, current mode, switch count, per-mode
residence time, RTT samples.

### 6.2 Decision rule with hysteresis

Two thresholds and a confirmation count (HY-02, HY-03):

```
if mode == GBN and loss_estimate > SWITCH_HIGH for HYSTERESIS_COUNT consecutive evaluations:
        switch to SR
if mode == SR  and loss_estimate < SWITCH_LOW  for HYSTERESIS_COUNT consecutive evaluations:
        switch to GBN
otherwise: stay, and reset the confirmation counter
```

Asymmetric thresholds (`SWITCH_LOW < SWITCH_HIGH`) plus the consecutive-observation
requirement give a dead band, which is what prevents a single unlucky loss burst from
oscillating the mode (HY-09).

**DECISION D9 — thresholds and hysteresis count.** Starting point for calibration:
`SWITCH_HIGH = 0.05`, `SWITCH_LOW = 0.02`, `HYSTERESIS_COUNT = 3`, evaluation cadence every
20 acked segments. These sit inside the loss grid of specs.md §17.1 so the matrix contains
conditions on both sides of the boundary. They are **not** final: T4 in `tasks.md` calibrates
them, and H-05 explicitly anticipates that bad thresholds make the hybrid worse than either
fixed strategy.

A `--mode fixed-hybrid` variant runs the controller's bookkeeping and MODE exchange but
never switches, isolating switching overhead from switching benefit (specs.md §18).

### 6.3 Safe transition mechanism

This is the correctness-critical part of the design. **DECISION D10 — transition
mechanism.** Recommend the **explicit MODE handshake at a quiescent boundary**:

1. Controller decides to switch. Sender stops introducing *new* segments (the window is not
   refilled) but keeps servicing timers under the **current** mode.
2. Sender waits until all outstanding segments are acknowledged, i.e. `send_base == next_seq`
   — a quiescent boundary where no packet's fate depends on which ACK semantics are live.
3. Sender sends `MODE {mode, effective_from_seq = next_seq, epoch = K+1}` and waits for the
   receiver's `MODE` echo, retransmitting on RTO like any other control packet.
4. On echo, both sides construct the new strategy from `export_state()` and resume sending
   from `next_seq`.
5. If the handshake exceeds the control-retry budget, the switch is **abandoned** and the
   transfer continues in the existing mode. A failed switch is a logged non-event, never a
   half-switched transfer (specs.md §13, "mode inconsistency: synchronize or fail safely").

Why quiescence rather than switching mid-window: the two modes disagree about what an ACK
*means* (D5 vs. D6). Draining the window first means no in-flight packet is ever interpreted
under the wrong semantics, which makes the transition invariants provable by construction
rather than by argument.

Cost, to be stated honestly in the report: draining the window stalls the pipe for roughly
one RTT per switch. That is a real component of measured hybrid performance and is exactly
what the fixed-hybrid control isolates.

The `epoch` counter guards against a stale MODE echo arriving late and reactivating a
superseded switch: an echo whose epoch is not current is discarded and logged.

Invariants held across a switch (HY-05…HY-09):

| Invariant | How the design guarantees it |
| --- | --- |
| No unacknowledged packet discarded | Switch only at `send_base == next_seq`; segment payloads live in the mode-independent transfer state, not inside the strategy object |
| Receiver buffers remain valid | At quiescence the SR buffer is empty and `expected_seq == next_seq`; nothing to carry over |
| Sequence numbers continue consistently | `next_seq` is owned by the transfer, not the strategy; `effective_from_seq` pins the boundary explicitly |
| Transition logged with timestamp and reason | `SWITCH` event with `loss_estimate`, `reason=MODE_THRESHOLD`, old/new mode, epoch |
| No rapid repeated transitions | Hysteresis counter plus a minimum residence time before the next evaluation |

## 7. State machines

### 7.1 Sender

```
IDLE → STARTING → SENDING → WAITING/RETRANSMITTING → SWITCHING (if required)
     → FINISHING → COMPLETE | ERROR
```

- `IDLE → STARTING`: file opened, metadata and hash computed.
- `STARTING`: send START, await START_ACK with retry budget; no START_ACK → `ERROR`
  (a receiver that never appears must fail, not hang — specs.md §13).
- `SENDING ↔ WAITING/RETRANSMITTING`: normal window servicing; timers drive retransmission
  under the active mode's rule.
- `→ SWITCHING`: entered only from a quiescent window (§6.3); returns to `SENDING`, or back
  to `SENDING` in the old mode if the handshake fails.
- `FINISHING`: all segments acked → send FIN, await FIN_ACK.
- `COMPLETE`: FIN_ACK received and reports a hash match. A hash mismatch is
  `ERROR`/integrity-failure, reported and never presented as success (IN-05, CC-01).

### 7.2 Receiver

```
LISTENING → INITIALIZING → RECEIVING → BUFFERING/DELIVERING
          → FINALIZING → COMPLETE | ERROR
```

- `INITIALIZING`: validate START, allocate output, reply START_ACK. A duplicate START for a
  run already in progress is re-ACKed idempotently (the first START_ACK may have been lost).
- `RECEIVING ↔ BUFFERING/DELIVERING`: per active mode (§5.1/§5.2).
- `FINALIZING`: on FIN, flush, hash the output, compare with the START hash, reply FIN_ACK
  carrying the result. FIN_ACK is retransmitted on a repeated FIN.
- Idle-timeout guard: if nothing arrives for a configured interval, abort and report rather
  than block forever.

### 7.3 Hybrid controller

```
MONITOR → GBN_ACTIVE → EVALUATE → SR_ACTIVE → EVALUATE → …
```

The controller evaluates conditions periodically and maintains hysteresis state
(consecutive-confirmation counter, current mode, mode entry time, epoch).

## 8. Network impairment (`network/simulator.py`)

Application-level impairment first, because it is portable to Windows and reproducible from
a seed; `tc`/netem stays optional (specs.md §1).

A wrapper around the UDP socket, applied at the **sender's send path and the receiver's
receive path** so drops are observable in program logs even when the packet never hits the
wire:

```python
class ImpairedSocket:
    def __init__(self, sock, loss_rate=0.0, delay_ms=0.0, jitter_ms=0.0,
                 seed=None, loss_schedule=None): ...
```

- **Loss**: a dedicated `random.Random(seed)` instance, never the global RNG, so impairment
  draws are independent of any other randomness and reproducible from the recorded seed
  (RP-03). A dropped packet is logged as `DROP` with the seq and direction — distinct from a
  checksum failure (IN-04).
- **Delay/jitter**: one-way delay of `delay_ms/2` per direction so a round trip totals the
  target RTT; jitter is a bounded random offset. Implemented by a scheduled-send queue rather
  than a blocking sleep, so delaying one packet cannot stall the whole sender.
- **ACK loss** is modeled by applying the same impairment to the receiver→sender direction,
  which is required by the failure tests (specs.md §25.3).
- **`loss_schedule`**: a list of `(t_seconds, loss_rate)` steps driving the dynamic-loss
  experiment E8 — this is what produces the loss-increases / loss-decreases / random-change
  conditions of specs.md §17.3.

Impairment is a wrapper, never a branch inside protocol code: at 0% loss with no delay the
protocol path is byte-identical to an unimpaired run.

## 9. Logging design

Two artifacts per run, both under `logs/<run_id>/`, written from a single event emitter so
the sender and receiver produce identically-shaped records:

- `events.csv` — one row per protocol event, columns exactly as specs.md §21:
  `timestamp, run_id, endpoint, event, sequence, ack, mode, window_size, loss_estimate, rtt_ms, reason`
- `summary.json` — the frozen run configuration (every value from specs.md §15, plus seed,
  file size, software version/commit per RP-08) and the final metrics of specs.md §20.

Event vocabulary: `SEND`, `RETX`, `ACK`, `TIMEOUT`, `TIMER_START`, `TIMER_STOP`, `SWITCH`,
`DROP`, `CHECKSUM_FAIL`, `MALFORMED`, `DUPLICATE`, `DELIVER`, `START`, `START_ACK`, `FIN`,
`FIN_ACK`, `ERROR`.

Design rules:

- `timestamp` is seconds from transfer start (monotonic clock), so sender and receiver logs
  can be interleaved without depending on wall-clock agreement.
- `SEND` and `RETX` are distinct events; retransmission count is a count of `RETX`, and
  `reason` distinguishes `TIMEOUT` from other triggers (TO-05).
- Every metric in specs.md §20 is derivable from `events.csv` alone. Nothing is computed only
  in memory and printed, because raw logs must be sufficient to explain any result (CC-06).
- Logs are append-only and never edited (RP-07). Aggregation reads them; it does not modify them.
- Writing is buffered and flushed at intervals — per-event `fsync` would distort the timing
  measurements the logs exist to record.

## 10. Experiment harness

`experiments/run_experiment.py` reads a config from `experiments/configs/` and, for each
cell of the matrix (specs.md §19) × system (GBN / SR / Hybrid) × trial:

1. Derive `run_id` (e.g. `E4_hybrid_trial03`) and a per-trial seed from a recorded base seed.
2. Launch the receiver, wait for its socket to bind, then launch the sender.
3. Wait for completion or the abort timeout; record exit status and integrity result.
4. Copy the frozen config into `summary.json`.

The same generated source file, segment size, initial window, transport, and impairment
procedure are used across all three systems within a cell — otherwise the comparison is not
fair (specs.md §18).

`experiments/analyze_results.py` loads all `events.csv`/`summary.json` with pandas,
aggregates per condition (mean plus spread across trials, not a single run), and emits the
graphs of specs.md §27 into `plots/`. Runs whose integrity check failed are reported
separately and never averaged into goodput, since a corrupted transfer has no meaningful
throughput.

## 11. Wireshark strategy

- Fixed UDP port 8888 during development so `udp.port == 8888` always works (WS-02, WS-03).
- Loopback capture for local runs; captures saved to `captures/`.
- Because the header is a fixed 21-byte prefix, sequence and ACK are readable at constant
  offsets in the payload pane without a dissector (WS-05).
- Evidence to capture deliberately: clean GBN, lossy GBN (range retransmission), lossy SR
  (individual retransmission), and at least one GBN→SR transition — the MODE exchange makes
  the transition visible as packets, not just as a log line (WS-08).
- An optional Lua dissector for named fields is a presentation nicety only; it is not on the
  critical path (specs.md §22).

## 12. Open decisions

| ID | Decision | Recommendation | Status |
| --- | --- | --- | --- |
| D1 | Byte order / packing | `!HBBBIIHHI`, big-endian, no padding, MAGIC 0x4841 | **Frozen** (T1.1) |
| D2 | Checksum algorithm + coverage | CRC-32 over header (checksum zeroed) + payload | **Frozen** (T1.2) |
| D3 | Max payload / `SEGMENT_SIZE` | 1024 B | **Frozen** (T1.3) |
| D4 | Initial sequence number | 0, segment-indexed | **Frozen** (T1.3) |
| D5 | GBN ACK semantics | Highest in-order sequence received | Proposed |
| D6 | SR ACK semantics | Per-segment, ACK = sequence acknowledged | Proposed |
| D7 | Baseline RTO | `max(4 × RTT, 200 ms)` per condition, then fixed | Proposed |
| D8 | Loss estimator | Sliding window of 50 outcomes, retx ratio | Proposed |
| D9 | Thresholds / hysteresis | HIGH 0.05, LOW 0.02, count 3 | Needs calibration (T4) |
| D10 | Transition mechanism | MODE handshake at quiescent window | Proposed |
| D11 | Primary window size | 8 (source spec default) | Proposed |
| D12 | Repetition count | 5 trials per cell | Proposed |
| D13 | Seed policy | Recorded base seed, derived per trial | Proposed |
| D14 | File size(s) | 1 MiB primary; 10 MiB secondary if time allows | Proposed |

Each of these maps to an item in specs.md §16 and must be marked frozen — in code and in
`specs.md` — before the final experiment sweep begins.
