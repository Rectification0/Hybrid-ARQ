# Specification — Hybrid Go-Back-N / Selective Repeat ARQ with Adaptive Switching

**Status:** Working specification
**Source of truth:** `Hybrid_GBN_SR_Functionality_and_Technical_Specification.docx`
**Scope:** Normative functional, protocol, experimental, and acceptance requirements.

This document restates the source specification in an implementable, traceable form.
`design.md` covers how these requirements are realized; `tasks.md` covers execution order.
Where the source document says a value is "to be frozen" or "to be determined", this
document marks it **OPEN** — those values must be fixed (see §16) before final experiments.

---

## 1. Project Definition

A reliable file-transfer protocol over UDP that combines Go-Back-N (GBN) and Selective
Repeat (SR) sliding-window ARQ, plus an adaptive controller that selects the active
strategy at runtime according to measured network conditions — primarily packet-loss
behavior.

This is an **engineering and experimental implementation**. The project must not claim
that GBN/SR switching is globally novel. Its contribution is the transparent
implementation, the switching policy, controlled evaluation, and demonstration of the
GBN vs. SR trade-off.

| Item | Specification |
| --- | --- |
| Project type | Computer Networks protocol implementation and experimental evaluation |
| Transport | UDP |
| Language | Python 3 |
| Core mechanisms | Sliding Window ARQ, GBN, SR |
| Upgrade | Runtime adaptive switching between GBN and SR |
| Primary adaptation signal | Observed packet-loss behavior |
| Experimental variables | RTT, delay, jitter, bandwidth, changing loss |
| Packet observation | Wireshark |
| Data analysis | Python, pandas, Matplotlib |
| Network emulation | Application-level loss injection initially; optional Linux `tc`/netem later |
| Initial UDP port | 8888 |

## 2. Goals

- G-01 Implement a correct baseline GBN protocol over UDP.
- G-02 Implement a correct baseline SR protocol over UDP.
- G-03 Use a common packet and file-transfer layer for both modes.
- G-04 Measure network conditions during transfer.
- G-05 Implement runtime GBN/SR mode switching.
- G-06 Use hysteresis to prevent rapid mode oscillation.
- G-07 Demonstrate behavior in Wireshark.
- G-08 Compare pure GBN, pure SR, and the adaptive hybrid under identical conditions.
- G-09 Collect reproducible logs and quantitative results.
- G-10 Verify every successful transfer by comparing source and received file integrity.

## 3. Scope Boundaries

Out of scope, explicitly:

- Replacing TCP, or implementing full TCP congestion control.
- Treating TCP's congestion window as identical to an ARQ reliability window.
- Any claim that GBN, SR, adaptive RTO, or adaptive switching is invented here.
- Adaptive RTO as the primary upgrade — timeout behavior stays **controlled** so the
  mode-switching effect can be isolated (see §11).
- Encryption, authentication, NAT traversal, multi-client support, production-grade security.

## 4. Functional Requirements

| ID | Requirement | Priority |
| --- | --- | --- |
| FR-01 | Sender accepts a source file and destination IP/port. | Must |
| FR-02 | Receiver listens on a configurable UDP port. | Must |
| FR-03 | Sender divides the file into numbered DATA segments. | Must |
| FR-04 | Receiver validates packets before accepting payload. | Must |
| FR-05 | Sender maintains a sliding transmission window. | Must |
| FR-06 | System supports GBN mode. | Must |
| FR-07 | System supports SR mode. | Must |
| FR-08 | System supports adaptive GBN/SR switching. | Must |
| FR-09 | System records retransmission and mode-switch events. | Must |
| FR-10 | Receiver reconstructs the file in original order. | Must |
| FR-11 | System verifies transfer integrity. | Must |
| FR-12 | System exposes sufficient packet information for Wireshark analysis. | Must |
| FR-13 | Experiment runner supports repeated controlled trials. | Should |
| FR-14 | Analysis tools calculate primary performance metrics. | Should |

## 5. Packet Specification

**FROZEN** (T1.1, T1.3 — decisions D1, D3, D4). The authoritative definition is the
docstring of `protocol/packet.py`; this section records the same layout.

```
MAGIC | VERSION | TYPE | FLAGS | SEQUENCE | ACK | WINDOW | PAYLOAD_LENGTH | CHECKSUM | PAYLOAD
```

Network byte order (big-endian), no implicit padding: `struct.Struct("!HBBBIIHHI")`,
a fixed **21-byte** header.

| Offset | Field | Size | Value / Meaning |
| --- | --- | --- | --- |
| 0 | MAGIC | 2 bytes | `0x4841`, ASCII `"HA"` — identifies protocol packets |
| 2 | VERSION | 1 byte | `1` |
| 3 | TYPE | 1 byte | DATA=1, ACK=2, START=3, START_ACK=4, FIN=5, FIN_ACK=6, MODE=7 |
| 4 | FLAGS | 1 byte | Reserved, `0` |
| 5 | SEQUENCE | 4 bytes | DATA segment index (not a byte offset) |
| 9 | ACK | 4 bytes | Acknowledgement number |
| 13 | WINDOW | 2 bytes | Window information |
| 15 | PAYLOAD_LENGTH | 2 bytes | Payload length in bytes |
| 17 | CHECKSUM | 4 bytes | CRC-32, see below |
| 21 | PAYLOAD | Variable | Data/control payload |

**Checksum coverage (D2):** CRC-32 (`zlib.crc32`) over the full 21-byte header **with the
CHECKSUM field zeroed**, followed by the payload — that is, every byte of the packet. A
corrupted SEQUENCE, TYPE or WINDOW is therefore detected, not only a corrupted payload.
Coverage cannot be inferred from the wire bytes, which is why it is written down in both
places. This detects accidental corruption only; it is not a MAC.

**Maximum DATA payload (D3):** `SEGMENT_SIZE = 1024` bytes, so 1045 bytes on the wire —
under the 1500-byte Ethernet MTU, confirmed fragmentation-free in a loopback capture
(T1.7). PAYLOAD_LENGTH is 2 bytes, so the format permits 65535; 1024 is the configured
limit DATA is held to.

Control payloads (START, START_ACK, FIN, FIN_ACK, MODE) are compact UTF-8 JSON objects
with sorted keys, so identical logical content always produces identical bytes.

### 5.1 Packet Types

| Type | Direction | Purpose |
| --- | --- | --- |
| DATA | Sender → Receiver | Carries file data |
| ACK | Receiver → Sender | Acknowledges DATA |
| START | Sender → Receiver | Begins transfer and sends metadata |
| START_ACK | Receiver → Sender | Confirms initialization |
| FIN | Sender → Receiver | Requests completion |
| FIN_ACK | Receiver → Sender | Confirms completion |
| MODE | Control | Optional explicit mode synchronization |

The implementation may reduce control types if a simpler, rigorously defined state
machine is preferable.

### 5.2 Sequence Number Rules

- SEQ-01 Each DATA segment receives a unique sequence number within a transfer.
- SEQ-02 The first sequence number must be selected once and used consistently.
- SEQ-03 ACK semantics must be unambiguous and documented.
- SEQ-04 Sequence wraparound is outside the first implementation unless required.
- SEQ-05 Duplicate DATA must never be written twice.

## 6. File Transfer Lifecycle

1. Receiver starts and binds its UDP socket.
2. Sender opens the source file.
3. Sender calculates metadata: filename, file size, integrity hash.
4. Sender sends START.
5. Receiver validates START and returns START_ACK.
6. Both sides initialize protocol state.
7. Sender segments the file into DATA packets.
8. Sender transmits packets subject to the current window.
9. Receiver validates and processes packets.
10. Receiver returns ACKs according to the active mode.
11. Sender advances its window as acknowledgements arrive.
12. Loss triggers the active mode's retransmission behavior.
13. Hybrid controller evaluates network observations.
14. If the switching rule is satisfied, the active mode changes at a safe protocol boundary.
15. Transfer continues until all data is acknowledged.
16. Sender sends FIN.
17. Receiver verifies reconstructed-file integrity.
18. Receiver sends FIN_ACK.
19. Both endpoints close the transfer and write final metrics.

## 7. GBN Requirements

### 7.1 Sender State
- `base` — oldest unacknowledged sequence number.
- `next_seq` — next sequence number available for transmission.
- `window_size` — maximum number of outstanding DATA packets.
- Outstanding packet buffer.
- Timer for the oldest outstanding packet, per the selected baseline timer policy.

### 7.2 ACK Processing
- GBN-01 ACKs are cumulative.
- GBN-02 Valid cumulative ACKs advance the sender base.
- GBN-03 Old or duplicate ACKs must not move the base backward.

### 7.3 Loss and Timeout
- GBN-04 On timeout, the sender retransmits the outstanding sequence range starting at
  the oldest unacknowledged packet, per the GBN rule.
- GBN-05 Retransmission events are logged.

### 7.4 Expected Trade-off
Simpler receiver state and generally lower buffering requirements, but a single loss can
cause correctly received later packets to be retransmitted.

## 8. SR Requirements

### 8.1 Sender State
- SR-01 Track acknowledgement state independently for outstanding packets.
- SR-02 Retain unacknowledged packets for possible individual retransmission.
- SR-03 Maintain sender-window boundaries.

### 8.2 Receiver State
- SR-04 Maintain the next in-order delivery position.
- SR-05 Accept valid packets within the receiver window.
- SR-06 Buffer correctly received out-of-order packets.
- SR-07 ACK individual packets.
- SR-08 Deliver buffered data when missing earlier data arrives.
- SR-09 Ignore duplicate payload delivery.

### 8.3 Loss and Retransmission
- SR-10 The sender retransmits **only** the missing packet, once loss is detected by the
  selected timeout/ACK mechanism.

### 8.4 Expected Trade-off
Can reduce retransmitted bytes under loss, but requires more sender/receiver state and
buffering.

## 9. Hybrid Controller Requirements

**Purpose.** Change the retransmission strategy without changing the application's
file-transfer semantics.

**Monitoring.** HY-01 The controller observes a defined recent packet window or
measurement interval and computes an observed loss indicator using the selected,
documented estimator.

**Decision.** HY-02 Initial principle: low observed loss favors GBN; sustained high
observed loss favors SR. Thresholds must be configurable and experimentally evaluated.

**Hysteresis.** HY-03 Use separate entry/exit conditions and/or a required number of
consecutive observations, to prevent GBN↔SR oscillation caused by individual random losses.

**Synchronization.** HY-04 Both endpoints must know which ACK/retransmission semantics
are active. A mode change must occur at a documented safe boundary or via an explicit
MODE exchange. Unacknowledged DATA must remain recoverable.

**Transition invariants.**
- HY-05 No unacknowledged packet may be discarded during a switch.
- HY-06 Receiver buffers remain valid.
- HY-07 Sender sequence numbers continue consistently.
- HY-08 Mode transition is logged with timestamp and reason.
- HY-09 Rapid repeated transitions are prevented by hysteresis.

## 10. Network Condition Monitoring

| Statistic | Meaning |
| --- | --- |
| Packets transmitted | Number of DATA transmissions |
| Unique DATA packets | Number of distinct DATA sequence numbers generated |
| ACKed packets | Number of DATA packets confirmed |
| Retransmissions | Additional DATA transmissions |
| Observed loss indicator | Defined recent-loss/retransmission estimator |
| Current mode | GBN or SR |
| Switch count | Number of mode transitions |
| Residence time | Time spent in each mode |
| RTT sample | Optional timing sample for analysis |

**The loss estimator must be frozen before final experiments**, because changing it
changes the switching behavior.

## 11. Timeout Policy

For the primary comparison, keep timeout behavior as similar as practical between GBN,
SR, and Hybrid. This isolates the effect of retransmission strategy.

- TO-01 Timeout is configurable.
- TO-02 Timer start/stop events are logged.
- TO-03 Retransmission logs include sequence number and mode.
- TO-04 Timeout behavior at mode transitions is explicitly defined.
- TO-05 Retransmission reasons distinguish timeout from other triggers.

## 12. Integrity / Error Detection

- IN-01 DATA packets contain an integrity field.
- IN-02 Receiver validates integrity before accepting payload.
- IN-03 Corrupted packets are never written to the output file.
- IN-04 Integrity failures are logged separately from simulated loss.
- IN-05 Final source/output hashes are compared.

## 13. Error Handling

| Failure | Required behavior |
| --- | --- |
| Malformed packet | Discard and log |
| Checksum failure | Discard, log, and rely on retransmission |
| Unexpected sequence | Handle according to active GBN/SR rules |
| Duplicate DATA | Do not duplicate output |
| Unknown packet type | Discard and log |
| Transfer timeout | Abort after configurable retry policy and report failure |
| Receiver unavailable | Eventually report failure rather than hang forever |
| Hash mismatch | Report integrity failure |
| Mode inconsistency | Synchronize or fail safely; never silently continue |

## 14. Interface Requirements

Proposed CLI:

```
python sender.py --file sample.bin --host 127.0.0.1 --port 8888 --mode hybrid --window 8
python receiver.py --port 8888 --output received.bin
```

Experiment configuration must allow mode, window size, impairment settings, seed, file
size, and output directory to be specified **without changing protocol code**.

## 15. Configuration Parameters

| Parameter | Purpose | Initial status |
| --- | --- | --- |
| `HOST` | Receiver address | `127.0.0.1` |
| `PORT` | UDP port | `8888` |
| `SEGMENT_SIZE` | DATA payload size | **OPEN** — to be frozen |
| `WINDOW_SIZE` | Initial window | `8` |
| `RTO` | Baseline timeout | **OPEN** — to be frozen |
| `LOSS_RATE` | Experiment loss rate | Per experiment |
| `RTT` | Network condition | Per experiment |
| `SWITCH_HIGH` | Enter-SR criterion | **OPEN** — TBD |
| `SWITCH_LOW` | Return-to-GBN criterion | **OPEN** — TBD |
| `HYSTERESIS_COUNT` | Required sustained observations | **OPEN** — TBD |
| `RANDOM_SEED` | Reproducibility | Per experiment |

## 16. Decisions to Freeze Before Final Experiments

Frozen items record their final value here. An item is frozen only when it is in the code,
recorded in this section, and marked Frozen in `design.md` §12.

1. ✅ **FROZEN (T1.1, D1)** — Exact packet byte layout and endianness:
   `struct.Struct("!HBBBIIHHI")`, big-endian, no padding, 21-byte header, MAGIC `0x4841`
   (`"HA"`), VERSION 1. Full offset table in §5. Rationale: explicit `!` prevents
   platform-dependent alignment that would silently break a cross-machine run; an ASCII
   MAGIC makes the packet identifiable in the Wireshark ASCII pane without a dissector.
2. ✅ **FROZEN (T1.3, D4)** — Initial sequence-number convention: first DATA segment is
   `0`, and sequence numbers are **segment indices**, not byte offsets. Rationale: window
   arithmetic, logs and captures stay directly comparable to textbook GBN/SR. Wraparound is
   out of scope (SEQ-04) — uint32 at 1 KiB segments covers ~4 TiB per transfer.
3. ✅ **FROZEN (T1.3, D3)** — Maximum DATA payload size: `SEGMENT_SIZE = 1024` bytes
   (1045 on the wire). Rationale: under the Ethernet MTU so captures show no IP
   fragmentation; verified fragmentation-free in T1.7.
4. ✅ **FROZEN (T1.2, D2)** — Checksum algorithm and coverage: CRC-32 (`zlib.crc32`) over
   the header with CHECKSUM zeroed plus the payload — every byte of the packet. Rationale:
   catches corruption in any header field, not just the payload; deterministic across
   Python versions; cheap. Verified by an exhaustive single-bit-flip test over all 168
   header bits (T1.6).
5. ✅ **FROZEN (T3.1, D5)** — GBN ACK semantics: **an ACK carries the highest in-order
   sequence number received**, so `ACK n` means "0..n arrived" and the sender advances
   `base` to `n+1`. ACKs are cumulative (GBN-01). An ACK below `base` is stale and is
   ignored; so is an ACK at or above `next_seq`, which acknowledges something never sent
   (GBN-03). Rationale: the value reads directly off a Wireshark capture as what actually
   arrived, with no off-by-one for the reader to apply. Corner case, stated because the
   convention requires it: before the first in-order segment there is no highest-in-order
   value (0 already means "segment 0 arrived"), so the receiver **sends no ACK at all**
   until one arrives — safe because the sender's timer already covers a window whose first
   segment was lost.
6. ✅ **FROZEN (T4.4, D6)** — SR ACK semantics: **an ACK acknowledges exactly the one
   segment it names**, not cumulatively. `ACK n` means "segment n arrived" and says nothing
   about n-1. Duplicate ACKs are idempotent; `send_base` advances past the contiguous run
   of acknowledged segments. Rationale: individual acknowledgement is what permits
   individual retransmission (SR-10) — the entire point of SR. Consequence, stated because
   it is the cost: a lost ACK is **not** repaired by the next one as it is under GBN's
   cumulative rule, but by that segment's own timer expiring. Note that `ACK n` therefore
   means different things under D5 and D6, which is exactly why a mode switch requires a
   quiescent window (D10) — an ACK read under the wrong convention would acknowledge
   segments that never arrived.
7. ✅ **FROZEN (T4.9, D11)** — Primary window size: `WINDOW_SIZE = 8` outstanding
   segments, identical for GBN, SR and Hybrid. Rationale: the source specification's own
   default, and fairness requires not the *best* window but the *same* window within each
   cell of the matrix (§18, RP-04). At 1024 B segments this puts 8 KiB in flight, enough
   for GBN's range retransmission to cost visibly more than SR's single resend — which is
   the effect being measured.
8. ✅ **FROZEN (T4.9, D7)** — Baseline timeout: `RTO = max(4 x RTT, 200 ms)`, computed
   once per experimental condition from that condition's RTT and then **held fixed for the
   whole run**, recorded in `summary.json`. Implemented as `config.baseline_rto()`.
   Rationale: a single absolute constant cannot serve both the 10 ms and 500 ms RTT cells
   of E7 — too short and every condition drowns in spurious retransmissions, too long and
   the fast conditions idle. Deriving it per condition keeps the three systems comparable
   *within* each cell, which is what fairness actually requires (§11). No adaptive RTO:
   RTT samples are measured for analysis only (Karn's rule) and never fed back, so the
   comparison isolates retransmission strategy rather than timer tuning (§3).
9. Loss-estimation formula.
10. Switching threshold(s).
11. Hysteresis rule.
12. Safe mode-transition mechanism.
13. Experiment repetition count.
14. Random-loss seed policy.
15. File size(s).

## 17. Network Impairment

### 17.1 Loss conditions

| Condition | Target loss |
| --- | --- |
| Clean | 0% |
| Very low | 1% |
| Low | 2% |
| Moderate | 5% |
| High | 10% |
| Very high | 20% |

### 17.2 RTT
Candidate conditions: 10 ms, 50 ms, 100 ms, 500 ms — subject to the selected test
environment.

### 17.3 Dynamic conditions
- Loss increases during a transfer.
- Loss decreases during a transfer.
- Random loss changes over time.
- Optional delay/jitter changes.

Useful comparison families: fixed-loss/varying-RTT, fixed-RTT/varying-loss, dynamic-loss.

## 18. Baselines

| System | Purpose |
| --- | --- |
| Pure GBN | Always use GBN |
| Pure SR | Always use SR |
| Hybrid GBN/SR | Adaptive strategy |
| Optional fixed hybrid | Control for switching overhead, if needed |

All primary comparisons must use the same source file, segment size, initial window,
transport, impairment condition, and measurement procedure.

## 19. Experimental Matrix

| Experiment | Loss | RTT | Systems |
| --- | --- | --- | --- |
| E1 | 0% | 100 ms | GBN / SR / Hybrid |
| E2 | 1% | 100 ms | GBN / SR / Hybrid |
| E3 | 2% | 100 ms | GBN / SR / Hybrid |
| E4 | 5% | 100 ms | GBN / SR / Hybrid |
| E5 | 10% | 100 ms | GBN / SR / Hybrid |
| E6 | 20% | 100 ms | GBN / SR / Hybrid |
| E7 | Fixed loss | 10/50/100/500 ms | GBN / SR / Hybrid |
| E8 | Dynamic | Changing | Hybrid |

Each condition must be repeated consistently. Repetition count, file size, random-seed
policy, and configuration must be recorded.

## 20. Metrics

| Metric | Definition |
| --- | --- |
| Goodput | Successfully delivered application bytes / transfer time |
| Completion time | Time from transfer start to verified completion |
| Retransmission count | Total DATA retransmission events |
| Retransmission overhead | Retransmitted bytes / total transmitted bytes |
| Integrity success | Whether source and received hashes match |
| Switch count | Total GBN↔SR transitions |
| GBN residence | Time spent in GBN |
| SR residence | Time spent in SR |
| Latency | Measured delivery/transfer delay where defined |
| Optional RTT statistics | Mean, median, tail RTT |

## 21. Logging Specification

Every important protocol event is recorded with enough context to reconstruct behavior.

| Field | Example |
| --- | --- |
| `timestamp` | `12.482931` |
| `run_id` | `loss05_trial03` |
| `endpoint` | `sender` |
| `event` | `SEND` / `ACK` / `TIMEOUT` / `RETX` / `SWITCH` |
| `sequence` | `42` |
| `ack` | `41` |
| `mode` | `SR` |
| `window_size` | `8` |
| `loss_estimate` | `0.071` |
| `rtt_ms` | `103.4` |
| `reason` | `TIMEOUT` / `MODE_THRESHOLD` |

CSV is recommended for experiment summaries; detailed event logs may additionally use JSON.

## 22. Wireshark Requirements

- WS-01 Capture the UDP protocol traffic.
- WS-02 Use a stable UDP port during development.
- WS-03 Apply a display filter such as `udp.port == 8888`.
- WS-04 Verify DATA and ACK packets.
- WS-05 Inspect sequence and ACK values.
- WS-06 Observe GBN retransmission patterns.
- WS-07 Observe SR selective retransmission patterns.
- WS-08 Capture at least one GBN→SR transition.
- WS-09 Capture an SR→GBN transition if produced by the chosen experiment.

A custom Wireshark dissector exposing sequence, ACK, flags, window, checksum, and payload
fields is an **optional** presentation enhancement; payload inspection suffices initially.

### 22.1 Expected Wireshark Evidence

| Condition | Expected observation |
| --- | --- |
| Clean GBN | Sequential DATA and cumulative ACKs with few retransmissions |
| Lossy GBN | Repeated retransmissions of outstanding sequence range |
| Lossy SR | Individual retransmissions for missing sequence numbers |
| Hybrid transition | Mode-control evidence followed by corresponding retransmission behavior |

Wireshark demonstrates packet-level behavior; internal controller calculations such as the
exact loss estimate are taken from program logs.

## 23. Reproducibility

- RP-01 Every experiment has a unique run ID.
- RP-02 All configuration values are recorded.
- RP-03 Randomized loss uses a recorded seed.
- RP-04 Compared systems use identical source data and network conditions.
- RP-05 Each condition is repeated consistently.
- RP-06 Raw logs are preserved before aggregation.
- RP-07 Raw data is never manually edited.
- RP-08 Final results identify the software version/commit used.

## 24. Correctness Criteria

- CC-01 Successful transfers reconstruct exactly the source bytes.
- CC-02 Recoverable packet losses do not silently corrupt output.
- CC-03 Duplicates never produce duplicate file data.
- CC-04 Mode changes do not lose protocol state.
- CC-05 Transfer eventually completes under tested recoverable conditions.
- CC-06 Logs contain enough information to explain each final result.

## 25. Testing Requirements

### 25.1 Unit tests
Packet serialization/deserialization round trip; checksum generation and validation;
malformed packet rejection; sequence-number handling; GBN cumulative ACK processing;
GBN timeout/retransmission; SR individual ACK processing; SR out-of-order buffering;
duplicate handling; hybrid threshold evaluation; hysteresis behavior.

### 25.2 Integration tests
Small file transfer at 0% loss; transfer under moderate loss; transfer through a GBN→SR
transition; transfer through an SR→GBN transition; final hash equals source hash.

### 25.3 Failure tests
Single packet loss; multiple consecutive losses; random packet loss; ACK loss; artificial
delay; packet corruption; conditions close to switching thresholds.

## 26. Performance Hypotheses

Hypotheses to test, **not** guaranteed results:

- H-01 Under very low loss, GBN may achieve competitive goodput with simpler state management.
- H-02 As loss increases, GBN retransmission overhead is expected to increase.
- H-03 Under sufficiently lossy conditions, SR is expected to reduce unnecessary retransmissions.
- H-04 A suitable hybrid should spend more time in GBN under clean conditions and more time
  in SR under sustained loss.
- H-05 Bad thresholds or excessive switching can make the hybrid *worse* than a fixed
  strategy — an important result to measure rather than hide.

## 27. Deliverables

Graphs (all generated from recorded experiment data):
- Goodput vs packet-loss rate.
- Retransmission count vs packet-loss rate.
- Retransmission overhead vs packet-loss rate.
- Completion time vs packet-loss rate.
- Mode selection over time during dynamic loss.
- Optional: goodput vs RTT; switching frequency vs loss rate; GBN/SR residence time vs loss rate.

## 28. Final Demonstration Script

1. Start receiver.
2. Start Wireshark capture on the selected interface.
3. Start a hybrid file-transfer run.
4. Show initial GBN behavior under clean conditions.
5. Introduce or configure sustained packet loss.
6. Show the controller detecting the changed condition.
7. Show transition to SR.
8. Show selective retransmission in Wireshark.
9. Restore low-loss conditions for a reverse-transition experiment if supported.
10. Show SR→GBN after hysteresis requirements are met.
11. Stop capture.
12. Verify source and received file hashes.
13. Display metrics and compare with pure GBN and pure SR.

## 29. Definition of Done

- [ ] Arbitrary test files can be transferred reliably.
- [ ] GBN works independently.
- [ ] SR works independently.
- [ ] Hybrid switching works during a transfer.
- [ ] Mode transitions preserve correctness.
- [ ] Controlled loss and optional delay/jitter can be reproduced.
- [ ] GBN, SR, and Hybrid are compared fairly.
- [ ] Raw logs are retained.
- [ ] Graphs are generated automatically.
- [ ] Wireshark captures demonstrate protocol behavior.
- [ ] Source and received file hashes match on successful runs.
- [ ] All major configuration choices are documented.
- [ ] Known limitations and failed experiments are reported.

## 30. Research Positioning

Prior-art verification found extensive existing research on adaptive ARQ windows, hybrid
GBN/SR schemes, and adaptive RTO — specifically including hybrid GBN/SR schemes and
WORM-ARQ, which reportedly switches between SR and GBN according to transmission quality.

The same verification found many student implementations of GBN and SR separately, but no
evidence of undergraduate projects specifically implementing runtime GBN/SR switching.
This supports presenting the project as an undergraduate **implementation and evaluation**,
not a claim of global algorithmic novelty. Hybrid GBN/SR switching was ranked as the best
balance among differentiation, feasibility, and measurable results, with an explicit
caution against novelty claims.
