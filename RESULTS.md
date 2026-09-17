# Results — Hybrid GBN/SR ARQ with Adaptive Switching

The findings, stated against the hypotheses that were written down before the experiments
were run (T10.3), the limitations and failed experiments (T10.4), and what this project does
and does not claim (T10.5).

**Evidence behind every number here:** 195 recorded transfers
([`experiments/results/experiment_runs.csv`](experiments/results/experiment_runs.csv),
written up in [`experiments.md`](experiments/results/experiments.md)), 444 calibration
transfers ([`calibration.md`](experiments/results/calibration.md)), eight figures
([`figures.md`](experiments/results/figures.md)), five packet captures
([`captures/README.md`](captures/README.md)) and one scripted demonstration rehearsal
([`demonstration.md`](experiments/results/demonstration.md)). Every metric is derived from
`events.csv` alone, and `analyze_results.py --check-logs` confirms all 195 runs still
re-derive to the numbers recorded for them.

**Conditions unless stated otherwise:** 1 MiB file, 1024 B segments, window 8, 100 ms RTT,
`RTO = max(4 × RTT, 200 ms)`, five trials per cell, per-trial seeds derived from one recorded
base seed, identical file and drop stream across systems within a cell.

---

## 1. The hypotheses (specs.md §26)

| | Hypothesis | Verdict |
| --- | --- | --- |
| **H-01** | Under very low loss, GBN may achieve competitive goodput with simpler state | **Supported** |
| **H-02** | As loss increases, GBN retransmission overhead increases | **Supported, strongly** |
| **H-03** | Under lossy conditions, SR reduces unnecessary retransmissions | **Supported, strongly** |
| **H-04** | A suitable hybrid spends more time in GBN when clean, more in SR under sustained loss | **Supported** |
| **H-05** | Bad thresholds or excessive switching can make the hybrid *worse* than a fixed strategy | **Supported — and observed in the shipped configuration, not only in a bad one** |

### H-01 — GBN is competitive under very low loss. Supported.

At 0% loss all four systems complete a 1 MiB transfer in 16.2 s (± 0.04), indistinguishable
within one standard deviation: the pipe, not the strategy, sets the pace. At 1% loss GBN
takes 20.9 s against SR's 19.6 s — 7% behind, for a protocol with one timer instead of
eight and no receive buffer. GBN's simpler state costs nothing measurable until loss is
sustained.

The crossover is at **2% loss**, where GBN falls to 26.3 s against SR's 22.6 s and the gap
stops being noise.

### H-02 — GBN's retransmission overhead rises with loss. Supported, strongly.

Retransmitted bytes as a fraction of bytes transmitted, mean of five trials:

| loss | 0% | 1% | 2% | 5% | 10% | 20% |
| --- | --- | --- | --- | --- | --- | --- |
| GBN | 0.000 | 0.072 | 0.142 | 0.307 | 0.482 | **0.675** |
| SR | 0.000 | 0.010 | 0.019 | 0.053 | 0.103 | 0.197 |

At 20% loss **two thirds of everything GBN puts on the wire is a retransmission**, and its
absolute count reaches 2129 segments against SR's 251 — 8.5×. The growth is faster than
linear in the loss rate, as the mechanism predicts: one drop costs the whole outstanding
window, so the expected cost per loss rises with how full the window is.

### H-03 — SR reduces unnecessary retransmissions. Supported, strongly.

The same table read the other way, and `captures/lossy_gbn_range_retx.pcapng` against
`captures/lossy_sr_individual_retx.pcapng` makes it literal. Those two captures come from the
same file and the same seeded drop stream:

- **GBN puts 62 redundant copies on the wire. SR puts none.**

A segment that was dropped never reached the wire, so resending it is not a duplicate there.
Every duplicate sequence in a capture is therefore a segment that *arrived* and was sent
again anyway — which is exactly the "unnecessary" in H-03, and SR's count of it is zero.

### H-04 — the hybrid follows the condition. Supported.

Fraction of the transfer spent in SR, hybrid mode, by loss level: 0% → **0.00**, 1% → 0.33,
2% → 0.57, 5% → 0.88, 10% → 0.87, 20% → 0.85.

At 0% loss it never leaves GBN in any of five trials. Under sustained loss it spends roughly
six sevenths of the transfer in SR. The plateau at 85–88% rather than 100% is the cost of
starting in GBN and having to earn the evidence to leave — the controller cannot know the
condition before it has observed it.

Under *dynamic* loss (E8) the direction is right both ways: rising loss produced a switch to
SR in 5 of 5 trials and no return; falling loss produced a return to GBN in 5 of 5. The lag
is asymmetric — 5.7 s to react to rising loss, 1.9 s to falling — because confirmation is
counted in acknowledged segments and acknowledgements arrive more slowly during the lossy
phase.

### H-05 — a hybrid can be worse than a fixed strategy. Supported, and it happened here.

This is the hypothesis the project was most at risk of soft-pedalling, so it is stated
plainly: **in the shipped, calibrated configuration the hybrid is worse than pure SR at every
static loss level measured, and worse than both baselines at 1% loss.**

| loss | hybrid ÷ GBN | hybrid ÷ SR |
| --- | --- | --- |
| 0% | 1.00 | 1.00 |
| 1% | **0.98** | **0.92** |
| 2% | 1.06 | 0.91 |
| 5% | 1.27 | 0.96 |
| 10% | 1.56 | 0.95 |
| 20% | **2.00** | 0.93 |

Two distinct failures sit behind those numbers:

1. **Oscillation at 1–2% loss.** 5.0 and 4.4 switches per transfer where a static condition
   justifies at most one, with a spread (±1.4, ±2.3) as large as the effect. The mechanism is
   in §2 below.
2. **The drain cost.** Each switch stalls the pipe for about 0.17 s at 100 ms RTT — roughly
   1.5 RTT — measured directly by the `fixed-hybrid` control. At 1–2% loss that is 3–4% of
   the transfer spent on handshakes that changed nothing useful.

The hybrid earns its cost only from about 2% loss upward, reaching 2.00× pure GBN at 20%.
It never beats SR. **If loss is known in advance to be sustained, pure SR is the better
choice and this controller's only defence is that it did not know.**

---

## 2. Limitations and failed experiments (T10.4)

### 2.1 The dead band sits between the two modes' scales, not between two loss levels

The single most important limitation, and the cause of the 1–2% oscillation.

`SWITCH_HIGH` and `SWITCH_LOW` are thresholds on the **D8 loss estimator**, which is the
fraction of the last 50 acknowledged segments that needed a retransmission. That estimator
reads differently in the two modes at the *same physical loss*, because under GBN one drop
marks the whole outstanding range as retransmitted. Measured at 1% physical loss:

- while GBN is active the estimator reads **0.16 – 0.30**, well above `SWITCH_HIGH = 0.10`;
- while SR is active it reads **0.00**, below `SWITCH_LOW = 0.02`.

So both thresholds are crossed on alternate evaluations and the transfer flaps. Hysteresis
delays each crossing but cannot prevent a sequence of them, because every confirmation
genuinely confirms — the estimator is not noisy here, it is mode-dependent. Widening the dead
band does not fix this; the two readings are at opposite ends of the scale.

**Why calibration did not catch it.** The D9 sweep disqualified any setting that switched at
0% or 1% loss, and at its 256 KiB transfer size none did — those transfers ended before a
second crossing could form. The frozen experimental size is four times larger and shows it.
The calibration was not wrong about what it measured; it measured too short a transfer.

D9 remains frozen. Changing it would invalidate 444 calibration transfers and all 195
experimental runs, and the honest report of a measured weakness is worth more than a
re-tuned number with no evidence behind it. A fix would have to make the estimator
mode-independent — for example, counting lost *segments* rather than retransmitted ones —
which is a change to D8, the other experiment-invalidating decision.

### 2.2 The loss estimator's GBN bias

Known and documented at freeze time (specs.md §16.9), not reconstructed afterwards: the
estimator over-reads loss by roughly 4–5× while GBN is active. The practical consequence is
that **`SWITCH_HIGH = 0.10` does not mean "switch at 10% loss"** — on this test bed it fires
at about 2% physical loss. Anywhere the threshold is quoted, this has to be quoted with it.

The bias fails safe: it biases the controller toward entering SR, which is the mode that
tolerates loss. It is also the direct cause of §2.1.

### 2.3 The per-switch drain cost

D10 trades throughput for correctness: the sender drains its window before every mode change,
so no packet is ever read under the wrong ACK convention. Measured cost ≈ 0.17 s per switch
at 100 ms RTT (≈1.5 RTT), from `fixed-hybrid`, which performs the same drains and handshakes
while changing nothing. That control also shows the *monitoring* is nearly free: it tracks
pure GBN to within 0.1–3.7% at every loss level.

### 2.4 Experiments that did not go as designed

- **E8's `loss_change` condition reached only 2 of its 4 scheduled steps**, in all five
  trials. The step times were set against the measured length of a sustained-loss transfer,
  but that profile runs mostly at 1–2% loss and finishes in ~30 s, so the 32 s and 44 s steps
  fell past the end. The condition still crosses the boundary twice and still produces 4.0
  switches, so it is usable — but it is a weaker test of repeated change than intended, and
  it is reported rather than relabelled.
- **Two oscillation-stage schedules were discarded during calibration** before the reported
  one, for the same class of reason: the transfer finished before the schedule reached its
  second step, so the "oscillating" condition never oscillated (`calibration.md` §3).
- **A hysteresis count of 1 scored best on every static and falling-loss condition** and
  would have been frozen had the oscillating stage not been added. It was excluded because
  it made 60% more switches than the condition justified. The stage was added *because* the
  evidence to hand conflicted with HY-09, and it reversed the reading.
- **The second file size proposed for D14 (10 MiB) was not run.** At the measured rate a
  10 MiB transfer at 20% loss would take about 23 minutes per run and would dominate the
  matrix without changing what any cell says. Recorded as not run rather than quietly
  dropped.

### 2.5 Scope limitations, by design

- One sender, one receiver, one file, loopback only. No multi-client, no NAT, no security.
- Application-level impairment rather than `tc`/netem — portable to Windows and reproducible
  from a seed, but it models loss and delay, not queueing, reordering or bursty loss.
  **Real networks lose packets in bursts**, which is precisely the regime where GBN's
  range retransmission is most expensive; an independent per-packet drop model understates
  that.
- Fixed RTO throughout (D7). Adaptive RTO is explicitly out of scope (specs.md §3) so that
  the mode-switching effect is not confounded with timer tuning.
- No congestion control. The window is fixed at 8 and never responds to loss, so none of
  these numbers should be read as throughput figures for a transport protocol.
- Sequence-number wraparound is not implemented (SEQ-04); uint32 at 1 KiB segments covers
  ~4 TiB per transfer.

---

## 3. Research positioning (T10.5, specs.md §3, §30)

**This is an implementation and evaluation project. It claims no algorithmic novelty.**

Adaptive ARQ windows, hybrid GBN/SR schemes and adaptive RTO are all established prior art,
including schemes such as WORM-ARQ that switch between SR and GBN according to transmission
quality. Nothing here is presented as inventing GBN, SR, adaptive switching or hysteresis.

What the project contributes is a working, instrumented implementation of runtime GBN↔SR
switching with its decision rule calibrated against measured data rather than chosen by
assertion, and an evaluation that reports where the approach **fails** as carefully as where
it succeeds. The prior-art check found many student implementations of GBN and SR
separately, but no evidence of undergraduate projects implementing runtime switching between
them with a recorded evaluation — which is the basis for presenting it as an undergraduate
implementation-and-evaluation exercise, and not as a contribution to the literature.

Explicitly out of scope (specs.md §3): replacing TCP, implementing congestion control,
treating an ARQ window as a congestion window, adaptive RTO as the primary mechanism, and
any security or multi-client concern.

---

## 4. Reproducing all of it

Every artefact above regenerates from recorded data without re-running a transfer, except
the matrix itself:

```bash
python -m pytest -q                              # 601 tests
python experiments/run_experiment.py --all --resume   # the 195-run matrix (~2.5 h)
python experiments/run_experiment.py --verify         # integrity + raw-log fingerprints
python experiments/analyze_results.py --check-logs    # aggregate, graphs, re-derivation
python experiments/capture_evidence.py                # the five Wireshark captures
python experiments/demonstrate.py                     # the specs.md §28 rehearsal
```

The matrix replays from two recorded numbers: the base seed `20260917` and the commit
recorded in every `summary.json`.
