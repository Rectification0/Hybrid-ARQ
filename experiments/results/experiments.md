# E1–E8 — the experimental matrix (T8.4, T8.5, T8.6, T8.7)

**Evidence:** `experiment_runs.csv`, one row per run, **195 runs, all with matching hashes**.
Produced by `experiments/run_experiment.py` from the configs in `experiments/configs/`. Every
metric in that file is re-derived from the run's own `events.csv` by `metrics.py` — the
derivation an outside reader would perform (CC-06) — rather than read out of the sender's
memory. The raw logs are kept untouched under `logs/experiments/<run_id>/`, and the index
records the SHA-256 of both event logs so a later edit is detectable rather than silent
(RP-07); `run_experiment.py --verify` recomputes them.

**Scale, frozen at T8.1 against a measured pilot:** 1 MiB source file (1024 segments of
1024 B, D14), 5 trials per cell (D12), per-trial seeds derived from base seed `20260917` by
`sha256(base/experiment/condition/trial)` (D13). Window 8 (D11), `RTO = max(4 × RTT, 200 ms)`
derived per condition and then held fixed (D7), abort timeout 300 s.

**What is identical within a cell (RP-04, specs.md §18):** the source file, the segment size,
the window, the derived RTO and **the impairment seed**. The seed does not depend on the
system, so GBN, SR, Hybrid and fixed-hybrid in one cell meet the byte-identical drop
sequence. Only the retransmission strategy differs.

**Systems.** `gbn` and `sr` are the fixed baselines; `hybrid` is the adaptive system;
`fixed-hybrid` is the switching-overhead control (T5.6) — controller and MODE machinery
active, switching disabled. It is included across E1–E6 because it is the only way to
separate "the hybrid adapted well" from "the hybrid's measurement overhead was small", and
it turned out to be the most informative column in the table.

---

## 1. What was run

| Experiment | Condition | Systems | Runs |
| --- | --- | --- | --- |
| E1–E6 | loss 0 / 1 / 2 / 5 / 10 / 20% at 100 ms RTT | GBN, SR, Hybrid, fixed-hybrid | 120 |
| E7 | 5% loss, RTT 10 / 50 / 100 / 500 ms | GBN, SR, Hybrid | 60 |
| E8 | dynamic loss — rising, falling, irregular — at 100 ms RTT | Hybrid | 15 |

---

## 2. E1–E6 — the loss sweep at 100 ms RTT

### Completion time, mean of 5 trials ± sd (s)

| loss | GBN | SR | **Hybrid** | fixed-hybrid |
| --- | --- | --- | --- | --- |
| 0% | 16.18 ± 0.04 | 16.21 ± 0.02 | **16.20 ± 0.01** | 16.20 ± 0.03 |
| 1% | 20.94 ± 1.45 | 19.57 ± 0.97 | **21.40 ± 1.33** | 21.74 ± 1.56 |
| 2% | 26.30 ± 2.72 | 22.59 ± 1.24 | **24.73 ± 0.93** | 27.05 ± 2.51 |
| 5% | 42.51 ± 2.53 | 31.99 ± 1.31 | **33.30 ± 1.24** | 42.91 ± 2.61 |
| 10% | 73.01 ± 9.11 | 43.91 ± 2.89 | **46.31 ± 3.90** | 72.98 ± 9.13 |
| 20% | 139.62 ± 8.70 | 64.63 ± 3.77 | **69.77 ± 3.16** | 139.44 ± 9.49 |

### Retransmissions and overhead (mean of 5; overhead = retransmitted ÷ transmitted bytes)

| loss | GBN retx | SR retx | Hybrid retx | GBN ovh | SR ovh | Hybrid ovh |
| --- | --- | --- | --- | --- | --- | --- |
| 0% | 0 | 0 | 0 | 0.000 | 0.000 | 0.000 |
| 1% | 80.0 | 10.2 | 59.0 | 0.072 | 0.010 | 0.054 |
| 2% | 170.8 | 20.0 | 73.8 | 0.142 | 0.019 | 0.067 |
| 5% | 454.6 | 57.4 | 88.2 | 0.307 | 0.053 | 0.079 |
| 10% | 961.0 | 117.6 | 180.0 | 0.482 | 0.103 | 0.149 |
| 20% | 2129.4 | 251.2 | 378.4 | 0.675 | 0.197 | 0.270 |

GBN's retransmission count is 8.5× SR's at 20% loss and its overhead 3.4×: at a window of 8,
one drop costs the whole outstanding range. This is the effect the hybrid exists to arbitrate,
measured at the frozen scale rather than assumed.

### The hybrid against the baselines

| loss | goodput ÷ GBN | goodput ÷ SR | switches | SR residence |
| --- | --- | --- | --- | --- |
| 0% | 1.00 | 1.00 | 0.0 | 0% |
| 1% | 0.98 | 0.92 | **5.0** | 33% |
| 2% | 1.06 | 0.91 | **4.4** | 57% |
| 5% | 1.27 | 0.96 | 1.4 | 88% |
| 10% | 1.56 | 0.95 | 1.0 | 86% |
| 20% | **2.00** | 0.93 | 1.0 | 85% |

The shape is the one calibration predicted, now at four times the transfer length: the hybrid
converges on SR's behaviour as loss rises, reaching **2.00× pure GBN at 20% loss** while
staying within 4–9% of pure SR. It never beats SR, because it starts in GBN
(`config.DEFAULT_MODE`) and has to earn the evidence to leave.

### What the control shows

`fixed-hybrid` runs the same controller and performs the **same number of window drains and
MODE handshakes** as the hybrid performs switches — 5.2 against 5.0 at 1% loss, 4.4 against
4.4 at 2%, 1.4, 1.0, 1.0 thereafter — but never changes mode. That makes the two differences
readable separately:

- **fixed-hybrid − GBN is the cost of the drain.** At 1% loss, 5.2 drains cost 0.80 s
  (3.8%); at 2%, 4.4 drains cost 0.75 s. Both work out at **≈0.17 s per drain, about 1.5×
  the 100 ms RTT** — which is what draining a window of 8 and exchanging MODE should cost,
  and is the first direct measurement of D10's price.
- **hybrid − fixed-hybrid is the value of actually switching.** At 5% loss and above it is
  the whole difference between 42.9 s and 33.3 s: the monitoring is nearly free (fixed-hybrid
  tracks pure GBN to within 0.1–3.7% everywhere), so the hybrid's gain is adaptation, not
  measurement overhead.

---

## 3. E7 — RTT sweep at 5% loss

| RTT | GBN | SR | **Hybrid** | ÷ GBN | ÷ SR | switches |
| --- | --- | --- | --- | --- | --- | --- |
| 10 ms | 15.46 ± 1.14 s | 11.96 ± 0.47 s | **12.31 ± 0.37 s** | 1.25 | 0.97 | 1.0 |
| 50 ms | 21.02 ± 1.68 s | 16.02 ± 1.08 s | **17.03 ± 1.43 s** | 1.24 | 0.94 | 1.4 |
| 100 ms | 41.80 ± 4.50 s | 31.80 ± 1.60 s | **33.16 ± 1.51 s** | 1.25 | 0.96 | 1.4 |
| 500 ms | 175.94 ± 12.61 s | 138.13 ± 6.44 s | **144.36 ± 3.81 s** | 1.22 | 0.96 | 1.8 |

**The switching decision is RTT-invariant, as the calibration expected but could not show.**
That sweep ran without emulated RTT and argued from the estimator's definition — a ratio of
segment outcomes, carrying no unit of time — that the thresholds should transfer. Across a
fiftyfold change in RTT the hybrid's advantage over GBN stays at 1.22–1.25× and its shortfall
against SR at 3–6%, with the switch count varying only between 1.0 and 1.8. The argument now
has evidence behind it.

Per-condition mean retransmission counts are flat across the sweep for each system (GBN
383–437, SR 48–57, hybrid 87–100), as they should be: the drop sequence is a function of the seed, not of
the delay. Only completion time scales.

---

## 4. E8 — dynamic loss

Hybrid only (specs.md §19); its fixed comparators at the same loss levels are E1–E6.

| condition | schedule | time | switches | SR residence | scheduled changes reached |
| --- | --- | --- | --- | --- | --- |
| `loss_rise` | 0% → 10% at 8 s | 32.18 ± 1.66 s | 1.0 | 57% | 1 of 1 |
| `loss_fall` | 10% → 0% at 20 s | 29.31 ± 0.68 s | 2.0 | 58% | 1 of 1 |
| `loss_change` | 2 → 12 → 1 → 15 → 2% at 8/20/32/44 s | 29.68 ± 0.79 s | 4.0 | 64% | **2 of 4** |

**Adaptation works in both directions, and the lag is asymmetric.** Under `loss_rise` the
controller enters SR 3.4–8.9 s after the change (mean 5.7 s) and stays there. Under
`loss_fall` it returns to GBN 1.6–2.1 s after the change (mean 1.9 s) in every trial. The
asymmetry is mechanical rather than mysterious: confirmation is counted in *acknowledged
segments* (20 per evaluation, 3 evaluations), and acknowledgements arrive far more slowly
during the lossy phase than during the clean one. The same three confirmations therefore take
three times longer in wall-clock on the way up.

A return to GBN inside a single transfer is something the 256 KiB calibration never once
observed (`calibration.md` §5). At the frozen 1 MiB size it happens in **5 of 5** falling-loss
trials, which is the clearest evidence that D14's size was the binding constraint on
observing adaptation, not the hysteresis rule.

**Two of the four scheduled changes in `loss_change` were never reached**, in all five
trials. The step times were set against the measured length of a *lossy* 1 MiB transfer
(46 s at 10% loss, E5), but that profile spends most of its time at 1–2% loss and finishes in
about 30 s, so the 32 s and 44 s steps fall past the end of the transfer. The condition still
crosses the boundary twice and still produces 4.0 switches per transfer, so it does its job —
but it is a weaker test of repeated change than intended, and it is recorded here rather than
quietly reported as a four-change condition. Fixing it means either a longer file or earlier
steps; both change the condition, so neither is applied retroactively to runs already
recorded.

---

## 5. Where the hybrid is worse, and what it costs (H-05)

This section is the counterpart of `calibration.md` §5, at the frozen experimental scale.

1. **The hybrid never beats pure SR — in any of the ten static cells measured.** Its goodput
   is 0.91–1.00 of SR's across E1–E7. If loss is known in advance to be sustained, SR alone is
   the better choice and the hybrid's only defence is that it did not know.

2. **At 1% and 2% loss it oscillates, and is worse than both baselines at 1%.** It averages
   **5.0 and 4.4 switches per transfer** where a static condition justifies at most one, and
   at 1% loss lands at 0.98× GBN and 0.92× SR — worse than either. The mechanism is visible
   in the log (`E2_hybrid_loss01_trial00`):

   ```
   2.42s  SWITCH -> sr   loss_estimate=0.160000  MODE_THRESHOLD;from=gbn;epoch=1
   4.29s  SWITCH -> gbn  loss_estimate=0.000000  MODE_THRESHOLD;from=sr;epoch=2
  12.09s  SWITCH -> sr   loss_estimate=0.300000  MODE_THRESHOLD;from=gbn;epoch=3
  14.85s  SWITCH -> gbn  loss_estimate=0.000000  MODE_THRESHOLD;from=sr;epoch=4
   ```

   At 1% physical loss the D8 estimator reads **0.16–0.30 while GBN is active** — one drop
   marks the whole outstanding range — and **0.00 while SR is active**, because a 50-segment
   window at 1% loss usually contains no retransmitted segment at all. Both thresholds are
   therefore crossed on alternate evaluations: the dead band of 0.02–0.10 does not separate
   the two *modes'* readings at low loss, it sits between them. Hysteresis delays each
   crossing but cannot prevent a sequence of them, because every confirmation genuinely
   confirms.

   **Why the calibration did not catch it.** The sweep disqualified any setting that switched
   at 0% or 1% loss, and at 256 KiB none did — those transfers ended before the estimator
   could complete a second crossing. The frozen transfer is four times longer and shows what
   a longer run does. D9 stays frozen: changing it would invalidate the 444-transfer
   calibration and all 195 runs here. This is recorded as a known limitation for T10.4, and it
   is a limitation of the *dead band's construction* — one scale for two modes that read
   differently — not of the hysteresis count.

3. **The drain is not free, and now has a price.** 0.17 s per switch at 100 ms RTT, ≈1.5 RTT,
   measured by the `fixed-hybrid` control. At 1–2% loss, where switches are frequent and
   pointless, that is 3–4% of the transfer spent on handshakes that changed nothing.

4. **At 0% loss the hybrid costs nothing measurable** (16.20 s against GBN's 16.18 s, inside
   one standard deviation). The ~21% monitoring cost seen in the calibration does not
   reproduce here; that sweep ran on an unthrottled loopback where the controller's work was
   a visible fraction of a very fast transfer, while at 100 ms RTT the pipe, not the CPU, sets
   the pace. Both numbers are real for their condition, and the difference between them is a
   statement about the test bed rather than about the controller.

5. **GBN completed every trial, including all five at 20% loss.** The calibration recorded
   pure GBN aborting one 20% trial at 256 KiB; here, with the RTO derived from a 100 ms RTT
   (400 ms rather than 200 ms), no run exhausted `DATA_RETRY_LIMIT`. The two results do not
   contradict each other — they are different conditions — and the claim "the hybrid completed
   where GBN aborted" is *not* supported by this matrix.

---

## 6. Integrity and reproducibility (T8.7)

- **195 of 195 runs passed their hash check** (IN-05, CC-01). No integrity failures, no
  aborted runs, no run whose sender exited non-zero. Nothing is excluded from the aggregates
  above.
- **The raw logs are intact.** `run_experiment.py --verify` recomputes the SHA-256 of every
  `events.csv` recorded in the index and reports 195/195 unchanged. Aggregation reads the
  logs; it does not write to them (RP-07).
- **The matrix replays from two numbers**: the base seed `20260917` and the commit recorded in
  every `summary.json` (RP-01, RP-02, RP-08). Each run also records the configuration it
  actually used — seed, impairment, derived RTO, file size, thresholds — and the freeze state
  of all fourteen decisions.
- The run was interrupted once at 156/195 by the machine running short of memory and resumed
  with `--resume`; the index is appended per run, so no completed run was repeated or lost.
