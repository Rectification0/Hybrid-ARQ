"""Go-Back-N sender and receiver strategy.

NOT YET IMPLEMENTED — scaffold only. Implemented by T3.1-T3.5.

Sender state: base, next_seq, window, outstanding buffer, one timer on the
oldest unacknowledged segment. Cumulative ACKs; base never moves backward.
Receiver state: a single expected_seq, no buffering, re-ACK on out-of-order.

Knows about windows, timers and ACK rules; must not know which mode is
"better", nor anything about thresholds or byte layout (design.md §1.1).
"""
