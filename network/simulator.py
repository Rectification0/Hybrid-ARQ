"""Controlled network impairment: loss, one-way delay, jitter.

NOT YET IMPLEMENTED — scaffold only. Implemented by T4.1-T4.3.

``ImpairedSocket`` wraps a UDP socket and applies impairment in both directions
(DATA and ACK) from a dedicated seeded RNG, so a recorded seed reproduces an
identical drop sequence (RP-03). Delayed delivery is non-blocking.

Two invariants:
  * 0% loss and 0 delay must be behaviourally identical to a raw socket.
  * simulated drops log DROP, never CHECKSUM_FAIL — the two are distinct
    events in the analysis (IN-04).
"""
