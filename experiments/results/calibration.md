# D9 calibration — switching thresholds and hysteresis (T7.1, T7.2, T7.3)

**Frozen:** `SWITCH_HIGH = 0.10`, `SWITCH_LOW = 0.02`, `HYSTERESIS_COUNT = 3`, with
`EVALUATION_INTERVAL_SEGMENTS = 20` and `MIN_MODE_RESIDENCE_S = 1.0` held fixed throughout
and therefore frozen by the same evidence.

**Evidence:** 444 transfers, recorded in `calibration_runs.csv` (one row per run) and
`calibration_summary.csv` (aggregated per condition). Produced by
`experiments/calibrate_thresholds.py`, which states its selection rule in its own docstring
*before* the data was collected. Every metric in both files is re-derived from `events.csv`
by `metrics.py` — the same derivation an outside reader would perform (CC-06), not read out
of the sender's memory.

**Conditions.** 256 KiB file (256 segments), window 8, `RTO = 200 ms`, loopback, three
trials per cell with per-trial seeds derived from base seed `20260916`. Within every cell
the file, window, RTO and seed are identical across systems and settings (RP-04); only the
setting varies. The oscillation stage uses a 3 MiB file, for the reason given below.

---

## 1. Entry threshold — 306 runs over the static loss grid

`SWITCH_HIGH` ∈ {0.05, 0.10, 0.20, 0.35, 0.50} × `HYSTERESIS_COUNT` ∈ {1, 2, 3} across
0%, 1%, 2%, 5%, 10% and 20% loss, against pure GBN and pure SR at the same seeds.

**The headline result is that `SWITCH_HIGH` barely matters across most of its range.** Mean
switches per transfer at the frozen hysteresis count of 3:

| loss | 0.05 | **0.10** | 0.20 | 0.35 | 0.50 |
| --- | --- | --- | --- | --- | --- |
| 0% | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| 1% | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| 2% | 0.33 | 0.33 | 0.33 | 0.00 | 0.00 |
| 5% | 1.00 | 1.00 | 1.00 | 0.67 | 0.00 |
| 10% | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 20% | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

Thresholds of 0.05, 0.10 and 0.20 are indistinguishable at every loss level tested. Only
above 0.20 does the threshold begin to bite, and even 0.50 still switches every time at
10% loss.

This is the **D8 GBN bias, measured**: because one loss under GBN retransmits the whole
outstanding range, the estimator reads roughly four to five times the physical loss rate
while GBN is active. At 5% physical loss it clears a threshold of 0.20; at 10% it clears
0.50. The frozen `SWITCH_HIGH = 0.10` therefore does **not** mean "switch at 10% loss" — on
this test bed it fires at about 2% physical loss. The number is a reading of the estimator,
not of the network, and the report must say so wherever it is quoted.

Where a higher threshold does act, it mostly delays the switch rather than preventing it: at
10% loss every threshold switched, but SR residence fell from 46% of the transfer at 0.05–0.20
to 31% at 0.50.

Scores (mean goodput ÷ the better fixed baseline at each loss level, across the grid), for
the frozen hysteresis count of 3:

| `SWITCH_HIGH` | 0.05 | **0.10** | 0.20 | 0.35 | 0.50 |
| --- | --- | --- | --- | --- | --- |
| score | 0.824 | **0.832** | 0.776 | 0.784 | 0.759 |

0.10 is the best of them, and no setting was disqualified: none switched at 0% or 1% loss,
and none exceeded two switches under a static condition.

## 2. Exit threshold — 42 runs against falling loss (15% → 0% at t = 3 s)

`SWITCH_LOW` ∈ {0.01, 0.02, 0.05, 0.10} × `HYSTERESIS_COUNT` ∈ {1, 2, 3}.

**`SWITCH_LOW` is entirely inert across its range.** Every value produced identical switch
counts and identical return rates, because once loss stops the estimator falls to zero and
crosses all four candidates within the same evaluation. The discriminator was the
hysteresis count alone:

| `HYSTERESIS_COUNT` | switches | returned to GBN | SR residence | goodput vs pure SR |
| --- | --- | --- | --- | --- |
| 1 | 2.00 | 3 of 3 | 58% | 0.99 |
| 2 | 1.67 | 2 of 3 | 34% | 0.98 |
| 3 | 1.00 | **0 of 3** | 6% | 0.97 |

At this transfer size a count of 3 never returns to GBN at all: it needs three confirming
evaluations at 20 acked segments each, *after* ~50 clean outcomes have flushed the
estimator's window — roughly 110 segments of post-change traffic, more than remained. That
is a real cost of the frozen value and is recorded as such in §5.

`SWITCH_LOW = 0.02` is frozen not because the sweep distinguished it — it did not — but
because it is the SR-side reading of the same physical loss that `SWITCH_HIGH = 0.10`
represents on the GBN side. The two thresholds describe one crossover at roughly 2% physical
loss, seen through an estimator that reads high under GBN and low under SR. That asymmetry
is what gives the dead band its width in practice, and it is why the pair is coherent rather
than arbitrary.

## 3. Hysteresis — 15 runs against an oscillating condition

The exit sweep made `HYSTERESIS_COUNT = 1` look best, and it is the value the entry sweep's
score also preferred. Both are conditions that cross the threshold **once**, so neither can
tell an adaptive controller from a twitchy one — and HY-09 requires rapid repeated
transitions to be *prevented*. A stage was therefore added that alternates loss across the
crossover (1% ↔ 6%, period 2.5 s, 3 MiB file, five crossings in a ~13.4 s transfer).

It reversed the reading:

| `HYSTERESIS_COUNT` | switches (per trial) | vs 5 crossings | goodput vs best fixed |
| --- | --- | --- | --- |
| 1 | 8.00 (9, 9, 6) | **+60%** | 0.98 |
| 2 | 7.00 (7, 8, 6) | +40% | 0.98 |
| **3** | **5.33 (6, 4, 6)** | **+7%** | 0.99 |

A count of 1 makes roughly three switches for every two the condition justifies, and buys
nothing for them — goodput is within 1% across all three counts. That is oscillation in the
sense HY-09 names, so a count of 1 is excluded on requirements grounds regardless of its
score under static loss. A count of 3 tracks the condition.

Two notes on method. The period is deliberately longer than `MIN_MODE_RESIDENCE_S`: a
condition alternating faster than the minimum residence cannot provoke oscillation whatever
the count, so testing against one would prove nothing. And the file is 3 MiB rather than
256 KiB because the first two attempts at this stage were invalid — the transfer finished
before the schedule reached its second step, so the "oscillating" condition never
oscillated during the run. Those runs were discarded rather than reported; a clean phase on
loopback runs roughly fifty times faster than a lossy one, which is why both phases of the
final schedule are lossy.

## 4. Confirmation at the frozen setting — 72 runs

`SWITCH_HIGH = 0.10`, `SWITCH_LOW = 0.02`, `HYSTERESIS_COUNT = 3`, against all three fixed
systems at the same seeds. Goodput in KiB/s, mean of three trials:

| loss | GBN | SR | fixed-hybrid | **hybrid** | hybrid ÷ best | hybrid ÷ GBN | switches |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0% | 10777 | 10365 | 8015 | 8495 | 0.79 | 0.79 | 0.00 |
| 1% | 383 | 493 | 380 | 376 | 0.76 | 0.98 | 0.00 |
| 2% | 349 | 383 | 337 | 348 | 0.91 | 1.00 | 0.33 |
| 5% | 78 | 111 | 78 | 86 | 0.77 | 1.10 | 1.00 |
| 10% | 47 | 61 | 47 | 61 | 1.00 | 1.30 | 1.00 |
| 20% | 19 | 39 | 18 | 25 | 0.65 | 1.32 | 1.00 |

**Integrity failures.** Pure GBN failed 1 of 3 trials at 20% loss, and fixed-hybrid — which
stays in GBN by construction — failed the same trial. Both exhausted `DATA_RETRY_LIMIT` on
a single segment and aborted. SR and the hybrid completed every trial. At 20% loss GBN does
not merely become slow; it can fail the transfer outright, and switching away from it is
what prevented that.

## 5. Where the hybrid is *worse* than a fixed strategy (T7.3, H-05)

Recorded because it is a result, not a defect to bury:

1. **The hybrid never beats pure SR at any loss level.** It beats GBN from 2% loss upward
   (1.00× to 1.32×) and is never worse than GBN beyond noise, but SR is better everywhere
   loss exists. Under a *sustained static* condition the hybrid cannot win: it starts in
   GBN by construction and must accumulate evidence before it may switch, so it always pays
   a GBN phase that pure SR does not. Its value has to come from conditions that change,
   and from not needing to know the condition in advance.
2. **At 0% loss the controller costs ~21% of goodput** (8495 against GBN's 10777), and
   fixed-hybrid shows the same drop (8015) with zero switches — so this is the cost of
   *monitoring*, not of switching. The caveat is that a 256 KiB transfer at 0% loss
   completes in about 25 ms on loopback, where fixed per-run overhead dominates; this
   number should not be read as a steady-state throughput penalty. It does say that at
   loopback speeds the per-ACK bookkeeping is not free.
3. **At 1% loss the hybrid is 24% worse than pure SR and no better than GBN**, because the
   estimator never rises far enough to switch. This is the dead band working as designed
   and costing what it costs: between roughly 1% and 2% physical loss, SR is the better
   strategy but the controller stays in GBN.
4. **`HYSTERESIS_COUNT = 3` cannot adapt back within a short transfer.** At 256 KiB the
   return to GBN never happened in any trial (§2). The frozen value is chosen for the
   oscillation case; on transfers shorter than roughly 110 segments of post-change traffic
   it means the hybrid enters SR and stays there. Since the planned experiment file size
   (D14, still open at T8.1) is 1 MiB, there is room — but this interaction must be
   re-examined if a smaller file is ever chosen.
5. **A low hysteresis count is actively harmful under oscillation** (§3): `HYSTERESIS_COUNT
   = 1` produced 60% more switches than the condition justified, for no goodput gain. It is
   the setting that would have been chosen had only the static and falling-loss evidence
   been collected.

## 6. Validity at non-zero RTT — 9 runs

The sweep runs at loopback speed because it is several hundred transfers. The D8 estimator
is a ratio of segment outcomes and carries no unit of time, so the thresholds should be
close to RTT-invariant — checked rather than assumed, at 50 ms emulated RTT and 10% loss:

| system | goodput | switches | completion |
| --- | --- | --- | --- |
| GBN | 27.1 KiB/s | 0.00 | 9.8 s |
| SR | 39.6 KiB/s | 0.00 | 6.6 s |
| hybrid | 33.6 KiB/s | 1.00 (3 of 3) | 7.7 s |

The hybrid switched exactly once in every trial and spent 55% of the transfer in SR — the
same behaviour as the 0 ms cell at the same loss rate. The threshold transfers to a
non-zero RTT. This is one cell, not a sweep; E7 (T8.5) tests the full RTT range.

## 7. What would invalidate this calibration

Changing `SWITCH_HIGH`, `SWITCH_LOW`, `HYSTERESIS_COUNT`, `EVALUATION_INTERVAL_SEGMENTS`,
`MIN_MODE_RESIDENCE_S`, `LOSS_WINDOW_SIZE` (D8), the window size (D11) or the RTO policy
(D7) — every comparison above was made at one fixed value of each. The two cadence
parameters were held fixed rather than swept, which is stated plainly here because a value
that was never varied is easy to mistake for one that was.

Re-running the sweep reproduces it from the base seed alone:

```bash
python experiments/calibrate_thresholds.py --stage entry   --trials 3 --segments 256
python experiments/calibrate_thresholds.py --stage exit    --trials 3 --segments 256  --switch-high 0.10 --append
python experiments/calibrate_thresholds.py --stage flap    --trials 3 --segments 3072 --switch-high 0.10 --switch-low 0.02 --append
python experiments/calibrate_thresholds.py --stage confirm --trials 3 --segments 256  --switch-high 0.10 --switch-low 0.02 --hysteresis 3 --append
python experiments/calibrate_thresholds.py --stage rtt-check --trials 3 --segments 256 --switch-high 0.10 --switch-low 0.02 --hysteresis 3 --rtt 50 --append
python experiments/calibrate_thresholds.py --stage report
```
