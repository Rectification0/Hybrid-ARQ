"""Single source of every tunable parameter (specs.md §15).

Rule (T0.3): no protocol module may contain a hard-coded tunable. If a value
could reasonably be changed for an experiment, it lives here and is imported.

Two kinds of value appear below:

  FROZEN      settled; changing one invalidates already-recorded experiments.
  NOT FROZEN  a *recommendation* from design.md, carried here so the code has
              something to run against. Every one is marked `# NOT FROZEN` with
              the decision ID (D1-D14) and the task that freezes it. They are
              not final until specs.md §16 records them and design.md §12 moves
              from Proposed to Frozen (T10.2).

Anything marked "per experiment" is supplied by the experiment runner (T8.2),
never edited here for a one-off run — the value is recorded in summary.json so
the run stays reproducible (RP-01, RP-02).
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
CAPTURE_DIR = PROJECT_ROOT / "captures"
PLOT_DIR = PROJECT_ROOT / "plots"
EXPERIMENT_CONFIG_DIR = PROJECT_ROOT / "experiments" / "configs"

# ---------------------------------------------------------------------------
# Endpoint (specs.md §15)
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"          # receiver address
PORT = 8888                 # UDP port; matches the M1 Wireshark filter udp.port == 8888

# ---------------------------------------------------------------------------
# Packet layer (specs.md §16.2, §16.3 — decisions D3, D4)
# ---------------------------------------------------------------------------

# Max DATA payload in bytes. 21-byte header + 1024 = 1045, well under a 1500-byte
# Ethernet MTU, so no IP fragmentation muddies the Wireshark evidence (§22).
SEGMENT_SIZE = 1024         # NOT FROZEN — D3, frozen by T1.3

# Sequence numbers are segment indices, not byte offsets; first DATA segment is 0.
INITIAL_SEQUENCE = 0        # NOT FROZEN — D4, frozen by T1.3

# Header is 21 bytes (design.md §3.1). Kept here so the receiver can size its
# socket reads without importing the packet layer's struct definition.
HEADER_SIZE = 21            # NOT FROZEN — D1, frozen by T1.1
MAX_DATAGRAM_SIZE = HEADER_SIZE + SEGMENT_SIZE

# ---------------------------------------------------------------------------
# Windows (specs.md §15, §16.7 — decision D11)
# ---------------------------------------------------------------------------

WINDOW_SIZE = 8             # NOT FROZEN — D11, frozen by T4.9

# SR requires the receive window to be at most half the sequence space (SR-10).
# Asserted in T4.7 rather than here, so a bad config fails a test, not an import.
SR_RECEIVE_WINDOW_SIZE = WINDOW_SIZE

# ---------------------------------------------------------------------------
# Timeout policy (specs.md §11, §16.8 — decision D7)
# ---------------------------------------------------------------------------
#
# One fixed RTO per run, identical for GBN, SR and Hybrid, so the comparison
# isolates retransmission strategy rather than timer tuning (TO-01, §11). No
# adaptive RTO: RTT samples are recorded for analysis only, never fed back.
#
# A single absolute constant cannot serve both the 10 ms and 500 ms RTT
# conditions of E7, so the baseline is derived per condition and then held fixed
# for the whole run.

RTO_RTT_MULTIPLIER = 4.0    # NOT FROZEN — D7, frozen by T4.9
RTO_FLOOR_S = 0.200         # NOT FROZEN — D7, frozen by T4.9
RTO_S = 0.200               # NOT FROZEN — effective value; set per run from baseline_rto()


def baseline_rto(rtt_ms: float) -> float:
    """Baseline retransmission timeout in seconds for a condition's RTT.

    D7: ``max(RTO_RTT_MULTIPLIER x RTT, RTO_FLOOR_S)``, computed once per
    experimental condition and then held fixed for the entire run. The result is
    recorded in summary.json so the run can be reproduced exactly (RP-01).
    """
    return max(RTO_RTT_MULTIPLIER * (rtt_ms / 1000.0), RTO_FLOOR_S)


# Control-packet retry budget before a transfer is abandoned with a clear error
# rather than hanging (§13, T2.4).
CONTROL_RETRY_LIMIT = 5             # NOT FROZEN — frozen by T2.4
CONTROL_RETRY_TIMEOUT_S = 1.0       # NOT FROZEN — frozen by T2.4
RECEIVER_IDLE_TIMEOUT_S = 30.0      # NOT FROZEN — frozen by T2.4

# ---------------------------------------------------------------------------
# Hybrid controller (specs.md §10, §16.9-§16.11 — decisions D8, D9)
# ---------------------------------------------------------------------------

# Loss estimator (D8): fraction of the last LOSS_WINDOW_SIZE DATA transmission
# outcomes that required a retransmission. Bounded in [0, 1], so thresholds read
# directly as loss rates. Known GBN bias must be documented before freezing
# (T5.2) — once frozen, changing it means re-running every experiment.
LOSS_WINDOW_SIZE = 50       # NOT FROZEN — D8, frozen by T5.2

# Dual thresholds with a dead band: SWITCH_LOW < SWITCH_HIGH. Both sit inside
# the loss grid of §17.1, so the experimental matrix contains conditions on
# either side of the boundary.
SWITCH_HIGH = 0.05          # NOT FROZEN — D9, enter SR above this; frozen by T7.2
SWITCH_LOW = 0.02           # NOT FROZEN — D9, return to GBN below this; frozen by T7.2

# Consecutive confirming observations required before a switch, so a single
# unlucky burst cannot oscillate the mode (HY-09).
HYSTERESIS_COUNT = 3        # NOT FROZEN — D9, frozen by T7.2

# Evaluation cadence, in acked segments, and the minimum time a mode must be
# held before it may be left again (HY-03).
EVALUATION_INTERVAL_SEGMENTS = 20   # NOT FROZEN — D9, frozen by T7.2
MIN_MODE_RESIDENCE_S = 1.0          # NOT FROZEN — D9, frozen by T7.2

DEFAULT_MODE = "GBN"        # starting mode for a hybrid transfer

# ---------------------------------------------------------------------------
# Per-experiment values (specs.md §15, §17, §19)
# ---------------------------------------------------------------------------
#
# Defaults describe a clean local run. The experiment runner overrides them per
# condition and records what it used; do not edit these for a single run.

LOSS_RATE = 0.0             # forward-direction DATA loss, per experiment (§17.1)
ACK_LOSS_RATE = 0.0         # reverse-direction ACK loss, per experiment (§17.3)
RTT_MS = 0.0                # emulated round-trip time in ms, per experiment (§17.2)
JITTER_MS = 0.0             # one-way delay jitter in ms, per experiment (§17.2)
LOSS_SCHEDULE = None        # step function for dynamic conditions, E8 (§17.3)

RANDOM_SEED = 0             # per trial; derived by the runner and recorded (RP-03)

# Experiment scale — all three still open (specs.md §16.13-§16.15).
TRIAL_COUNT = 5             # NOT FROZEN — D13, frozen by T8.1
TRANSFER_FILE_SIZE_BYTES = 1 * 1024 * 1024   # NOT FROZEN — D14, frozen by T8.1
EXPERIMENT_ABORT_TIMEOUT_S = 300.0           # NOT FROZEN — frozen by T8.2
