# Hybrid GBN/SR ARQ with Adaptive Switching

A UDP file-transfer protocol that implements both Go-Back-N and Selective Repeat
retransmission, monitors observed loss during a transfer, and switches between the two
strategies at runtime — then measures whether that switching actually pays off against
either fixed strategy.

Positioning: this is an implementation and evaluation project. It makes no claim of
global novelty (`specs.md` §30).

## Documents

| File | Role |
| --- | --- |
| `Hybrid_GBN_SR_Functionality_and_Technical_Specification.docx` | Source of truth |
| `specs.md` | Requirements, with stable requirement IDs |
| `design.md` | Technical design: modules, state machines, decisions D1–D14 |
| `tasks.md` | Task breakdown with explicit completion conditions |
| `RESULTS.md` | The findings: hypotheses H-01…H-05, limitations, positioning |

## Status

**All phases complete (M1–M11).** 601 tests pass. The protocol, both baselines and the
hybrid controller are implemented and tested; the experimental matrix of `specs.md` §19 has
been run and written up; the graphs, the Wireshark evidence set and the demonstration
rehearsal are all produced by script from recorded data.

The findings are in [`RESULTS.md`](RESULTS.md) — hypotheses, limitations, positioning —
with eight figures in `plots/` (indexed in
[`experiments/results/figures.md`](experiments/results/figures.md)) and the five-file
Wireshark evidence set in `captures/`. The matrix behind them:
**195 transfers, every one with a matching hash**, recorded in
[`experiments/results/experiment_runs.csv`](experiments/results/experiment_runs.csv) and
written up in [`experiments/results/experiments.md`](experiments/results/experiments.md).

Headline, 1 MiB over loopback at 100 ms RTT, five trials per cell, identical seed and file
across systems within each cell:

| loss | GBN | SR | Hybrid | hybrid ÷ GBN | hybrid ÷ SR | switches |
| --- | --- | --- | --- | --- | --- | --- |
| 0% | 16.2 s | 16.2 s | 16.2 s | 1.00 | 1.00 | 0.0 |
| 1% | 20.9 s | 19.6 s | 21.4 s | 0.98 | 0.92 | 5.0 |
| 5% | 42.5 s | 32.0 s | 33.3 s | 1.27 | 0.96 | 1.4 |
| 20% | 139.6 s | 64.6 s | 69.8 s | **2.00** | 0.93 | 1.0 |

Three things that matrix settled, and one it exposed:

- **The hybrid doubles pure GBN's throughput at 20% loss** and stays within 4–9% of pure SR.
  It never beats SR: it starts in GBN and has to earn the evidence to leave.
- **The switching decision is RTT-invariant** across 10/50/100/500 ms (E7) — an advantage of
  1.22–1.25× over GBN at every RTT. The calibration argued this from the estimator's
  definition; E7 measured it.
- **The MODE drain costs ≈0.17 s, about 1.5 RTT**, measured directly by the `fixed-hybrid`
  control, which performs the same handshakes while changing no mode.
- **It oscillates at 1–2% loss** — 5.0 switches where one is justified, and at 1% loss it is
  worse than both baselines. At the same physical loss the estimator reads 0.16–0.30 under
  GBN and 0.00 under SR, so the dead band sits *between* the two modes' scales instead of
  separating them. The 256 KiB calibration transfers ended before a second crossing could
  form. D9 stays frozen — changing it invalidates every recorded run — and this is reported
  as a limitation, not patched away.

`network/simulator.py` injects loss, one-way delay and jitter from a seeded RNG, so a
condition replays exactly from a recorded seed. `protocol/sr.py` implements Selective
Repeat: per-segment timers, out-of-order buffering, individual ACKs, and single-segment
retransmission.

The trade-off, measured rather than assumed — 1 MiB at 10% loss and 5% ACK loss, same
seed, same window:

| | retransmissions | time | integrity |
| --- | --- | --- | --- |
| GBN | 883 | 23.2 s | OK |
| SR | 167 | 19.7 s | OK |

That 5x gap under loss, against GBN's simpler state and cheaper clean-path behaviour, is
exactly what the hybrid controller is meant to arbitrate.

`protocol/hybrid.py` is the controller: it watches the loss estimator, applies dual
thresholds with hysteresis, and decides. It never performs the switch itself — the
endpoints drain the window and negotiate a MODE handshake, because GBN and SR disagree
about what an ACK means and no packet may ever be read under the wrong convention.

**Every decision D1–D14 is now frozen**: **D1** byte layout, **D2** checksum coverage,
**D3** segment size, **D4** sequence convention, **D5** GBN ACK semantics, **D6** SR ACK
semantics, **D7** baseline RTO, **D8** loss estimator, **D9** switching thresholds,
**D10** transition mechanism, **D11** window size, and — settled at T8.1 against a measured
pilot rather than against their standing recommendation — **D12** five trials per cell,
**D13** the seed policy, **D14** a 1 MiB transfer for the whole matrix.

The findings are written up in [`RESULTS.md`](RESULTS.md): which of the five hypotheses
held, where the hybrid is *worse* than a fixed strategy, and what the project does not
claim.

D9 was calibrated rather than guessed — 444 recorded transfers, written up in
[`experiments/results/calibration.md`](experiments/results/calibration.md). Three findings
worth knowing before reading any hybrid result:

- **The thresholds are readings of the loss *estimator*, not loss rates.** The estimator
  over-reads loss under GBN by four to five times, because one drop resends the whole
  window, so `SWITCH_HIGH = 0.10` fires at about 2% physical loss.
- **The hybrid never beats pure SR under sustained static loss** — it starts in GBN and must
  earn the evidence to leave. It beats GBN from 2% loss upward (to 1.32× there, 2.00× in the
  Phase 8 matrix) and completed every 20%-loss trial where pure GBN exhausted its retry
  budget and aborted — though at the larger Phase 8 file size and 100 ms RTT, GBN aborted
  nothing, so that last claim does not generalise.
- **The hysteresis count is the only real lever on oscillation**, and only a condition that
  *changes* can calibrate it. A count of 1 scored best under static and falling loss; under
  an alternating condition it made 60% more switches than the condition justified.

The logs are the deliverable as much as the code, so they are audited rather than assumed:
`metrics.py` derives every specs.md §20 metric from `events.csv` alone, and the tests assert
it agrees with what the endpoints computed while they ran. Every run records the
configuration it actually used — seed, impairment, derived RTO, file size, thresholds — with
the freeze state of each decision and the commit that produced it.

Reproducing any of it is one command each — see **Reproducing the results** below.

See the status snapshot in `tasks.md` for the current milestone map.

## Setup

**The protocol itself needs nothing but Python 3** — standard library only, no install step.
Clone the repository and it runs:

```bash
git clone https://github.com/Rectification0/Hybrid-ARQ.git
cd Hybrid-ARQ
python -c "import protocol, network; print('ready')"
```

The analysis stage — and only that — needs pandas, Matplotlib and pytest:

```bash
pip install -r requirements.txt
```

Optional, for the packet-level evidence: **Wireshark**, with the npcap *"Adapter for
loopback traffic capture"* option enabled at install time. Without it a local transfer is
invisible to every interface, since loopback traffic never reaches a normal adapter. To read
the captures with named fields, copy `tools/hybrid_arq.lua` into your personal Wireshark
plugin folder (`Help → About Wireshark → Folders → Personal Lua Plugins`) and press
Ctrl+Shift+L; `tshark` takes it per command with `-X lua_script:tools/hybrid_arq.lua`
instead.

Tested on Windows 11 with Python 3.12. Nothing in the protocol is platform-specific, but the
loopback capture device name in `experiments/capture_evidence.py` is (`\Device\NPF_Loopback`).

## Packet format

21-byte header, network byte order, `struct.Struct("!HBBBIIHHI")`:

```
offset  0      2   3    4      5         9     13      15              17
        MAGIC  VER TYPE FLAGS  SEQUENCE  ACK   WINDOW  PAYLOAD_LENGTH  CHECKSUM  PAYLOAD
        2B     1B  1B   1B     4B        4B    2B      2B              4B        0-1024B
```

MAGIC is `0x4841`, ASCII `"HA"`, so a packet is identifiable in the Wireshark ASCII pane
without a dissector. CHECKSUM is CRC-32 over the header with the checksum field zeroed
plus the payload — every byte, so corruption in *any* header field is caught. Sequence
numbers are segment indices starting at 0, not byte offsets.

Full definition, including the rationale for each frozen decision, is the module
docstring of `protocol/packet.py`. `specs.md` §5 and §16 record the same values.

## Running a transfer

Two terminals:

```
python receiver.py --port 8888 --output received.bin
python sender.py --file sample.bin --host 127.0.0.1 --port 8888 --mode saw --window 8
```

`--mode` accepts `saw`, `gbn`, `sr`, `hybrid` and `fixed-hybrid`, and all five work
today. `gbn` (the default) and `sr` are the measured baselines; `hybrid` starts in
`config.DEFAULT_MODE` and switches at runtime; `fixed-hybrid` runs the same controller and
the same MODE handshakes but never changes mode, which is what isolates switching *cost*
from switching *benefit*. `saw` is the Phase 2 placeholder, not a system under evaluation.
`--window` sets the number of segments in flight and is ignored by `saw`. Each run writes
`logs/<run_id>/<endpoint>/events.csv` and `summary.json`.

A hybrid run under loss:

```
python sender.py --file sample.bin --mode hybrid --window 8 --loss 0.10 --seed 7
```

The `SWITCH` rows in `events.csv` carry the old and new mode, the loss estimate that
triggered the change, and the handshake epoch; the `MODE` rows show the exchange itself,
including a handshake that was refused or abandoned.

### Running under controlled loss

```
python sender.py --file sample.bin --mode sr --window 8 \
    --loss 0.10 --ack-loss 0.05 --rtt 50 --jitter 5 --seed 99
python sender.py --file sample.bin --mode gbn --loss-schedule 5:0.2,10:0.0
```

`--seed` makes a condition reproducible: the same seed replays the same drop sequence
(RP-03). `--rtt` is the round trip, half applied on each direction, so the receiver needs
the matching `--rtt` to carry the ACK half. `--loss-schedule` is `t:rate,t:rate` in
seconds, for the dynamic-loss experiments.

## Reproducing the results

Four things can be regenerated, and only the first re-runs a transfer.

**One experiment, or the whole matrix** (E1–E8, 195 runs, about 2.5 hours):

```bash
python experiments/run_experiment.py --config experiments/configs/E4.json
python experiments/run_experiment.py --all --dry-run       # print the plan, run nothing
python experiments/run_experiment.py --all --resume        # skip what is already recorded
python experiments/run_experiment.py --verify              # integrity + raw-log fingerprints
```

`--resume` makes the matrix interruptible: the index is appended per run, so a stopped sweep
resumes where it left off. Each run writes raw logs under `logs/experiments/<run_id>/` and
one row to `experiments/results/experiment_runs.csv`. The whole matrix replays from two
recorded numbers — the base seed in each config, and the commit in every `summary.json`.

**The aggregate and the graphs** (reads the recorded runs; never re-runs one):

```bash
python experiments/analyze_results.py                  # aggregate.csv + all eight plots
python experiments/analyze_results.py --check-logs     # re-derive all 195 runs from events.csv
```

**The Wireshark evidence set** (re-runs five short transfers under tshark):

```bash
python experiments/capture_evidence.py                 # all five captures + the manifest
python experiments/capture_evidence.py --only clean_gbn
python experiments/capture_evidence.py --manifest-only # rebuild captures/README.md in place

tshark -r captures/lossy_gbn_range_retx.pcapng -X lua_script:tools/hybrid_arq.lua \
       -T fields -e frame.number -e hybridarq.type_name -e hybridarq.seq -e hybridarq.ack
```

**The demonstration** — the thirteen steps of `specs.md` §28, performed in order:

```bash
python experiments/demonstrate.py                      # ~2 minutes, writes a transcript
```

## Tests

```
python -m pytest -q
```

`pytest.ini` pins collection to `test/`, so a bare `pytest` runs the suite and nothing
else. `smoke_udp.py` is a script you run by hand, not a test module.

## Which receiver is the real one

The M1 spike listener now lives at **`test/spike_receiver.py`**, so the name
`receiver.py` stays free for the real receiver that T2.2 writes at the project root. The
spike has no windows, no ACKs, no retransmission and writes no file — it decodes packets
and prints their headers, nothing more. Nothing should import it.

`smoke_udp.py` (renamed from `test_udp.py`, which pytest would otherwise try to collect as
a test module) is kept deliberately as the environment smoke test. Per T1.7 it no longer
sends `TEST_PACKET_n` strings: it sends real encoded packets, so the same Wireshark check
that proved connectivity in M1 now also proves the header is readable at fixed offsets.

To run the smoke test, in two terminals:

```
python test/spike_receiver.py     # terminal 1 — the M1 spike listener
python smoke_udp.py               # terminal 2
```

Wireshark filter: `udp.port == 8888`. On Windows, loopback traffic needs the npcap
"Adapter for loopback traffic capture" interface. `captures/t1_7_packet_format.pcapng`
is a recorded example.

## Layout

```
sender.py              transfer lifecycle and hybrid decisions     ✅
receiver.py            validate, reconstruct, verify integrity     ✅
config.py              every tunable parameter (specs.md §15)      ✅
protocol/
  packet.py            wire format, checksum                       ✅
  strategy.py          strategy interface + stop-and-wait          ✅
  gbn.py               Go-Back-N: window, one timer, range retx     ✅
  sr.py                Selective Repeat: per-segment timers + buffer ✅
  hybrid.py            statistics, loss estimator, threshold rule  ✅
network/
  udp.py               socket creation; Windows ICMP-reset contract ✅
  simulator.py         loss, delay, jitter; seeded and reproducible ✅
experiments/
  calibrate_thresholds.py  the D9 calibration sweep                ✅
  run_experiment.py    automated runs, E1–E8 matrix                ✅
  analyze_results.py   aggregation, metrics, graphs                ✅
  capture_evidence.py  the scripted Wireshark evidence set         ✅
  configs/             per-experiment configuration (E1–E8)        ✅
  results/             calibration + matrix evidence, aggregate    ✅
tools/hybrid_arq.lua   optional Wireshark dissector (T9.5)         ✅
eventlog.py            events.csv / summary.json emitter            ✅
metrics.py             specs.md §20 derived from events.csv alone   ✅
logs/  captures/  plots/   run output — git-ignored; the curated figure
                       and capture sets are force-added as M10 evidence
test/                  unit, integration and failure tests         ✅
  spike_receiver.py    M1 smoke-test listener, not the real one    ✅
```

## Configuration

Every tunable lives in `config.py`; no protocol module may hard-code one. Values still
open are marked `# NOT FROZEN` with their decision ID (D1–D14) and the task that freezes
them. Freezing a value means recording it in `specs.md` §16 and moving `design.md` §12
from Proposed to Frozen (T10.2). The loss estimator and the switching thresholds in
particular must be frozen *before* the final experiments — changing either one afterwards
invalidates every recorded run.

## Build order

The rule from the source specification §41: **both ARQ baselines must be correct before
the hybrid controller is written.** The controller is never to be debugged at the same
time as the mechanism underneath it. That ordering was held: Phase 5 began only once T3.7
and T4.8 passed for both baselines.
