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
| `eventlog.py` | The single event emitter behind both endpoints (§9) |
| `metrics.py` | Derives the specs.md §20 metrics from `events.csv` alone (CC-06) |
| `protocol/strategy.py` | Strategy interface, transfer state, stop-and-wait baseline (§4) |
| `network/udp.py` | UDP socket creation; the Windows ICMP-reset contract |
| `protocol/packet.py` | Packet constants, serialization, parsing, checksum |
| `protocol/gbn.py` | GBN sender/receiver state and retransmission |
| `protocol/sr.py` | SR sender/receiver state, buffering, individual retransmission |
| `protocol/hybrid.py` | Network-condition monitoring and mode selection |
| `network/simulator.py` | Optional controlled loss/delay/jitter |
| `experiments/run_experiment.py` | Automated experiment execution |
| `experiments/analyze_results.py` | Result aggregation, metrics, graphs |
| `experiments/capture_evidence.py` | The scripted Wireshark evidence set (§22.1) |
| `tools/hybrid_arq.lua` | Optional Wireshark dissector for the frozen header |
| Wireshark | Packet capture and visual verification |

### 2.1 Directory structure

```
Hybrid-ARQ/
├── sender.py
├── receiver.py
├── config.py
├── eventlog.py
├── metrics.py
├── protocol/
│   ├── __init__.py
│   ├── packet.py
│   ├── strategy.py
│   ├── gbn.py
│   ├── sr.py
│   └── hybrid.py
├── network/
│   ├── __init__.py
│   ├── udp.py
│   └── simulator.py
├── experiments/
│   ├── run_experiment.py
│   ├── analyze_results.py
│   ├── capture_evidence.py
│   ├── configs/
│   └── results/
├── tools/
│   └── hybrid_arq.lua
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

**DECISION D1 — endianness and packing. FROZEN (T1.1, specs.md §16.1).** Recommended and adopted: network byte order (big-endian) with
no implicit padding: `struct.Struct("!HBBBIIHHI")`. Explicit `!` prevents platform-dependent
alignment, which would otherwise silently break cross-machine runs.

**DECISION D2 — checksum algorithm and coverage. FROZEN (T1.2, specs.md §16.4).** Recommended and adopted: CRC-32
(`zlib.crc32`) computed over *the header with the CHECKSUM field zeroed, plus the payload*.
Rationale: covers every field (so a corrupted SEQUENCE is caught, not just corrupted data),
is deterministic across Python versions, and is cheap. Coverage must be documented in the
module docstring since it cannot be inferred from the wire bytes.

**DECISION D3 — maximum payload / `SEGMENT_SIZE`. FROZEN (T1.3, specs.md §16.3).** Recommended and adopted: 1024 bytes, keeping
21 + 1024 = 1045 bytes well under a 1500-byte Ethernet MTU so no IP fragmentation confuses
the Wireshark evidence (specs.md §22). PAYLOAD_LENGTH is 2 bytes, so the format permits up
to 65535; the *configured* limit is what must be frozen.

**DECISION D4 — initial sequence number. FROZEN (T1.3, specs.md §16.2).** Recommended and adopted: `0` for the first DATA segment, with
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

START and FIN carry metadata; the header alone is not enough. Adopted: a JSON object in
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
- ACK rule: cumulative. **DECISION D5 — GBN ACK semantics. FROZEN (T3.1, specs.md §16.5).** Recommended and adopted:
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

- ACK rule: **DECISION D6 — SR ACK semantics. FROZEN (T4.4, specs.md §16.6).** Recommended and adopted: per-segment ACKs where
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
- **DECISION D7 — baseline `RTO`. FROZEN (T4.9, specs.md §16.8).** Recommended and adopted: `max(4 × RTT, 200 ms)` computed *per
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

**DECISION D8 — loss estimator. FROZEN (T5.2, specs.md §16.9).** Recommended and adopted: a fixed-size sliding window over the last
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

**DECISION D9 — thresholds and hysteresis count. FROZEN (T7.2):** `SWITCH_HIGH = 0.10`,
`SWITCH_LOW = 0.02`, `HYSTERESIS_COUNT = 3`, evaluation cadence every 20 acked segments,
minimum residence 1 s. Calibrated by 444 recorded transfers; the evidence, the rule applied
to it and what the choice cost are in `experiments/results/calibration.md`, and specs.md
§16.10–§16.11 record the values.

Three things the sweep established that the recommendation above did not anticipate:

- **The thresholds are readings of the estimator, not loss rates.** D8 over-reads loss under
  GBN by roughly four to five times, so `SWITCH_HIGH = 0.10` fires at about 2% physical loss.
  Quoting it as "switch at 10% loss" would be wrong.
- **`SWITCH_HIGH` is nearly inert**: 0.05, 0.10 and 0.20 behave identically at every loss
  level tested, and `SWITCH_LOW` is inert across 0.01–0.10. The dead band's width comes from
  the estimator reading high under GBN and low under SR, not from the gap between the two
  numbers.
- **The hysteresis count is the only real lever on oscillation**, and it can only be
  calibrated against a condition that changes. Under static and falling-loss conditions a
  count of 1 scored best; under an alternating condition it made 60% more switches than the
  condition justified. H-05's anticipation is confirmed, and §5 of the calibration report
  lists every setting and condition under which the hybrid is worse than a fixed strategy.

A `--mode fixed-hybrid` variant runs the controller's bookkeeping and MODE exchange but
never switches, isolating switching overhead from switching benefit (specs.md §18).

### 6.3 Safe transition mechanism

This is the correctness-critical part of the design. **DECISION D10 — transition mechanism.
FROZEN (T5.4, specs.md §16.12).** Recommended and adopted: the **explicit MODE handshake at
a quiescent boundary**:

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

Sender and receiver are separate processes, so each writes into its own
`logs/<run_id>/<endpoint>/` subdirectory; the filenames and columns below are shared,
which is what lets the two logs be concatenated and interleaved during analysis.

- `events.csv` — one row per protocol event, columns exactly as specs.md §21:
  `timestamp, run_id, endpoint, event, sequence, ack, mode, window_size, loss_estimate, rtt_ms, bytes, reason`
- `summary.json` — the frozen run configuration (every value from specs.md §15, plus seed,
  file size, software version/commit per RP-08) and the final metrics of specs.md §20.

Event vocabulary: `SEND`, `RETX`, `ACK`, `TIMEOUT`, `TIMER_START`, `TIMER_STOP`, `SWITCH`,
`DROP`, `CHECKSUM_FAIL`, `MALFORMED`, `DUPLICATE`, `DELIVER`, `LOSS_CHANGE`, `START`,
`START_ACK`, `FIN`, `FIN_ACK`, `MODE`, `ERROR`.

`LOSS_CHANGE` was added in T4.2: a dynamic-loss run (E8) is only interpretable if the
moment the condition changed is recorded alongside the protocol's reaction to it, and no
other event in the vocabulary carries that.

`MODE` was added in T5.4. Every other control packet type already had an event of its own,
and the MODE *exchange* has to stay distinguishable from the `SWITCH` it may or may not
produce: a handshake that is refused, repeated or abandoned is exactly the case a reader
needs to see, and it is not a transition. `SWITCH` therefore means "the mode changed",
`MODE` means "the handshake did something". A `SWITCH` row whose reason is
`FIXED_HYBRID_NOOP` is the `--mode fixed-hybrid` control paying the drain without changing
semantics; a count of real transitions is the count of `SWITCH` rows excluding those.

`bytes` was added in T6.2. Retransmission overhead is defined in bytes (specs.md §20) and
so is goodput, and neither is computable from a log that records only sequence numbers —
CC-06 was not in fact satisfied before it existed. On `SEND` and `RETX` it is the datagram
size as it went on the wire; on `DELIVER` it is the application payload written to the
file; elsewhere it is empty.

### 9.1 Event audit (T6.1)

Every name in the vocabulary, the endpoint that emits it, and what makes it happen. The
audit is enforced by `test/test_logging.py`, not only recorded here: a name with no emitter,
an emitter outside the vocabulary, and a drift between this list and the code each fail a
test.

| Event | Emitted by | When |
| --- | --- | --- |
| `SEND` | sender | a segment goes out for the first time |
| `RETX` | sender | a segment goes out again; `reason` names the trigger (TO-05) |
| `ACK` | both | sender: an ACK arrives, `reason=DUPLICATE` if it carried nothing new. Receiver: an ACK is sent |
| `TIMEOUT` | both | sender: an RTO fires, or a control exchange runs out of time. Receiver: the idle timeout expires |
| `TIMER_START` / `TIMER_STOP` | sender | a strategy arms or cancels a timer, queued via `TimerTracker` and drained by the endpoint (TO-02) |
| `SWITCH` | both | the active mode changed, or — with `reason=FIXED_HYBRID_NOOP` — a `fixed-hybrid` handshake completed without changing it |
| `MODE` | both | a MODE request or echo was sent, refused, repeated or found stale |
| `DROP` | both | the simulator discarded a datagram; a *simulated* loss, never real corruption (IN-04) |
| `CHECKSUM_FAIL` | both | a datagram arrived with a bad CRC-32 — real corruption |
| `MALFORMED` | both | a datagram failed any other `decode()` check |
| `DUPLICATE` | receiver | a DATA segment arrived that had already been received |
| `DELIVER` | receiver | a segment was written to the output file |
| `LOSS_CHANGE` | both | a `loss_schedule` step changed the condition (T4.2) |
| `START` / `START_ACK` / `FIN` / `FIN_ACK` | both | the control exchange, from either side |
| `ERROR` | both | the transfer failed; `reason` carries the message the user is told |

Two deliberate asymmetries, decided by this audit rather than left to chance:

- **A duplicate ACK is not a `DUPLICATE` row.** `DUPLICATE` means a duplicate *DATA* arrival
  at the receiver. A duplicate ACK is an `ACK` row with `reason=DUPLICATE`, which keeps
  `ACK` rows a complete count of the ACKs actually received.
- **The receiver emits no timer events** and the sender emits no `DELIVER`, because the
  receiver owns no timers and the sender writes no file. An event absent from one endpoint's
  log means it cannot happen there, not that it went unrecorded.

The receiver's log cannot be opened until a START names the run, so events observed before
that — a rejected packet, the idle timeout when no sender ever appears — are held with the
time they occurred and replayed onto the same timeline when the log opens. A receiver that
never hears from anyone still writes `events.csv` and `summary.json`, because a run that
produced no log at all is a run nobody can explain afterwards.

Design rules:

- `timestamp` is seconds from that endpoint's start, on `time.perf_counter` — monotonic like
  `time.monotonic` but ~100 ns rather than the 15.6 ms of `GetTickCount64` on Windows, which
  would quantise every RTT sample and residence time to a tick and make E7's 10 ms RTT cell
  unmeasurable (T6.2). The sender's origin is the moment its log was created and the
  receiver's is the moment it bound its socket, so the two are *not* on a common origin:
  interleave the logs by aligning on the `START` row they share, and never subtract one
  endpoint's timestamp from the other's.
- `SEND` and `RETX` are distinct events; retransmission count is a count of `RETX`, and
  `reason` distinguishes `TIMEOUT` from other triggers (TO-05).
- Every metric in specs.md §20 is derivable from `events.csv` alone. Nothing is computed only
  in memory and printed, because raw logs must be sufficient to explain any result (CC-06).
  `metrics.py` is that claim made checkable: it derives §20 from event rows and nothing else,
  and T6.2 asserts it agrees with what the endpoints computed while running. The endpoints
  keep their own counters as a cross-check; if one ever became the only source of a metric,
  the derivation would have nothing to read and the test would fail.
- Logs are append-only and never edited (RP-07). Aggregation reads them; it does not modify them.
- Writing is buffered and flushed at intervals — per-event `fsync` would distort the timing
  measurements the logs exist to record.

## 10. Experiment harness

`experiments/run_experiment.py` reads a config from `experiments/configs/` and, for each
cell of the matrix (specs.md §19) × system (GBN / SR / Hybrid) × trial:

1. Derive `run_id` (`E4_hybrid_loss05_trial03`) and a per-trial seed from a recorded base
   seed (D13).
2. Launch the receiver, wait for its socket to bind, then launch the sender.
3. Wait for completion or the abort timeout; record exit status and integrity result.
4. Copy the frozen config into `summary.json`.

The same generated source file, segment size, initial window, transport, and impairment
procedure are used across all three systems within a cell — otherwise the comparison is not
fair (specs.md §18). The seed is derived from the condition and trial only, never from the
system, which is what makes that identity hold for the *drop sequence* and not merely for
the settings.

Two things the implementation settled that this section originally left open:

- **The two endpoints are subprocesses**, driven through their own command lines, where
  `calibrate_thresholds.py` runs its transfers in process. The sweep chose speed because it
  is several hundred short transfers; the published matrix chooses fidelity — a reader runs
  the CLI, not a library call — and, decisively, an abort timeout can only be *enforced*
  against a process. An in-process transfer that hangs cannot be abandoned without killing
  the runner with it, and specs.md §13 requires the run to be abandoned and reported.
- **The runner keeps an index, not a copy.** `experiments/results/experiment_runs.csv` holds
  one row per run, every metric re-derived from that run's `events.csv` by `metrics.py`, plus
  the SHA-256 of both raw event logs. `--verify` recomputes those hashes, so a log that was
  edited, truncated or regenerated between the run and the analysis is a reported mismatch
  rather than a silent one (RP-07, T8.7). The raw logs themselves are never rewritten.
- A run's status distinguishes `ok` from `integrity`, `failed` and `aborted`. A transfer
  whose hashes did not match is never recorded as `ok`, whatever the sender's exit code
  said (CC-01).

`experiments/analyze_results.py` aggregates per condition (mean plus spread across trials,
not a single run) and emits the graphs of specs.md §27 into `plots/`. Runs whose integrity
check failed are reported separately and never averaged into goodput, since a corrupted
transfer has no meaningful throughput.

It reads the run index rather than re-deriving 195 runs on every invocation — but the index
*is* the logs, one step removed, since `run_experiment.py` built each row by calling
`metrics.py` on that run's `events.csv`. `--check-logs` re-derives everything and compares,
so the shortcut cannot quietly stop being equivalent. The mode timeline is the exception that
must come from the logs directly: "mode selection over time" is a sequence, and an aggregate
row cannot hold one.

`experiments/capture_evidence.py` records the specs.md §22.1 evidence set the same way the
matrix was run — real transfers through the endpoints' CLIs, with `tshark` on the loopback
adapter — so a capture can be retaken and compared rather than taken on trust. Each scenario
carries a fixed seed; the lossy GBN and lossy SR scenarios deliberately share one, so the two
files show a single drop stream handled two ways.

## 11. Wireshark strategy

- Fixed UDP port 8888 during development so `udp.port == 8888` always works (WS-02, WS-03).
- Loopback capture for local runs; captures saved to `captures/`.
- Because the header is a fixed 21-byte prefix, sequence and ACK are readable at constant
  offsets in the payload pane without a dissector (WS-05).
- Evidence to capture deliberately: clean GBN, lossy GBN (range retransmission), lossy SR
  (individual retransmission), and at least one GBN→SR transition — the MODE exchange makes
  the transition visible as packets, not just as a log line (WS-08).
- `tools/hybrid_arq.lua` is that optional Lua dissector (T9.5). specs.md §22 rightly calls it
  a presentation nicety — the header is a fixed 21-byte prefix and reads without one — but it
  turned out to earn more than that: `capture_evidence.py` counts each capture's contents
  *through* it, so what a committed `.pcapng` holds is a query anyone can re-run rather than
  a claim about what someone saw on screen.
- Captures of the two long transition runs are taken with a snaplen: every header byte is
  kept and only the payloads are truncated. A transition needs ~110 acknowledged segments of
  evidence before the controller will act, and the payload of a DATA packet is the file,
  which the hash already verifies.

## 12. Open decisions

| ID | Decision | Recommendation | Status |
| --- | --- | --- | --- |
| D1 | Byte order / packing | `!HBBBIIHHI`, big-endian, no padding, MAGIC 0x4841 | **Frozen** (T1.1) |
| D2 | Checksum algorithm + coverage | CRC-32 over header (checksum zeroed) + payload | **Frozen** (T1.2) |
| D3 | Max payload / `SEGMENT_SIZE` | 1024 B | **Frozen** (T1.3) |
| D4 | Initial sequence number | 0, segment-indexed | **Frozen** (T1.3) |
| D5 | GBN ACK semantics | Highest in-order sequence received; no ACK before the first | **Frozen** (T3.1) |
| D6 | SR ACK semantics | Per-segment, ACK = sequence acknowledged | **Frozen** (T4.4) |
| D7 | Baseline RTO | `max(4 × RTT, 200 ms)` per condition, then fixed | **Frozen** (T4.9) |
| D8 | Loss estimator | Sliding window of 50 segment outcomes, retx ratio, recorded at ACK | **Frozen** (T5.2) |
| D9 | Thresholds / hysteresis | HIGH 0.10, LOW 0.02, count 3, cadence 20 acked segments, min residence 1 s | **Frozen** (T7.2) |
| D10 | Transition mechanism | MODE handshake at a quiescent window, epoch-guarded, abandoned on retry exhaustion | **Frozen** (T5.4) |
| D11 | Primary window size | 8 (source spec default) | **Frozen** (T4.9) |
| D12 | Repetition count | 5 trials per cell | **Frozen** (T8.1) |
| D13 | Seed policy | One recorded base seed; `sha256(base/experiment/condition/trial)` per trial, system not an input | **Frozen** (T8.1) |
| D14 | File size(s) | 1 MiB for the whole matrix; the proposed 10 MiB second size was not run | **Frozen** (T8.1) |

Each of these maps to an item in specs.md §16 and must be marked frozen — in code and in
`specs.md` — before the final experiment sweep begins. **All fourteen are now frozen**, and
the DECISION blocks earlier in this document carry the same marker where they are stated, so
a reader who arrives at one of them mid-document is not left thinking it is still a proposal
(T10.2). specs.md §16 holds each final value together with the rationale that was recorded
at the moment it was frozen — not reconstructed afterwards, which is how a frozen value
becomes an unexplained one.
D12-D14 were settled at T8.1 against a measured pilot of the matrix (one trial of E1 and one
of E6 at 1 MiB and 100 ms RTT: 16.2 s clean, 137.1 s for GBN at 20% loss with 2112
retransmissions, 67.9 s for SR with 264), not against the recommendations they carried here
— which is why D14's second file size is recorded as *not run* rather than quietly dropped.

## 13. Frontend architecture (Phase 11, T11.1)

The frontend is a **presentation and control layer over the finished system**. It is not a
replacement for the CLI, the protocol modules, the experiment runner or Wireshark, and it
adds no capability to the protocol — it makes the capabilities that already exist easy to
see.

Decisions here are deliberately **not D-numbered**. D1–D14 are the decisions whose values are
baked into recorded evidence, so changing one invalidates experiments; a frontend framework
choice cannot invalidate a transfer that already happened. They are recorded below with their
rationale and are *not* added to `specs.md` §16. For the same reason Phase 11 introduced no
new requirement IDs: it adds no requirement to the protocol.

### 13.1 Stack

| ID | Choice | Why |
| --- | --- | --- |
| F1 | Server: `http.server` from the standard library (`frontend/server.py`) | The protocol is standard library only. Requiring Flask or FastAPI to *look at* a stdlib protocol means an evaluator installs a web framework before they can read a result. `requirements.txt` stays what it was: analysis dependencies for `experiments/`. |
| F2 | UI: hand-written ES modules, no framework and no build step | The files are served exactly as written. No `npm install`, no bundler, no `node_modules`, and no generated artifact that could drift from its source. An evaluator with a browser has everything. |
| F3 | Charts: inline SVG (`static/js/chart.js`) | Same reason as F2, and it keeps the drawing code readable: every function takes points that came out of a log and draws exactly those. The recorded figures in `plots/` are shown as images beside them, never redrawn — a second rendering of the same data could disagree with the one in the report. |
| F4 | Run command: `python -m frontend` | One command, no environment beyond the project's own. `--port` moves it, `--no-browser` suppresses the browser launch. |
| F5 | Binds `127.0.0.1` by default | The dashboard starts processes and reads this checkout. It is a local tool, not a service, and the default should not be one. |
| F6 | One dark theme, not a system-preference pair | The primary use is a demonstration on a projector. A palette that changes with the viewer's OS setting would change what the mode colours look like between the rehearsal and the room, and the figures in `plots/` are fixed regardless. |

Mode colour is consistent everywhere a mode appears and matches the figures exactly — GBN
blue, SR orange, Hybrid aqua, fixed-hybrid yellow (`experiments/results/figures.md`). Colour
is never the only carrier: every mode chip is also labelled, every chart series carries its
name, and the retransmission plot gives each event type its own marker shape.

### 13.2 Module boundaries

The layering rule of §1.1 extends by one row, and the arrow points one way only:

| Layer | Knows about | Must not know about |
| --- | --- | --- |
| `frontend/data.py` | recorded artifacts: `events.csv`, `summary.json`, results CSVs, `plots/`, `captures/` | sockets, windows, timers, how a decision was made |
| `frontend/runner.py` | subprocess lifecycle, request validation | packet layout, ARQ bookkeeping, switching policy |
| `frontend/server.py` | HTTP routing, JSON, static files | everything below `data`/`runner` |
| `frontend/static/` | rendering what an endpoint returned | all of the above |

**Nothing below the frontend imports it.** `test/test_frontend.py` parses every module under
`protocol/`, `network/` and `experiments/`, plus both endpoints, `metrics.py`, `eventlog.py`
and `config.py`, and asserts none of them names it — which is what makes "deleting the
frontend leaves the protocol's correctness and every recorded experimental result unchanged"
demonstrable rather than merely stated (T11.15). Every other test module is checked the same
way, so the suite that proves the protocol correct still runs with the package removed.

### 13.3 How data reaches the UI

Read-only, and from the artifacts that already exist:

```
logs/<group>/<run_id>/<endpoint>/events.csv   ---+
logs/<group>/<run_id>/<endpoint>/summary.json ---+--> frontend/data.py --> JSON --> /api/* --> views
experiments/results/*.csv                     ---+
plots/*.png, captures/*.pcapng                ---+
```

A **second, incompatible logging path would break CC-06** — the rule that every metric is
derivable from the event log alone — by giving one number two sources that can disagree. So
the frontend writes no event log, and a test asserts it never constructs an `EventLog` and
never opens a file for writing. Metrics come from `metrics.py`, the same derivation T6.2
asserts against the endpoints' own counters; a test asserts the API returns exactly what
`metrics.derive_from_run` returns for the same run.

**Run identity.** `EventLog` writes `<log_dir>/<run_id>/<endpoint>/`, and log directories
nest (`logs/experiments/E1_gbn_loss00_trial00/sender/`), so a bare `run_id` is not unique
across the tree. Each run therefore carries a `key` — its path relative to `logs/` — for
addressing, alongside the `run_id` it recorded, which is what the UI displays and what
`experiment_runs.csv` is joined on. Keys arrive from URLs and are resolved against `logs/`,
so `..` and absolute paths are refused.

`experiment_runs.csv` records absolute `log_dir` paths from the machine that produced the
matrix. Only the tail below `logs/` is used to find them here, and a row whose directory is
not present on this checkout says so rather than linking to nothing.

### 13.4 Live status

`frontend/runner.py` starts `receiver.py`, reads back its bound port, then starts
`sender.py` — the same orchestration `experiments/run_experiment.py` performs, reusing its
helpers rather than reimplementing them. A run launched from the dashboard is therefore the
same run as one typed into a terminal, writing the same logs through the same emitter, under
`logs/ui/`.

Progress, lifecycle state and current mode are **read back out of that run's own
`events.csv`**, not out of the runner's memory. The consequence is that the dashboard shows
the same numbers `metrics.py` would derive, and stays honest about a transfer someone started
from a terminal instead. Lifecycle states are the ones §7 already defines; no new vocabulary
is invented for the screen.

`eventlog.FLUSH_EVERY` is 64 and **stays 64**: a per-event `fsync` would distort the very
timings the log exists to measure. The live view is therefore *near*-real-time and can trail
a transfer by up to 64 events. T11.4 states that in the UI rather than hiding it — the flush
policy must not be loosened to make an animation smoother, which would trade measurement
fidelity for presentation.

One transfer at a time. Two would contend for the UDP port and, worse, would make "the
current run" ambiguous everywhere else in the UI.

### 13.5 What the UI is forbidden to do

Three rules, each checked by a test rather than left to discipline:

1. **It never computes protocol behaviour.** The switching policy stays in
   `protocol/hybrid.py`. Switch reasons, the loss estimate at a transition and the epoch are
   read from the recorded `SWITCH` and `MODE` rows verbatim. A test parses `data.py` and
   fails if it compares anything against `SWITCH_HIGH`, `SWITCH_LOW` or `HYSTERESIS_COUNT` —
   a threshold evaluated a second time is a second controller, and the two would drift.
   A `SWITCH` row whose reason is `FIXED_HYBRID_NOOP` is shown as the control paying the
   drain cost, and is excluded from the transition count exactly as `metrics.py` excludes it.
2. **It never fabricates.** No placeholder metric, invented packet, simulated switch or
   demo-only animation that looks like measured behaviour. A missing run, an absent results
   CSV or a capture that does not exist produces an explicit empty state naming the path it
   looked for. A value that was not recorded renders as a dash, never as zero, and a run with
   no FIN_ACK verdict is never reported as a passing integrity check (CC-01).
3. **Frozen values are shown, never edited.** The editable fields are declared in
   `data.EDITABLE_FIELDS`; `SEGMENT_SIZE`, the D9 thresholds, the hysteresis count, the
   evaluation cadence, the minimum residence and the D8 window are read-only context carrying
   the decision that froze each one. RTO is not a field at all: D7 derives it from the
   condition's RTT and then holds it fixed for the run. An unknown key is refused rather than
   passed through — the same discipline `config.snapshot` applies to a recorded config.

The loss estimate is labelled everywhere as **a reading of the D8 estimator, not a loss
rate**. It over-reads under GBN by four to five times, so `SWITCH_HIGH = 0.10` fires at
roughly 2% physical loss, and the UI never describes it as "switch at 10% loss". Configured
impairment and observed measurement appear under separate headings for the same reason:
simulated loss is a controlled input, not a measurement of a network.

Results that reflect badly on the hybrid are shown with the rest (H-05). The comparison view
has a panel of its own for the cells where the hybrid loses to a fixed strategy and for the
1–2% oscillation, and points at `calibration.md` §5 for the full account.

### 13.6 Wireshark, and what the dashboard does not claim

The dashboard reads logs; Wireshark reads the wire. The companion panel gives the filter, the
port, the mode and the timestamps worth jumping to, and states which facts come from which
source. **It does not inspect packets**, and says so. `captures/` holds the curated T9.4
evidence set and the demonstration capture — one per *scenario*, not one per transfer — so a
run is offered the relevant recorded capture, labelled as being from another run, rather than
being implied to have one of its own.

### 13.7 Known gaps, documented rather than worked around

Found before planning rather than during, per rule 15 of the phase brief:

- **Live event data lags by up to 64 rows**, by design (§13.4). Stated in the UI.
- **Captures exist per scenario, not per run** (§13.6). Stated in the UI.
- **The receiver's computed hash was not in its `summary.json`.** It recorded
  `expected_sha256` and sent its own hash in the FIN_ACK payload, but never wrote it down, so
  "source hash vs received hash" had only one side on disk. `received_sha256` was added
  beside it — a *logging* addition of exactly the kind T6.2 made when it added the `bytes`
  column, not a protocol change, and covered by `test/test_logging.py`.
