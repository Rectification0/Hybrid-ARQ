# Figures (T9.2, T9.3, specs.md §27)

Every figure in `plots/` is produced by `python experiments/analyze_results.py` from
`experiment_runs.csv` and the raw event logs. **Nothing is drawn by hand and nothing is
edited afterwards** — regenerating them is one command, and a figure that disagreed with the
data would be a bug in the script rather than a slip of the mouse.

The aggregate behind them is `aggregate.csv`: one row per (experiment, condition, system)
with the mean and the standard deviation across that cell's five trials, the number of trials
aggregated, and the number excluded. Runs whose integrity check failed are **excluded from
every mean and counted separately** (CC-01); in this matrix that count is zero everywhere.

Reading conventions, identical in every figure: GBN blue circles, SR orange squares, Hybrid
aqua diamonds, fixed-hybrid yellow dashed triangles. Colour is never the only carrier of
identity — each series also has its own marker, and is labelled at its right-hand end as
well as in the legend. Error bars are ±1 standard deviation across trials.

---

## Required (§27)

| Figure | Shows | The reading |
| --- | --- | --- |
| `goodput_vs_loss.png` | Goodput vs loss, four systems | SR leads everywhere; the hybrid tracks it from 5% loss up and sits with GBN below 2%. |
| `retransmissions_vs_loss.png` | Retransmission count vs loss | GBN and fixed-hybrid are one line — 2129 retransmissions at 20% loss against SR's 251. The control's line lying exactly on GBN's is the clearest statement that the controller's overhead is not what separates them. |
| `retransmission_overhead_vs_loss.png` | Retransmitted ÷ transmitted bytes | At 20% loss GBN spends 67.5% of its bytes on retransmission, SR 19.7%, the hybrid 27.0%. |
| `completion_time_vs_loss.png` | Completion time vs loss | The same shape inverted: 139.6 s for GBN at 20% loss against 69.8 s for the hybrid. |
| `mode_selection_over_time.png` | Mode over time under dynamic loss (E8) | One band per trial, GBN blue and SR orange, with the scheduled loss changes marked. Rising loss: every trial enters SR and stays. Falling loss: every trial returns to GBN about 2 s after the change. The irregular schedule: visibly more switching, including two trials that flap early. |

## Optional (T9.3)

| Figure | Shows | The reading |
| --- | --- | --- |
| `goodput_vs_rtt.png` | Goodput vs RTT at 5% loss (E7) | Three near-parallel lines across a fiftyfold RTT range: the hybrid keeps a 1.22–1.25× advantage over GBN at every RTT, so the switching decision does not depend on delay. |
| `switching_frequency_vs_loss.png` | Switches per transfer vs loss | The oscillation, seen directly: a spike to 5.0 switches at 1% loss and 4.4 at 2%, falling to 1.0 by 10%. A static condition justifies at most one. The spread there — ±1.4 switches at 1% loss and ±2.3 at 2%, against ±0.0 at 10% and 20% — is what an unstable decision looks like. |
| `sr_residence_vs_loss.png` | Fraction of the transfer spent in SR | 0% at no loss, 33% at 1%, 57% at 2%, then a plateau at 85–88% from 5% loss up. The plateau, not 100%, is the cost of starting in GBN and having to earn the evidence to leave. |

---

## Why two of these are worth more than the headline numbers

`retransmissions_vs_loss.png` and `switching_frequency_vs_loss.png` carry the two findings a
table of means hides.

The first is the control doing its job: GBN and fixed-hybrid are indistinguishable at every
loss level, so the hybrid's advantage cannot be attributed to anything the controller does
*except* changing mode. The second is the failure: the hybrid's switching frequency peaks
where its benefit is lowest, and the spread there is enormous. Both are plotted rather than
described because the shape is the argument.
