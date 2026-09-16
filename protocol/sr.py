"""Selective Repeat sender and receiver strategy.

NOT YET IMPLEMENTED — scaffold only. Implemented by T4.4-T4.6.

Sender state: per-segment ACK tracking, per-segment timers, retained unacked
payloads, single-segment retransmission.
Receiver state: receive window, out-of-order buffering, individual ACKs,
in-order delivery once a gap fills, duplicate suppression.

The receive window must not exceed half the sequence space (asserted in T4.7).
"""
