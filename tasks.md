# Tasks — Hybrid GBN/SR ARQ with Adaptive Switching

**Source of truth:** `Hybrid_GBN_SR_Functionality_and_Technical_Specification.docx`
**Requirements:** `specs.md` · **Design:** `design.md`

Task IDs are stable. Each task carries the requirement IDs it satisfies and an explicit
completion condition, so "done" is checkable rather than a judgement call.

**Ordering rule (from the source specification §41):** both ARQ baselines must be correct
before the hybrid controller is written. The controller must never be debugged at the same
time as the mechanism underneath it. Do not start Phase 5 with Phase 3 or 4 failing.

---

## Status snapshot

Per the source document §37, carried forward as the current baseline:

| Item | Status |
| --- | --- |
| Python environment | ✅ Completed |
| UDP sender/receiver test | ✅ Completed |
| Wireshark installation | ✅ Completed |
| Wireshark packet visibility | ✅ Completed |
| Custom packet format | ⏭️ **Next** |
| GBN | ⬜ Not started |
| SR | ⬜ Not started |
| Loss simulation | ⬜ Not started |
| Hybrid controller | ⬜ Not started |
| Experiment automation | ⬜ Not started |
| Analysis / graphs | ⬜ Not started |
| Final demonstration | ⬜ Not started |

Milestone map: **M1** ✅ · **M2** ⏭️ current · **M3–M11** pending.

---

## Phase 0 — Project skeleton (M1 completion)

- [x] **T0.1** Verify Python 3, UDP send/receive, and Wireshark packet visibility.
      *Done — `test_udp.py` + root `receiver.py` spike, packets visible on `udp.port == 8888`.*
- [x] **T0.2** Create the directory structure from `design.md` §2.1 with `__init__.py` files
      and `.gitkeep` in `logs/`, `captures/`, `plots/`.
      **Done when:** `import protocol, network` succeeds from the project root.
- [x] **T0.3** Create `config.py` holding every parameter in `specs.md` §15, with the
      **OPEN** values as named constants marked `# NOT FROZEN`.
      **Satisfies:** §15 · **Done when:** no protocol module contains a hard-coded tunable.
- [x] **T0.4** Initialize Git; commit the skeleton, the source `.docx`, and these three docs.
      **Satisfies:** RP-08 · **Done when:** `git log` has an initial commit and later runs can
      cite a commit hash.
- [x] **T0.5** Retire the M1 spike: keep `test_udp.py` as the environment smoke test, and
      note in `README.md` that root `receiver.py` is superseded by the real receiver (T2.2).
      **Done when:** no ambiguity about which receiver is the real one.

## Phase 1 — Packet layer (M2)

- [ ] **T1.1** Freeze the binary packet format: field order, sizes, endianness, packing.
      **Decides:** D1 · **Satisfies:** §5, §16.1 · **Done when:** the `struct` format string
      and offset table are written into the `packet.py` docstring and `specs.md` §5 is updated
      from "suggested" to frozen.
- [ ] **T1.2** Freeze checksum algorithm and coverage.
      **Decides:** D2 · **Satisfies:** §16.4, IN-01 · **Done when:** coverage is documented and
      a deliberately flipped bit in *any* header field is detected by a test.
- [ ] **T1.3** Freeze `SEGMENT_SIZE` (max DATA payload) and the initial sequence-number
      convention.
      **Decides:** D3, D4 · **Satisfies:** §16.2, §16.3, SEQ-01, SEQ-02
      **Done when:** values are in `config.py` and the header+payload total is confirmed under
      the path MTU so no IP fragmentation appears in captures.
- [ ] **T1.4** Implement `PacketType`, `Packet`, `encode()`, `decode()`, and the
      `PacketError` hierarchy.
      **Satisfies:** §5.1, FR-04 · **Done when:** `decode` rejects a bad packet before any
      caller can read its payload.
- [ ] **T1.5** Define the START / START_ACK / FIN / FIN_ACK / MODE control payloads.
      **Satisfies:** §5.1, §6 · **Done when:** each is round-trip encodable and documented.
- [ ] **T1.6** Unit tests: encode/decode round trip (including empty and maximum payload);
      checksum generation and validation; rejection of short, bad-MAGIC, bad-VERSION,
      unknown-TYPE, length-mismatch and corrupted packets; sequence-number handling.
      **Satisfies:** §25.1 · **Done when:** all pass and every `PacketError` subclass has a test.
- [ ] **T1.7** Replace the temporary `TEST_PACKET_n` strings with real encoded packets and
      confirm the header is readable at fixed offsets in Wireshark.
      **Satisfies:** FR-12, WS-04, WS-05 · **Done when:** a capture shows DATA with a
      readable sequence value.

**M2 complete when:** packet format, parsing, serialization, and checksum tests all pass.

## Phase 2 — Basic transfer (M3)

- [ ] **T2.1** Implement `sender.py`: CLI (`--file --host --port --mode --window`), file
      read, SHA-256 metadata, START/START_ACK, segmentation, FIN/FIN_ACK, sender state
      machine (`design.md` §7.1).
      **Satisfies:** FR-01, FR-03, §6, §14 · **Done when:** a file transfers end to end at 0% loss.
- [ ] **T2.2** Implement `receiver.py`: CLI (`--port --output`), bind, validate, in-order
      write, hash comparison, receiver state machine (`design.md` §7.2).
      **Satisfies:** FR-02, FR-10, FR-11, IN-02, IN-05 · **Done when:** output hash matches source.
- [ ] **T2.3** Implement the duplicate-write guard in the file writer (highest-delivered
      sequence check), independent of ARQ mode.
      **Satisfies:** SEQ-05, CC-03 · **Done when:** a replayed DATA packet writes nothing and logs `DUPLICATE`.
- [ ] **T2.4** Implement control-packet retry budgets and the receiver idle timeout.
      **Satisfies:** §13 (transfer timeout, receiver unavailable) · **Done when:** a sender
      started with no receiver fails with a clear error instead of hanging.
- [ ] **T2.5** Implement the event emitter and `events.csv` / `summary.json` writers with the
      exact columns of `specs.md` §21.
      **Satisfies:** FR-09, §21, CC-06 · **Done when:** a 0%-loss run produces a complete,
      parseable event log and summary.

**M3 complete when:** a file transfers correctly at 0% loss with hashes matching and logs written.

## Phase 3 — GBN (M4)

- [ ] **T3.1** Freeze GBN ACK semantics and document them.
      **Decides:** D5 · **Satisfies:** §16.5, SEQ-03, GBN-01
- [ ] **T3.2** Implement the GBN sender: `base`, `next_seq`, window, outstanding buffer,
      single oldest-packet timer.
      **Satisfies:** FR-05, FR-06, §7.1
- [ ] **T3.3** Implement cumulative ACK processing, including refusing to move `base`
      backward on stale or duplicate ACKs.
      **Satisfies:** GBN-02, GBN-03
- [ ] **T3.4** Implement GBN timeout retransmission of the full outstanding range, with
      `RETX` logging carrying sequence, mode, and reason.
      **Satisfies:** GBN-04, GBN-05, TO-02, TO-03, TO-05
- [ ] **T3.5** Implement the GBN receiver (single `expected_seq`, no buffering, re-ACK on
      out-of-order).
      **Satisfies:** §7, FR-10
- [ ] **T3.6** Unit tests: cumulative ACK processing; base non-regression; timeout
      retransmission of the correct range.
      **Satisfies:** §25.1
- [ ] **T3.7** Integration test: GBN at 0% loss, hash match.
      **Satisfies:** §25.2

**M4 complete when:** GBN retransmission tests pass. *(Controlled-loss GBN validation is
T4.6 — it needs the simulator.)*

## Phase 4 — Impairment and SR (M5, M6)

Ordered so the simulator lands before SR, giving SR a loss-capable test bed from the start.

- [ ] **T4.1** Implement `network/simulator.py`: `ImpairedSocket` with loss, one-way
      delay, jitter, a dedicated seeded RNG, and non-blocking delayed delivery.
      **Satisfies:** §17.1, §17.2, RP-03 · **Done when:** the same seed reproduces an identical
      drop sequence, and 0% loss / 0 delay is behaviourally identical to a raw socket.
- [ ] **T4.2** Add ACK-direction impairment and the `loss_schedule` step function for
      dynamic conditions.
      **Satisfies:** §17.3, §25.3 · **Done when:** a scheduled loss change is visible in the event log.
- [ ] **T4.3** Log `DROP` events distinctly from `CHECKSUM_FAIL`.
      **Satisfies:** IN-04
- [ ] **T4.4** Freeze SR ACK semantics and document them.
      **Decides:** D6 · **Satisfies:** §16.6, SEQ-03
- [ ] **T4.5** Implement the SR sender: per-segment ACK tracking, per-segment timers,
      retention of unacked payloads, window boundaries, single-segment retransmission.
      **Satisfies:** FR-07, SR-01, SR-02, SR-03, SR-10
- [ ] **T4.6** Implement the SR receiver: receive window, out-of-order buffering, individual
      ACKs, in-order delivery on gap fill, duplicate suppression.
      **Satisfies:** SR-04 … SR-09
- [ ] **T4.7** Unit tests: SR individual ACK processing; out-of-order buffering and
      gap-fill delivery; duplicate handling; the half-sequence-space window assertion.
      **Satisfies:** §25.1
- [ ] **T4.8** Failure tests against **both** baselines: single loss, multiple consecutive
      losses, random loss, ACK loss, artificial delay, packet corruption.
      **Satisfies:** §25.3, CC-02, CC-05, IN-03 · **Done when:** every case still produces a
      matching hash or a clearly reported failure — never silent corruption.
- [ ] **T4.9** Freeze the baseline RTO policy and the primary window size.
      **Decides:** D7, D11 · **Satisfies:** §16.7, §16.8, §11, TO-01, TO-04

**M5 complete when:** SR buffering and individual-retransmission tests pass.
**M6 complete when:** controlled loss is reproducible from a recorded seed.

## Phase 5 — Hybrid controller (M7)

**Do not start until T3.7 and T4.8 pass for both baselines.**

- [ ] **T5.1** Implement the statistics collector for every item in `specs.md` §10
      (transmissions, unique DATA, ACKed, retransmissions, loss indicator, mode, switch
      count, residence times, RTT samples).
      **Satisfies:** G-04, §10, HY-01
- [ ] **T5.2** Implement and **freeze** the loss estimator.
      **Decides:** D8 · **Satisfies:** §10, §16.9 · **Done when:** the formula and its known
      GBN bias are documented, and no later change is made without re-running all experiments.
- [ ] **T5.3** Implement the threshold decision rule with dual thresholds, the
      consecutive-confirmation counter, and a minimum residence time.
      **Satisfies:** FR-08, HY-02, HY-03, HY-09
- [ ] **T5.4** Implement the MODE handshake transition: quiesce the window, exchange MODE
      with epoch and `effective_from_seq`, rebuild strategies from exported transfer state,
      abandon safely on handshake failure.
      **Decides:** D10, D12(mechanism) · **Satisfies:** §16.12, HY-04 … HY-08, §13 (mode inconsistency)
      **Done when:** a switch mid-transfer preserves every unacked segment and the final hash matches.
- [ ] **T5.5** Log `SWITCH` events with timestamp, old/new mode, loss estimate, reason, epoch.
      **Satisfies:** FR-09, HY-08
- [ ] **T5.6** Add `--mode fixed-hybrid` (controller and MODE machinery active, switching
      disabled) as the switching-overhead control.
      **Satisfies:** §18
- [ ] **T5.7** Unit tests: threshold evaluation at, just below, and just above each
      threshold; hysteresis suppresses a single-observation spike; a stale MODE echo is
      discarded; a failed handshake leaves the transfer in a valid single mode.
      **Satisfies:** §25.1
- [ ] **T5.8** Integration tests: transfer through GBN→SR; transfer through SR→GBN; hash
      matches in both; a transfer held near the threshold does not oscillate.
      **Satisfies:** §25.2, §25.3, CC-04

**M7 complete when:** runtime switching works with no corruption across repeated switches.

## Phase 6 — Logging completeness (M8)

- [ ] **T6.1** Audit every event in the `design.md` §9 vocabulary against the code; add any
      missing emission point.
      **Satisfies:** §21
- [ ] **T6.2** Confirm every metric in `specs.md` §20 is computable from `events.csv` alone,
      with no in-memory-only value.
      **Satisfies:** FR-14, CC-06
- [ ] **T6.3** Record the full frozen config, seed, file size, and software version/commit in
      `summary.json`.
      **Satisfies:** RP-01, RP-02, RP-08

**M8 complete when:** automated event logs are generated for every run without manual steps.

## Phase 7 — Threshold calibration

- [ ] **T7.1** Sweep `SWITCH_HIGH` / `SWITCH_LOW` / `HYSTERESIS_COUNT` over the loss grid and
      record switch counts, residence times, and goodput per setting.
      **Satisfies:** HY-02, §16.10, §16.11
- [ ] **T7.2** Freeze the chosen thresholds and hysteresis rule; record the calibration
      evidence that justified them.
      **Decides:** D9 · **Done when:** thresholds are in `config.py` and unchanged thereafter.
- [ ] **T7.3** Note any setting that made the hybrid *worse* than a fixed strategy — this is
      a reportable result, not a bug to bury.
      **Satisfies:** H-05

## Phase 8 — Experiments (M9)

- [ ] **T8.1** Freeze the remaining experimental decisions: repetition count, random-seed
      policy, file size(s).
      **Decides:** D12, D13, D14 · **Satisfies:** §16.13, §16.14, §16.15
- [ ] **T8.2** Implement `experiments/run_experiment.py`: config-driven runs, `run_id`
      generation, per-trial seed derivation, receiver/sender orchestration, abort timeout,
      config capture.
      **Satisfies:** FR-13, §19, RP-01, RP-02
- [ ] **T8.3** Write `experiments/configs/` for E1–E8, with identical file, segment size,
      window, and impairment procedure across GBN / SR / Hybrid within each cell.
      **Satisfies:** §18, §19, RP-04
- [ ] **T8.4** Run E1–E6 (loss sweep at fixed RTT) × 3 systems × N trials.
      **Satisfies:** G-08, §19
- [ ] **T8.5** Run E7 (fixed loss, RTT sweep 10/50/100/500 ms) × 3 systems.
      **Satisfies:** §17.2, §19
- [ ] **T8.6** Run E8 (dynamic loss via `loss_schedule`), hybrid, covering loss-increases,
      loss-decreases, and random-change.
      **Satisfies:** §17.3, §19
- [ ] **T8.7** Verify every run's integrity result; preserve raw logs untouched before any
      aggregation.
      **Satisfies:** IN-05, RP-06, RP-07, CC-01

**M9 complete when:** baseline and hybrid experiments run reproducibly end to end.

## Phase 9 — Analysis, graphs, captures (M10)

- [ ] **T9.1** Implement `experiments/analyze_results.py`: load all runs with pandas,
      aggregate per condition with across-trial spread, exclude integrity failures from
      goodput and report them separately.
      **Satisfies:** FR-14, §20
- [ ] **T9.2** Generate the required graphs into `plots/`: goodput vs loss; retransmission
      count vs loss; retransmission overhead vs loss; completion time vs loss; mode selection
      over time under dynamic loss.
      **Satisfies:** §27 · **Done when:** every plot is produced from recorded data by script,
      with no manual editing.
- [ ] **T9.3** Optional graphs if time allows: goodput vs RTT; switching frequency vs loss;
      GBN/SR residence time vs loss.
      **Satisfies:** §27
- [ ] **T9.4** Capture the Wireshark evidence set into `captures/`: clean GBN; lossy GBN
      range retransmission; lossy SR individual retransmission; a GBN→SR transition; an
      SR→GBN transition if the experiment produces one.
      **Satisfies:** G-07, WS-01 … WS-09, §22.1
- [ ] **T9.5** Optional: Lua dissector exposing named header fields. Presentation nicety only.
      **Satisfies:** §22 (optional)

**M10 complete when:** graphs and Wireshark captures together demonstrate the protocol behavior.

## Phase 10 — Finalization (M11)

- [ ] **T10.1** Write `README.md`: install, run sender/receiver, reproduce an experiment,
      regenerate graphs.
- [ ] **T10.2** Record every frozen decision from `specs.md` §16 with its final value and the
      rationale; update `design.md` §12 from Proposed to Frozen.
      **Satisfies:** §29 ("all major configuration choices are documented")
- [ ] **T10.3** Write up the results against hypotheses H-01 … H-05, stating explicitly which
      were supported and which were not.
      **Satisfies:** §26
- [ ] **T10.4** Document known limitations and failed experiments, including the loss
      estimator's GBN bias and the per-switch drain cost.
      **Satisfies:** §29
- [ ] **T10.5** State the research positioning: implementation and evaluation, with no claim of
      global novelty (`specs.md` §30).
      **Satisfies:** §3, §30
- [ ] **T10.6** Rehearse the final demonstration end to end, in order, per `specs.md` §28:
      receiver → capture → hybrid run → GBN under clean → induce loss → detection → SR
      transition → selective retransmission in Wireshark → restore low loss → SR→GBN after
      hysteresis → stop capture → hash verification → metrics vs pure GBN and pure SR.
      **Satisfies:** §28
- [ ] **T10.7** Walk the Definition of Done checklist (`specs.md` §29) and check off every item.

**M11 complete when:** code, tests, results, documentation, and presentation are all complete.

---

## Milestone reference

| Milestone | Completion condition | Tasks |
| --- | --- | --- |
| M1 Environment | Python, UDP, and Wireshark verified | T0.1 |
| M2 Packet layer | Packet format, parsing, serialization, checksum tests pass | T1.1–T1.7 |
| M3 Basic transfer | File transfers correctly at 0% loss | T2.1–T2.5 |
| M4 GBN | GBN retransmission tests pass | T3.1–T3.7 |
| M5 SR | SR buffering and individual retransmission tests pass | T4.4–T4.8 |
| M6 Impairment | Controlled loss is reproducible | T4.1–T4.3 |
| M7 Hybrid | Runtime switching works without corruption | T5.1–T5.8 |
| M8 Logging | Automated event logs are generated | T6.1–T6.3 |
| M9 Evaluation | Baseline and hybrid experiments run reproducibly | T8.1–T8.7 |
| M10 Visualization | Graphs and Wireshark captures demonstrate behavior | T9.1–T9.5 |
| M11 Finalization | Code, tests, results, documentation, presentation complete | T10.1–T10.7 |

## Immediate next steps

The source specification's implementation sequence (§41), mapped to task IDs:

1. T0.2 – T0.3 — skeleton and `config.py`.
2. T1.1 – T1.3 — **freeze** the binary packet format, checksum, segment size, sequence convention.
3. T1.4 – T1.5 — serialization, parsing, control payloads.
4. T1.6 — packet-layer unit tests.
5. T1.7 — replace the `TEST_PACKET` strings with the real format.
6. T3.* — pure GBN, tested at zero loss.
7. T4.1 – T4.3 — the loss simulator, then GBN under controlled loss.
8. T4.4 – T4.8 — pure SR, tested at zero and controlled loss.
9. **Only then** T5.* — hybrid switching.
10. T8.* — automated experiment execution.
11. T9.* — analysis and visualization.
