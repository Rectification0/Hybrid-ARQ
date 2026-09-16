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
SEGMENT_SIZE = 1024         # FROZEN — D3 (T1.3)

# Sequence numbers are segment indices, not byte offsets; first DATA segment is 0.
INITIAL_SEQUENCE = 0        # FROZEN — D4 (T1.3)

# HEADER_SIZE and MAX_DATAGRAM_SIZE deliberately do NOT live here. They are
# derived from the frozen struct format, not tunable, so protocol.packet owns
# them — duplicating the value 21 in two files is exactly how a wire format
# drifts. Import them from protocol.packet.

# ---------------------------------------------------------------------------
# Windows (specs.md §15, §16.7 — decision D11)
# ---------------------------------------------------------------------------

WINDOW_SIZE = 8             # FROZEN — D11 (T4.9)

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

RTO_RTT_MULTIPLIER = 4.0    # FROZEN — D7 (T4.9)
RTO_FLOOR_S = 0.200         # FROZEN — D7 (T4.9)
RTO_S = 0.200               # effective value; set per run from baseline_rto() (D7)


def baseline_rto(rtt_ms: float) -> float:
    """Baseline retransmission timeout in seconds for a condition's RTT.

    D7: ``max(RTO_RTT_MULTIPLIER x RTT, RTO_FLOOR_S)``, computed once per
    experimental condition and then held fixed for the entire run. The result is
    recorded in summary.json so the run can be reproduced exactly (RP-01).
    """
    return max(RTO_RTT_MULTIPLIER * (rtt_ms / 1000.0), RTO_FLOOR_S)


# Retry budgets. specs.md §13 requires a transfer to abort after a configurable
# retry policy and a receiver that never appears to be reported rather than
# waited on forever — so every wait below is bounded (T2.4).
CONTROL_RETRY_LIMIT = 5             # START / FIN attempts before giving up
CONTROL_RETRY_TIMEOUT_S = 1.0       # per-attempt wait for START_ACK / FIN_ACK
DATA_RETRY_LIMIT = 10               # retransmissions of one segment before abort
RECEIVER_IDLE_TIMEOUT_S = 30.0      # receiver gives up if nothing arrives
RECEIVER_LINGER_S = 2.0             # keep re-ACKing a repeated FIN after FIN_ACK

# ---------------------------------------------------------------------------
# Hybrid controller (specs.md §10, §16.9-§16.11 — decisions D8, D9)
# ---------------------------------------------------------------------------

# Loss estimator (D8): the fraction of the last LOSS_WINDOW_SIZE *segment
# outcomes* that required a retransmission, each outcome recorded when its
# segment is acknowledged. Bounded in [0, 1], so thresholds read directly as
# loss rates. See protocol/hybrid.py for the formula, the rationale and the
# known GBN bias; specs.md §16.9 records it. Changing this value changes every
# switching decision and invalidates every experiment already run.
LOSS_WINDOW_SIZE = 50       # FROZEN — D8 (T5.2)

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

# Starting mode for a hybrid transfer. Both endpoints read this same value, so
# the receiver builds the right strategy from a START that names only "hybrid" —
# the starting mode needs no field on the wire and the two sides cannot disagree
# about where the transfer began (T5.4).
DEFAULT_MODE = "GBN"

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


# ---------------------------------------------------------------------------
# Run capture
# ---------------------------------------------------------------------------


def snapshot(overrides: dict | None = None) -> dict:
    """Every specs.md §15 parameter as a flat dict, for summary.json.

    A run is only reproducible if the configuration it actually ran under is
    recorded alongside its results (RP-01, RP-02), so this is written into
    every run's summary rather than left implicit in the source file.

    ``overrides`` replaces the module defaults with what a run actually used:
    the seed and impairment come from the CLI, the RTO is derived per condition
    (D7), and a hybrid run's thresholds may be supplied per sweep (T7.1). An
    unknown key raises rather than being added silently — a typo that invented a
    parameter would leave the recorded config quietly wrong, which is worse than
    not recording it at all (T6.3).
    """
    values = _defaults()
    for key, value in (overrides or {}).items():
        if key not in values:
            raise KeyError(
                f"{key!r} is not a specs.md §15 parameter; add it to snapshot() "
                f"first rather than recording an unknown key")
        values[key] = value
    return values


def _defaults() -> dict:
    """The module's own values, before a run's overrides are applied."""
    return {
        "host": HOST,
        "port": PORT,
        "segment_size": SEGMENT_SIZE,
        "initial_sequence": INITIAL_SEQUENCE,
        "window_size": WINDOW_SIZE,
        "sr_receive_window_size": SR_RECEIVE_WINDOW_SIZE,
        "rto_s": RTO_S,
        "rto_rtt_multiplier": RTO_RTT_MULTIPLIER,
        "rto_floor_s": RTO_FLOOR_S,
        "control_retry_limit": CONTROL_RETRY_LIMIT,
        "control_retry_timeout_s": CONTROL_RETRY_TIMEOUT_S,
        "data_retry_limit": DATA_RETRY_LIMIT,
        "receiver_idle_timeout_s": RECEIVER_IDLE_TIMEOUT_S,
        "loss_window_size": LOSS_WINDOW_SIZE,
        "switch_high": SWITCH_HIGH,
        "switch_low": SWITCH_LOW,
        "hysteresis_count": HYSTERESIS_COUNT,
        "evaluation_interval_segments": EVALUATION_INTERVAL_SEGMENTS,
        "min_mode_residence_s": MIN_MODE_RESIDENCE_S,
        "default_mode": DEFAULT_MODE,
        "loss_rate": LOSS_RATE,
        "ack_loss_rate": ACK_LOSS_RATE,
        "rtt_ms": RTT_MS,
        "jitter_ms": JITTER_MS,
        "loss_schedule": LOSS_SCHEDULE,
        "random_seed": RANDOM_SEED,
        "trial_count": TRIAL_COUNT,
        "transfer_file_size_bytes": TRANSFER_FILE_SIZE_BYTES,
    }


#: Which of the decisions in design.md §12 are settled, and where each one's
#: value actually lives (T6.3, RP-08). Values that belong to another module are
#: named by source rather than copied here: duplicating the struct format or the
#: ACK convention into config.py is exactly how the two drift apart. What this
#: records is the *freeze state* — which is what a reader of an old run needs in
#: order to know whether a later result is comparable with it.
_DECISIONS = (
    ("D1", "packet byte layout and endianness", "T1.1", "protocol/packet.py", None),
    ("D2", "checksum algorithm and coverage", "T1.2", "protocol/packet.py", None),
    ("D3", "maximum DATA payload", "T1.3", "config.SEGMENT_SIZE", "segment_size"),
    ("D4", "initial sequence convention", "T1.3", "config.INITIAL_SEQUENCE", "initial_sequence"),
    ("D5", "GBN ACK semantics", "T3.1", "protocol/gbn.py", None),
    ("D6", "SR ACK semantics", "T4.4", "protocol/sr.py", None),
    ("D7", "baseline RTO policy", "T4.9", "config.baseline_rto()", "rto_s"),
    ("D8", "loss estimator", "T5.2", "protocol/hybrid.py", "loss_window_size"),
    ("D10", "mode transition mechanism", "T5.4", "sender.py / receiver.py", None),
    ("D11", "primary window size", "T4.9", "config.WINDOW_SIZE", "window_size"),
)

#: Still open, with the task that closes each. D9 is the experiment-invalidating
#: one: it is deliberately left to Phase 7 so the thresholds are calibrated
#: against real data rather than guessed before any exists.
_OPEN_DECISIONS = (
    ("D9", "switching thresholds and hysteresis", "T7.2"),
    ("D12", "experiment repetition count", "T8.1"),
    ("D13", "random-seed policy", "T8.1"),
    ("D14", "file size(s)", "T8.1"),
)


def frozen_decisions(overrides: dict | None = None) -> dict:
    """Freeze state of every design.md §12 decision, for summary.json.

    Frozen values that live in config.py are read back through ``snapshot`` so
    a run's *effective* value is recorded, not the module default.
    """
    values = snapshot(overrides)
    decisions = {
        identifier: {
            "decision": what,
            "status": "frozen",
            "frozen_by": task,
            "source": source,
            "value": values[key] if key else None,
        }
        for identifier, what, task, source, key in _DECISIONS
    }
    decisions.update({
        identifier: {"decision": what, "status": "open", "frozen_by": task,
                     "source": None, "value": None}
        for identifier, what, task in _OPEN_DECISIONS
    })
    return dict(sorted(decisions.items(), key=lambda item: int(item[0][1:])))
