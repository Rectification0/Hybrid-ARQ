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
| Custom packet format | ✅ Completed |
| GBN | ✅ Completed |
| SR | ✅ Completed |
| Loss simulation | ✅ Completed |
| Hybrid controller | ✅ Completed |
| Experiment automation | ✅ Completed |
| Analysis / graphs | ✅ Completed |
| Final demonstration | ✅ Completed |
| Frontend dashboard | ⏭️ **Next** |

Milestone map: **M1** ✅ · **M2** ✅ · **M3** ✅ · **M4** ✅ · **M5** ✅ · **M6** ✅ · **M7** ✅ · **M8** ✅ · Phase 7 ✅ · **M9** ✅ · **M10** ✅ · **M11** ✅ · **M12** ⬜ — the protocol project is complete; Phase 11 adds a presentation layer over it.

---

## Phase 0 — Project skeleton (M1 completion)

- [x] **T0.1** Verify Python 3, UDP send/receive, and Wireshark packet visibility.
      *Done — the M1 spike (since renamed `smoke_udp.py` + `test/spike_receiver.py`),
      packets visible on `udp.port == 8888`.*
- [x] **T0.2** Create the directory structure from `design.md` §2.1 with `__init__.py` files
      and `.gitkeep` in `logs/`, `captures/`, `plots/`.
      **Done when:** `import protocol, network` succeeds from the project root.
- [x] **T0.3** Create `config.py` holding every parameter in `specs.md` §15, with the
      **OPEN** values as named constants marked `# NOT FROZEN`.
      **Satisfies:** §15 · **Done when:** no protocol module contains a hard-coded tunable.
- [x] **T0.4** Initialize Git; commit the skeleton, the source `.docx`, and these three docs.
      **Satisfies:** RP-08 · **Done when:** `git log` has an initial commit and later runs can
      cite a commit hash.
- [x] **T0.5** Retire the M1 spike: keep the smoke test, and note in `README.md` that root
      `receiver.py` is superseded by the real receiver (T2.2).
      **Done when:** no ambiguity about which receiver is the real one.
      *Done — the spike listener moved to `test/spike_receiver.py` rather than being left at
      `receiver.py` to be overwritten, and `test_udp.py` was renamed `smoke_udp.py` so pytest
      does not collect a runnable script as a test module.*

## Phase 1 — Packet layer (M2)

- [x] **T1.1** Freeze the binary packet format: field order, sizes, endianness, packing.
      **Decides:** D1 · **Satisfies:** §5, §16.1 · **Done when:** the `struct` format string
      and offset table are written into the `packet.py` docstring and `specs.md` §5 is updated
      from "suggested" to frozen.
- [x] **T1.2** Freeze checksum algorithm and coverage.
      **Decides:** D2 · **Satisfies:** §16.4, IN-01 · **Done when:** coverage is documented and
      a deliberately flipped bit in *any* header field is detected by a test.
- [x] **T1.3** Freeze `SEGMENT_SIZE` (max DATA payload) and the initial sequence-number
      convention.
      **Decides:** D3, D4 · **Satisfies:** §16.2, §16.3, SEQ-01, SEQ-02
      **Done when:** values are in `config.py` and the header+payload total is confirmed under
      the path MTU so no IP fragmentation appears in captures.
- [x] **T1.4** Implement `PacketType`, `Packet`, `encode()`, `decode()`, and the
      `PacketError` hierarchy.
      **Satisfies:** §5.1, FR-04 · **Done when:** `decode` rejects a bad packet before any
      caller can read its payload.
- [x] **T1.5** Define the START / START_ACK / FIN / FIN_ACK / MODE control payloads.
      **Satisfies:** §5.1, §6 · **Done when:** each is round-trip encodable and documented.
- [x] **T1.6** Unit tests: encode/decode round trip (including empty and maximum payload);
      checksum generation and validation; rejection of short, bad-MAGIC, bad-VERSION,
      unknown-TYPE, length-mismatch and corrupted packets; sequence-number handling.
      **Satisfies:** §25.1 · **Done when:** all pass and every `PacketError` subclass has a test.
- [x] **T1.7** Replace the temporary `TEST_PACKET_n` strings with real encoded packets and
      confirm the header is readable at fixed offsets in Wireshark.
      **Satisfies:** FR-12, WS-04, WS-05 · **Done when:** a capture shows DATA with a
      readable sequence value.

**M2 complete when:** packet format, parsing, serialization, and checksum tests all pass.

## Phase 2 — Basic transfer (M3)

- [x] **T2.1** Implement `sender.py`: CLI (`--file --host --port --mode --window`), file
      read, SHA-256 metadata, START/START_ACK, segmentation, FIN/FIN_ACK, sender state
      machine (`design.md` §7.1).
      **Satisfies:** FR-01, FR-03, §6, §14 · **Done when:** a file transfers end to end at 0% loss.
- [x] **T2.2** Implement `receiver.py`: CLI (`--port --output`), bind, validate, in-order
      write, hash comparison, receiver state machine (`design.md` §7.2).
      **Satisfies:** FR-02, FR-10, FR-11, IN-02, IN-05 · **Done when:** output hash matches source.
- [x] **T2.3** Implement the duplicate-write guard in the file writer (highest-delivered
      sequence check), independent of ARQ mode.
      **Satisfies:** SEQ-05, CC-03 · **Done when:** a replayed DATA packet writes nothing and logs `DUPLICATE`.
- [x] **T2.4** Implement control-packet retry budgets and the receiver idle timeout.
      **Satisfies:** §13 (transfer timeout, receiver unavailable) · **Done when:** a sender
      started with no receiver fails with a clear error instead of hanging.
- [x] **T2.5** Implement the event emitter and `events.csv` / `summary.json` writers with the
      exact columns of `specs.md` §21.
      **Satisfies:** FR-09, §21, CC-06 · **Done when:** a 0%-loss run produces a complete,
      parseable event log and summary.

**M3 complete when:** a file transfers correctly at 0% loss with hashes matching and logs written.
*Done — 1 MiB over loopback, hashes match, events.csv + summary.json on both endpoints.
Phase 2 transfers with a stop-and-wait placeholder strategy (`protocol/strategy.py`);
GBN replaces it in T3.2 without touching `sender.py`.*

## Phase 3 — GBN (M4)

- [x] **T3.1** Freeze GBN ACK semantics and document them.
      **Decides:** D5 · **Satisfies:** §16.5, SEQ-03, GBN-01
- [x] **T3.2** Implement the GBN sender: `base`, `next_seq`, window, outstanding buffer,
      single oldest-packet timer.
      **Satisfies:** FR-05, FR-06, §7.1
- [x] **T3.3** Implement cumulative ACK processing, including refusing to move `base`
      backward on stale or duplicate ACKs.
      **Satisfies:** GBN-02, GBN-03
- [x] **T3.4** Implement GBN timeout retransmission of the full outstanding range, with
      `RETX` logging carrying sequence, mode, and reason.
      **Satisfies:** GBN-04, GBN-05, TO-02, TO-03, TO-05
- [x] **T3.5** Implement the GBN receiver (single `expected_seq`, no buffering, re-ACK on
      out-of-order).
      **Satisfies:** §7, FR-10
- [x] **T3.6** Unit tests: cumulative ACK processing; base non-regression; timeout
      retransmission of the correct range.
      **Satisfies:** §25.1
- [x] **T3.7** Integration test: GBN at 0% loss, hash match.
      **Satisfies:** §25.2

**M4 complete when:** GBN retransmission tests pass. *(Controlled-loss GBN validation is
T4.6 — it needs the simulator.)*
*Done — D5 frozen, GBN implemented behind the shared strategy interface, 1 MiB over
loopback with 8 segments in flight and a matching hash. Loss recovery is verified by
driving a drop directly against the strategy; the networked version is T4.8.*

## Phase 4 — Impairment and SR (M5, M6)

Ordered so the simulator lands before SR, giving SR a loss-capable test bed from the start.

- [x] **T4.1** Implement `network/simulator.py`: `ImpairedSocket` with loss, one-way
      delay, jitter, a dedicated seeded RNG, and non-blocking delayed delivery.
      **Satisfies:** §17.1, §17.2, RP-03 · **Done when:** the same seed reproduces an identical
      drop sequence, and 0% loss / 0 delay is behaviourally identical to a raw socket.
- [x] **T4.2** Add ACK-direction impairment and the `loss_schedule` step function for
      dynamic conditions.
      **Satisfies:** §17.3, §25.3 · **Done when:** a scheduled loss change is visible in the event log.
- [x] **T4.3** Log `DROP` events distinctly from `CHECKSUM_FAIL`.
      **Satisfies:** IN-04
- [x] **T4.4** Freeze SR ACK semantics and document them.
      **Decides:** D6 · **Satisfies:** §16.6, SEQ-03
- [x] **T4.5** Implement the SR sender: per-segment ACK tracking, per-segment timers,
      retention of unacked payloads, window boundaries, single-segment retransmission.
      **Satisfies:** FR-07, SR-01, SR-02, SR-03, SR-10
- [x] **T4.6** Implement the SR receiver: receive window, out-of-order buffering, individual
      ACKs, in-order delivery on gap fill, duplicate suppression.
      **Satisfies:** SR-04 … SR-09
- [x] **T4.7** Unit tests: SR individual ACK processing; out-of-order buffering and
      gap-fill delivery; duplicate handling; the half-sequence-space window assertion.
      **Satisfies:** §25.1
- [x] **T4.8** Failure tests against **both** baselines: single loss, multiple consecutive
      losses, random loss, ACK loss, artificial delay, packet corruption.
      **Satisfies:** §25.3, CC-02, CC-05, IN-03 · **Done when:** every case still produces a
      matching hash or a clearly reported failure — never silent corruption.
- [x] **T4.9** Freeze the baseline RTO policy and the primary window size.
      **Decides:** D7, D11 · **Satisfies:** §16.7, §16.8, §11, TO-01, TO-04

**M5 complete when:** SR buffering and individual-retransmission tests pass.
**M6 complete when:** controlled loss is reproducible from a recorded seed.
*Done — D6, D7 and D11 frozen. Both baselines survive every T4.8 failure case with
matching hashes. At 10% loss / 5% ACK loss with the same seed over 1 MiB, GBN resent 883
segments and SR 167 — the trade-off the hybrid is built to exploit, measured rather than
assumed. A recorded seed replays a transfer to the same retransmission count.*

## Phase 5 — Hybrid controller (M7)

**Do not start until T3.7 and T4.8 pass for both baselines.**

- [x] **T5.1** Implement the statistics collector for every item in `specs.md` §10
      (transmissions, unique DATA, ACKed, retransmissions, loss indicator, mode, switch
      count, residence times, RTT samples).
      **Satisfies:** G-04, §10, HY-01
- [x] **T5.2** Implement and **freeze** the loss estimator.
      **Decides:** D8 · **Satisfies:** §10, §16.9 · **Done when:** the formula and its known
      GBN bias are documented, and no later change is made without re-running all experiments.
- [x] **T5.3** Implement the threshold decision rule with dual thresholds, the
      consecutive-confirmation counter, and a minimum residence time.
      **Satisfies:** FR-08, HY-02, HY-03, HY-09
- [x] **T5.4** Implement the MODE handshake transition: quiesce the window, exchange MODE
      with epoch and `effective_from_seq`, rebuild strategies from exported transfer state,
      abandon safely on handshake failure.
      **Decides:** D10, D12(mechanism) · **Satisfies:** §16.12, HY-04 … HY-08, §13 (mode inconsistency)
      **Done when:** a switch mid-transfer preserves every unacked segment and the final hash matches.
- [x] **T5.5** Log `SWITCH` events with timestamp, old/new mode, loss estimate, reason, epoch.
      **Satisfies:** FR-09, HY-08
- [x] **T5.6** Add `--mode fixed-hybrid` (controller and MODE machinery active, switching
      disabled) as the switching-overhead control.
      **Satisfies:** §18
- [x] **T5.7** Unit tests: threshold evaluation at, just below, and just above each
      threshold; hysteresis suppresses a single-observation spike; a stale MODE echo is
      discarded; a failed handshake leaves the transfer in a valid single mode.
      **Satisfies:** §25.1
- [x] **T5.8** Integration tests: transfer through GBN→SR; transfer through SR→GBN; hash
      matches in both; a transfer held near the threshold does not oscillate.
      **Satisfies:** §25.2, §25.3, CC-04

**M7 complete when:** runtime switching works with no corruption across repeated switches.
*Done — D8 frozen (the estimator, with its GBN bias recorded at freeze time rather than
reconstructed later) and D10 frozen (the MODE handshake at a quiescent window). The
controller decides and the endpoints negotiate; neither reaches into the other. A switch
mid-transfer preserves every unacknowledged segment by construction — payloads live in the
transfer state, not in the strategy — and both directions of switch finish with a matching
hash under loss. A handshake that cannot be negotiated is abandoned and the transfer
completes in its existing mode; a stale epoch is discarded rather than replayed. 446 tests
pass. **D9 is deliberately still open**: Phase 7 calibrates the thresholds against real
data, and the Phase 5 tests pin the rule rather than the numbers so calibration does not
have to rewrite them.*

## Phase 6 — Logging completeness (M8)

- [x] **T6.1** Audit every event in the `design.md` §9 vocabulary against the code; add any
      missing emission point.
      **Satisfies:** §21
- [x] **T6.2** Confirm every metric in `specs.md` §20 is computable from `events.csv` alone,
      with no in-memory-only value.
      **Satisfies:** FR-14, CC-06
- [x] **T6.3** Record the full frozen config, seed, file size, and software version/commit in
      `summary.json`.
      **Satisfies:** RP-01, RP-02, RP-08

**M8 complete when:** automated event logs are generated for every run without manual steps.
*Done — the audit found four real gaps, not a clean bill of health: the receiver never
logged its idle timeout, a receiver that never saw a START wrote no log at all, packets
rejected before START were dropped from the record, and retransmission overhead and goodput
were **not** in fact derivable from `events.csv` because nothing recorded a byte count. The
`bytes` column (specs.md §21) closes the last one; `metrics.py` derives all of §20 from event
rows and nothing else, and the tests assert it agrees with what the endpoints computed while
running. `summary.json` now records the configuration each run actually used — seed,
impairment, derived RTO, file size, swept thresholds — plus the freeze state of every D1–D14
decision and the commit that produced it. 494 tests pass.*

## Phase 7 — Threshold calibration

- [x] **T7.1** Sweep `SWITCH_HIGH` / `SWITCH_LOW` / `HYSTERESIS_COUNT` over the loss grid and
      record switch counts, residence times, and goodput per setting.
      **Satisfies:** HY-02, §16.10, §16.11
- [x] **T7.2** Freeze the chosen thresholds and hysteresis rule; record the calibration
      evidence that justified them.
      **Decides:** D9 · **Done when:** thresholds are in `config.py` and unchanged thereafter.
- [x] **T7.3** Note any setting that made the hybrid *worse* than a fixed strategy — this is
      a reportable result, not a bug to bury.
      **Satisfies:** H-05

**Phase 7 complete.** *D9 frozen at `SWITCH_HIGH = 0.10`, `SWITCH_LOW = 0.02`,
`HYSTERESIS_COUNT = 3` from 444 recorded transfers
(`experiments/results/calibration.md`, `calibration_runs.csv`). Three findings the
recommendation did not anticipate: the thresholds are readings of the **estimator**, not
loss rates — D8 over-reads loss under GBN by 4–5×, so 0.10 fires at about 2% physical loss;
`SWITCH_HIGH` is inert across 0.05–0.20 and `SWITCH_LOW` across 0.01–0.10, so the dead band's
width comes from the estimator's mode dependence rather than the gap between the numbers; and
the hysteresis count is the only real lever on oscillation, measurable only against a
condition that changes. A count of 1 scored best under static and falling loss and would have
been frozen had the oscillating case not been run — it made 60% more switches than the
condition justified. T7.3's findings are `calibration.md` §5: the hybrid never beats pure SR
at any static loss level, is 24% worse than SR at 1% loss, and costs ~21% at 0% loss for
monitoring alone; it beats GBN from 2% loss up (to 1.32×) and completed every 20%-loss trial
where pure GBN aborted one.*

## Phase 8 — Experiments (M9)

- [x] **T8.1** Freeze the remaining experimental decisions: repetition count, random-seed
      policy, file size(s).
      **Decides:** D12, D13, D14 · **Satisfies:** §16.13, §16.14, §16.15
- [x] **T8.2** Implement `experiments/run_experiment.py`: config-driven runs, `run_id`
      generation, per-trial seed derivation, receiver/sender orchestration, abort timeout,
      config capture.
      **Satisfies:** FR-13, §19, RP-01, RP-02
- [x] **T8.3** Write `experiments/configs/` for E1–E8, with identical file, segment size,
      window, and impairment procedure across GBN / SR / Hybrid within each cell.
      **Satisfies:** §18, §19, RP-04
- [x] **T8.4** Run E1–E6 (loss sweep at fixed RTT) × 3 systems × N trials.
      **Satisfies:** G-08, §19 · *120 runs — four systems, since `fixed-hybrid` is what
      separates adaptation from monitoring overhead.*
- [x] **T8.5** Run E7 (fixed loss, RTT sweep 10/50/100/500 ms) × 3 systems.
      **Satisfies:** §17.2, §19 · *60 runs.*
- [x] **T8.6** Run E8 (dynamic loss via `loss_schedule`), hybrid, covering loss-increases,
      loss-decreases, and random-change.
      **Satisfies:** §17.3, §19 · *15 runs.*
- [x] **T8.7** Verify every run's integrity result; preserve raw logs untouched before any
      aggregation.
      **Satisfies:** IN-05, RP-06, RP-07, CC-01 · *195/195 hashes matched; the index records
      a SHA-256 per raw event log and `--verify` recomputes all of them.*

**M9 complete when:** baseline and hybrid experiments run reproducibly end to end.
*Done — 195 runs, every one with a matching hash, written up in
`experiments/results/experiments.md` with `experiment_runs.csv` as the record. D12 (5 trials),
D13 (one base seed, per-trial derivation, system deliberately not an input) and D14 (1 MiB for
the whole matrix) were frozen against a measured pilot rather than against the recommendation
they had carried since design.md was written — which is also why D14's proposed 10 MiB second
size is recorded as **not run**, with the reason, instead of quietly dropped. All fourteen
decisions are now frozen.*

*Four results the matrix produced that the calibration could not:*

1. *The hybrid reaches **2.00× pure GBN at 20% loss** and stays within 4–9% of pure SR — but
   **never beats SR** in any of the ten static cells.*
2. *The switching decision is **RTT-invariant** across a fiftyfold sweep (1.22–1.25× GBN at
   every RTT), which the calibration argued from the estimator's definition but could not
   measure.*
3. ***The hybrid oscillates at 1% and 2% loss** — 5.0 and 4.4 switches where one is justified,
   and at 1% it is worse than both baselines. The estimator reads 0.16–0.30 under GBN and 0.00
   under SR at the same physical loss, so the dead band sits *between* the two modes' scales
   rather than separating them. The 256 KiB calibration transfers ended before a second
   crossing could form; the frozen 1 MiB transfer shows it. D9 stays frozen — changing it
   invalidates 444 calibration transfers and all 195 runs here — and this is recorded as a
   limitation for T10.4.*
4. *The MODE drain costs **≈0.17 s, about 1.5 RTT**, measured directly by the `fixed-hybrid`
   control, which performs the same handshakes while changing nothing.*

## Phase 9 — Analysis, graphs, captures (M10)

The data is already recorded: `experiments/results/experiment_runs.csv` (195 runs) and the raw
logs under `logs/experiments/`. Phase 9 reads them and never re-runs them (RP-07).

- [x] **T9.1** Implement `experiments/analyze_results.py`: load all runs with pandas,
      aggregate per condition with across-trial spread, exclude integrity failures from
      goodput and report them separately.
      **Satisfies:** FR-14, §20 · *39 cells in `aggregate.csv`, mean and sd across the five
      trials. `--check-logs` re-derives all 195 runs from `events.csv` and confirms they
      match the index exactly, so the aggregation still rests on the logs (CC-06).*
- [x] **T9.2** Generate the required graphs into `plots/`: goodput vs loss; retransmission
      count vs loss; retransmission overhead vs loss; completion time vs loss; mode selection
      over time under dynamic loss.
      **Satisfies:** §27 · **Done when:** every plot is produced from recorded data by script,
      with no manual editing.
- [x] **T9.3** Optional graphs if time allows: goodput vs RTT; switching frequency vs loss;
      GBN/SR residence time vs loss.
      **Satisfies:** §27 · *All three drawn; `switching_frequency_vs_loss.png` is where the
      1–2% oscillation is visible as a shape rather than a footnote.*
- [x] **T9.4** Capture the Wireshark evidence set into `captures/`: clean GBN; lossy GBN
      range retransmission; lossy SR individual retransmission; a GBN→SR transition; an
      SR→GBN transition if the experiment produces one.
      **Satisfies:** G-07, WS-01 … WS-09, §22.1 · *Five captures, scripted by
      `experiments/capture_evidence.py` so each can be retaken and compared rather than
      taken on trust. `captures/README.md` records what each one holds, counted from the
      capture itself.*
- [x] **T9.5** Optional: Lua dissector exposing named header fields. Presentation nicety only.
      **Satisfies:** §22 (optional) · *`tools/hybrid_arq.lua`. It earned its place beyond
      presentation: the capture summaries are counted through it, so the evidence is
      queried rather than eyeballed.*

**M10 complete when:** graphs and Wireshark captures together demonstrate the protocol behavior.
*Done — eight figures in `plots/` (`experiments/results/figures.md` indexes them) and five
captures in `captures/`, all regenerable by script from data that was never re-run. Three
things the figures say that the tables did not:*

1. *In `retransmissions_vs_loss.png` **GBN and fixed-hybrid are one line**. The control lying
   exactly on the baseline is the strongest available statement that the hybrid's advantage
   comes from changing mode and from nothing else the controller does.*
2. *In `switching_frequency_vs_loss.png` the switch count **peaks at 1–2% loss**, where the
   hybrid's benefit is lowest, with a spread (±1.4, ±2.3) as large as the effect. The
   oscillation of T8.4 has a shape.*
3. *The two lossy captures, taken from one seeded drop stream, show the GBN/SR difference on
   the wire: **GBN puts 62 redundant copies on it and SR puts none**. A resent segment that
   was dropped never reaches the wire, so every duplicate sequence in a capture is a segment
   that arrived and was sent again anyway — which is Go-Back-N's cost, made literal.*

## Phase 10 — Finalization (M11)

- [x] **T10.1** Write `README.md`: install, run sender/receiver, reproduce an experiment,
      regenerate graphs.
      *Setup moved above the run instructions, since a reader needs it first; a
      **Reproducing the results** section covers the matrix, the aggregate and graphs, the
      capture set and the demonstration, one command each.*
- [x] **T10.2** Record every frozen decision from `specs.md` §16 with its final value and the
      rationale; update `design.md` §12 from Proposed to Frozen.
      **Satisfies:** §29 ("all major configuration choices are documented")
      *All fourteen carry their value and the rationale recorded **at freeze time**. The
      DECISION blocks in the body of `design.md` now carry the same marker as the §12 table,
      so a reader arriving mid-document is not left thinking a settled decision is still a
      proposal.*
- [x] **T10.3** Write up the results against hypotheses H-01 … H-05, stating explicitly which
      were supported and which were not.
      **Satisfies:** §26 · *`RESULTS.md` §1. All five supported — but H-05 was supported by
      the shipped configuration rather than by a deliberately bad one, which is a sharper
      result than the hypothesis anticipated.*
- [x] **T10.4** Document known limitations and failed experiments, including the loss
      estimator's GBN bias and the per-switch drain cost.
      **Satisfies:** §29 · *`RESULTS.md` §2, including the one the project would most like to
      forget: the dead band sits **between the two modes' scales** rather than between two
      loss levels, which is why the hybrid oscillates at 1–2% loss and why widening the band
      would not fix it.*
- [x] **T10.5** State the research positioning: implementation and evaluation, with no claim of
      global novelty (`specs.md` §30).
      **Satisfies:** §3, §30 · *`RESULTS.md` §3.*
- [x] **T10.6** Rehearse the final demonstration end to end, in order, per `specs.md` §28:
      receiver → capture → hybrid run → GBN under clean → induce loss → detection → SR
      transition → selective retransmission in Wireshark → restore low loss → SR→GBN after
      hysteresis → stop capture → hash verification → metrics vs pure GBN and pure SR.
      **Satisfies:** §28 · *Scripted as `experiments/demonstrate.py` and run: all thirteen
      steps in order, transcript in `experiments/results/demonstration.md`, capture in
      `captures/demonstration.pcapng`. The condition and seed are fixed in the source, so the
      transcript reports whatever happened rather than the best of several attempts.*
- [x] **T10.7** Walk the Definition of Done checklist (`specs.md` §29) and check off every item.
      *Every box now names its evidence; a checklist ticked from memory is not a checklist.*

**M11 complete when:** code, tests, results, documentation, and presentation are all complete.
*Done. The rehearsal is the part worth recording honestly: the controller detected a change
it was not told about, switched to SR 5.7 s later, returned to GBN 1.6 s after the condition
reverted, and delivered a file whose hash matched — but finished at only 1.03× pure GBN and
0.96× pure SR on that condition, because it is lossy for just 12 s of a ~25 s transfer and
the hybrid pays two drains neither baseline pays. The transcript says so. The performance
claim belongs to the matrix (2.00× pure GBN at 20% loss), and the demonstration is evidence
for the mechanism.*

---

## Phase 11 — Frontend / interactive dashboard (M12)

**The frontend is a presentation and control layer over the finished system. It is not a
replacement for the CLI, the protocol modules, the experiment runner or Wireshark**, and the
project stays what it is: a UDP file-transfer protocol with GBN, SR and runtime adaptive
switching. Phase 11 makes the existing capabilities easy to see; it adds none.

Three rules govern every task below, and each is checkable rather than aspirational:

1. **It reads recorded data.** `events.csv`, `summary.json`, `experiment_runs.csv`,
   `aggregate.csv` and `plots/` already hold everything the UI shows. A second, incompatible
   logging path would break CC-06 — the rule that every metric is derivable from the event
   log alone — by giving the same number two sources that can disagree.
2. **It never computes protocol behaviour.** The switching policy stays in
   `protocol/hybrid.py`; the UI visualizes decisions the controller already recorded. A
   threshold re-evaluated in JavaScript is a second controller, and the two would drift.
3. **It never fabricates.** No placeholder metric, invented packet, simulated switch or
   demo-only animation that looks like measured behaviour. Where data does not exist, the UI
   says so — an empty state is information; a plausible-looking fake is a lie with a
   stylesheet.

**Decisions taken here are not D-numbered.** D1–D14 are the decisions whose values are baked
into recorded evidence, so changing one invalidates experiments. A frontend framework choice
cannot invalidate a transfer that already happened, so frontend decisions are recorded in
`design.md` §13 with their rationale and are *not* added to `specs.md` §16. For the same
reason Phase 11 introduces **no new requirement IDs**: it adds no requirement to the
protocol, so each task cites the existing requirement whose evidence it presents.

### Known gaps in the backend surface, found before planning rather than during

Rule 15 of the phase brief — document a missing capability instead of bending the protocol
around it. Three exist, and the tasks that hit them say so:

- **Live event data lags by up to 64 rows.** `eventlog.py` flushes every `FLUSH_EVERY = 64`
  events on purpose: per-event `fsync` would distort the very timings the log exists to
  measure. A live view therefore tails `events.csv` and is *near*-real-time. **The flush
  policy must not be changed to make the UI smoother** — that would trade measurement
  fidelity for animation, which is the wrong way round. T11.4 states the lag in the UI.
- **The receiver's computed hash is not in its `summary.json`.** It records
  `expected_sha256` and `integrity_success`, and sends the hash it computed in the FIN_ACK
  payload, but never writes it down. Showing "source hash vs received hash" side by side
  (T11.4) therefore needs one field added to the receiver's summary — a *logging* addition
  of exactly the kind T6.2 made when it added the `bytes` column, not a protocol change, and
  covered by `test/test_logging.py` when it lands.
- **Captures exist per scenario, not per run.** `captures/` holds the curated T9.4 evidence
  set and the demonstration capture, not a capture for every transfer. T11.12 links the
  relevant recorded capture and says which run it came from; it must not imply that an
  arbitrary run has one.

- [ ] **T11.1** Choose and freeze the frontend architecture: framework, backend/API
      mechanism, run command, directory structure, how log and result data reach the UI, how
      live status is exposed, and how the UI launches the existing sender/receiver/runner.
      Record it in `design.md` §13 with the rationale.
      **Done when:** the architecture is documented, protocol responsibilities are unchanged,
      and every existing CLI command still works exactly as before.
- [ ] **T11.2** Build the application shell and visual system: header, run status, navigation,
      cards, status indicators, responsive layout, and a distinct visual treatment per mode
      (GBN / SR / Hybrid) used consistently everywhere a mode appears.
      **Done when:** the app opens into a coherent protocol-analysis dashboard with a clear
      hierarchy and every major section present.
- [ ] **T11.3** Build the Transfer Control panel: file, host, port, mode, window, loss, RTT,
      jitter, seed, experiment config; start, stop where safe, reset, validation, disabled and
      loading states, and a clear indication of whether the backend is reachable.
      **Satisfies:** presents FR-01, FR-02, §15
      **Done when:** a valid run can be configured and launched from the UI without editing
      any source file, and an invalid one is refused with a readable reason rather than a
      stack trace. Frozen values (segment size, thresholds, RTO policy) are shown as
      read-only context, never as editable fields.
- [ ] **T11.4** Build the live Transfer Overview: lifecycle state, filename, size, progress,
      segments sent and acknowledged, window, current mode, elapsed time, and the integrity
      result with source and received hashes shown side by side.
      **Satisfies:** presents FR-11, IN-05, CC-01, §20
      **Done when:** an evaluator can tell at a glance what the protocol is doing right now;
      the lifecycle states are the ones `design.md` §7 already defines, not new ones; and the
      near-real-time lag is stated in the UI rather than hidden.
- [ ] **T11.5** Build the Adaptive Mode Visualization — the centrepiece. Current and previous
      mode, switch count, transition timestamps, reason, loss estimate at the transition,
      epoch, and time spent in each mode, on a timeline.
      **Satisfies:** presents HY-08, §10, and the `SWITCH` / `MODE` event rows
      **Done when:** an evaluator can say when and *why* the controller switched, reading only
      the screen — with the reason taken from the recorded event, never recomputed. A
      `SWITCH` row whose reason is `FIXED_HYBRID_NOOP` is shown as the control paying the
      drain cost, not as a mode change.
- [ ] **T11.6** Build the Network Conditions panel: configured impairment and seed alongside
      the observed loss estimate, retransmissions and RTT, with sparklines over time.
      **Satisfies:** presents §17, §10
      **Done when:** configured impairment and observed measurement are visually distinct and
      labelled as such. **Simulated loss is never presented as measured physical loss**, and
      the loss estimate is labelled as a reading of the D8 estimator — which over-reads under
      GBN by four to five times — not as a loss rate.
- [ ] **T11.7** Build the Packet / Event Activity view over `events.csv`: the §21 columns,
      visually distinct event types, filtering by event and mode, search by sequence,
      auto-scroll and pause.
      **Satisfies:** presents §21, FR-09
      **Done when:** an evaluator can trace one segment from `SEND` through `DROP`, `RETX` and
      `ACK`, and the view reads the existing format with no second log written anywhere.
- [ ] **T11.8** Build the Retransmission Visualization: a sequence-number timeline with
      retransmission markers and a mode overlay, making GBN's range retransmission and SR's
      single-segment retransmission visibly different shapes.
      **Satisfies:** presents GBN-04, SR-10, §22.1
      **Done when:** the difference is legible from the picture alone, drawn entirely from
      recorded `SEND`/`RETX` rows. Nothing is drawn that did not happen.
- [ ] **T11.9** Build the Metrics Dashboard: every §20 metric with units — goodput,
      completion time, retransmission count and overhead, integrity, switch count, GBN and SR
      residence, latency, and the RTT statistics where samples exist.
      **Satisfies:** presents §20, FR-14
      **Done when:** a completed run's numbers match what `metrics.py` derives for the same
      run. Any presentation-only calculation is labelled as one.
- [ ] **T11.10** Build the GBN vs SR vs Hybrid comparison view over the recorded matrix and
      the generated figures, including the mode-selection timeline under dynamic loss.
      **Satisfies:** presents §18, §19, §27
      **Done when:** an evaluator can compare systems per condition without opening a Python
      script, reading the same `aggregate.csv` the analysis produced. **Results that reflect
      badly on the hybrid are shown with the rest** — the 1–2% oscillation and the cells
      where it loses to SR are part of the result, not an omission.
- [ ] **T11.11** Build the Experiment History / run browser over `experiment_runs.csv`: run
      id, mode, condition, RTT, seed, file size, status, completion time, goodput,
      retransmissions and integrity, each opening into that run's metrics, events, mode
      timeline, configuration and figures.
      **Satisfies:** presents RP-01, RP-02, §19
      **Done when:** any of the 195 recorded runs can be explored from the UI without
      re-running it, keyed by the recorded `run_id`.
- [ ] **T11.12** Build the Wireshark companion: current run id, port, the
      `udp.port == 8888` filter, current mode, relevant retransmission and switch timestamps,
      a reference to the corresponding capture where one exists, and a concise "what to look
      for" panel per mode.
      **Satisfies:** presents WS-01 … WS-09, §22.1
      **Done when:** the panel moves an evaluator from dashboard to Wireshark, and states
      plainly which facts come from the logs (controller state, loss estimate, metrics) and
      which come from the capture (packets on the wire). **It does not claim to inspect
      packets itself.**
- [ ] **T11.13** Build the final demonstration mode: the thirteen steps of `specs.md` §28 as
      a guided flow, with the important transitions prominent and no page-hopping mid-demo.
      **Satisfies:** §28, G-07 · *`experiments/demonstrate.py` already performs the sequence
      headlessly and writes a transcript; the UI presents that same run rather than inventing
      a parallel script.*
      **Done when:** the project can be demonstrated end to end from one screen while
      Wireshark stays available for packet-level verification. **No fake events, and no
      pre-recorded animation presented as live.**
- [ ] **T11.14** Handle error and edge states: backend unreachable, receiver not running,
      invalid file or configuration, transfer timeout, transfer failure, hash mismatch,
      abandoned mode switch, malformed or missing log data, missing results, missing capture.
      **Satisfies:** presents §13, CC-01
      **Done when:** every one shows a readable explanation *and* preserves the underlying
      logged error. A hash mismatch is never softened, and the UI never appears frozen.
- [ ] **T11.15** Validate the architecture: protocol behaviour still in the protocol modules,
      switching still in `protocol/hybrid.py`, experiment logic still in `experiments/`,
      metrics consistent with `metrics.py`, event logs still the only event source, no
      frontend value presented as a protocol measurement, CLI unchanged, tests passing.
      **Done when:** **deleting the frontend leaves the protocol's correctness and every
      recorded experimental result unchanged** — demonstrated, not asserted.
- [ ] **T11.16** Presentation polish: desktop and laptop layouts, readability from a distance,
      consistent spacing, loading, empty and error states, chart labels and units, tooltips,
      no clipped tables, no scrolling during the main demonstration.
      **Done when:** it reads as a finished protocol-analysis tool, with the primary
      demonstration screen prioritized over secondary pages.
- [ ] **T11.17** Document the frontend in `README.md`: purpose, architecture, install, run
      and startup order, configuration, launching a transfer, viewing results, demo mode, its
      relationship to Wireshark, and known limitations.
      **Done when:** a new evaluator can start the system and follow the workflow without
      reading the source. **The documentation does not claim packet-level inspection the
      frontend does not perform.**

### Frontend definition of done

- [ ] A polished dashboard is available.
- [ ] GBN, SR and Hybrid can be selected where supported.
- [ ] Transfer configuration is available through the UI.
- [ ] Transfer progress is visible.
- [ ] The current ARQ mode is clearly displayed.
- [ ] GBN to SR and SR to GBN transitions are clearly visualized.
- [ ] Switching reasons come from recorded controller events.
- [ ] Network-condition information is visualized, with configured and observed kept distinct.
- [ ] Retransmission activity is visualized from recorded events.
- [ ] Event logs can be inspected.
- [ ] Metrics are displayed from recorded data and agree with `metrics.py`.
- [ ] GBN / SR / Hybrid results can be compared.
- [ ] Previous runs can be browsed.
- [ ] Integrity and hash status is clearly displayed.
- [ ] Wireshark evidence is connected to its run without being replaced.
- [ ] The final demonstration can be presented coherently from the frontend.
- [ ] Error states are handled clearly.
- [ ] Existing protocol tests still pass.
- [ ] The existing CLI workflow still works.
- [ ] The frontend does not modify protocol semantics.
- [ ] No fabricated metric, packet, switch or experimental result appears anywhere.

**M12 complete when:** the existing implementation can be controlled, monitored, visualized
and demonstrated through the frontend while the protocol, experiments, logging, metrics and
Wireshark behaviour are exactly what they were before Phase 11 began.

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
| M12 Frontend | The finished system can be driven and demonstrated from a dashboard | T11.1–T11.17 |

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
12. T11.* — the frontend, which presents all of the above and changes none of it.
