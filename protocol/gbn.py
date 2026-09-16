"""Go-Back-N sender and receiver strategy (T3.1-T3.5).

FROZEN DECISION
===============

D5 — GBN ACK semantics (T3.1, specs.md §16.5, SEQ-03, GBN-01)
--------------------------------------------------------------
**An ACK carries the highest in-order sequence number received.** ``ACK n``
therefore means "every segment 0..n has arrived", and the sender may advance
``base`` to ``n + 1``. ACKs are cumulative (GBN-01): one ACK acknowledges every
segment up to and including its own number, so a lost ACK is repaired by the
next one rather than by a retransmission.

The alternative convention — ACK carries the *next expected* sequence — is
equally workable. What matters (SEQ-03) is that one is chosen, written down, and
used identically on both endpoints. This one was chosen because "highest
received" reads directly off a Wireshark capture without the reader having to
subtract one to see what actually arrived.

Its one awkward corner, and the reason it needs stating: before any segment has
arrived in order there *is* no highest-in-order sequence, and 0 already means
"segment 0 arrived". The receiver therefore **sends no ACK at all** until the
first in-order segment lands. That is safe because the sender's retransmission
timer is already the mechanism covering a window whose first segment was lost;
silence and a lost ACK are handled the same way.

An ACK below ``base`` is stale and is ignored — it must never move ``base``
backward (GBN-03). An ACK at or above ``next_seq`` acknowledges something never
sent and is likewise ignored.

MECHANISM
=========
Sender (specs.md §7.1): ``base``, ``next_seq``, ``window_size``, the outstanding
segment payloads (held in the shared transfer state, not here — see
strategy.py), and **one timer, on the oldest outstanding segment**.

* Send rule: transmit while ``next_seq < base + window_size`` and segments remain.
* Timeout rule: retransmit the **entire** outstanding range ``base .. next_seq-1``
  and restart the timer (GBN-04). This is the defining cost of GBN and the thing
  the hybrid controller exists to avoid under high loss: one lost segment forces
  the retransmission of every correctly received segment behind it.

Receiver: a single ``expected_seq``, no buffer. In-order DATA is delivered
straight through; anything else is discarded and the last in-order sequence is
re-ACKed. Discarding correctly-received out-of-order data is not an oversight,
it is the state-simplicity side of the trade-off being measured (specs.md §7.4).
"""

from __future__ import annotations

from protocol.strategy import (AckResult, ReceiverResult, ReceiverTransferState,
                               SenderTransferState, TimerTracker)


class GbnSender(TimerTracker):
    """Go-Back-N sending rules: one window, one timer, range retransmission."""

    name = "GBN"

    def __init__(self, state: SenderTransferState, rto: float):
        super().__init__()
        if state.window_size < 1:
            raise ValueError(f"window_size must be >= 1, got {state.window_size}")
        self.state = state
        self.rto = rto
        self._timer_started: float | None = None
        self._timer_seq: int | None = None

    # -- timer -------------------------------------------------------------

    def _start_timer(self, seq: int, now: float) -> None:
        """Arm the timer on the oldest outstanding segment (TO-02)."""
        self._timer_started = now
        self._timer_seq = seq
        self.note_timer_start(seq)

    def _stop_timer(self) -> None:
        if self._timer_seq is not None:
            self.note_timer_stop(self._timer_seq)
        self._timer_started = None
        self._timer_seq = None

    def next_timeout(self, now: float) -> float | None:
        """Seconds until the timer expires, so the caller can size its wait."""
        if self._timer_started is None:
            return None
        return max(0.0, self._timer_started + self.rto - now)

    # -- strategy interface ------------------------------------------------

    def packets_to_send(self, now: float) -> list[int]:
        """Every sequence the window currently permits, sent for the first time."""
        limit = min(self.state.base + self.state.window_size,
                    self.state.total_segments)
        fresh = list(range(self.state.next_seq, limit))
        if not fresh:
            return []

        if self._timer_started is None:
            # The window was empty; this send makes `base` the oldest outstanding.
            self._start_timer(self.state.base, now)
        self.state.next_seq = limit
        return fresh

    def on_ack(self, ack_number: int, now: float) -> AckResult:
        """Cumulative ACK processing (GBN-02, GBN-03).

        ``base`` only ever moves forward. A stale ACK, a duplicate, or an ACK for
        a segment never sent all leave the window exactly where it was.
        """
        if ack_number < self.state.base:
            return AckResult(duplicate=True, stale=True)
        if ack_number >= self.state.next_seq:
            # Acknowledges something that was never transmitted.
            return AckResult(duplicate=True, stale=True)

        newly_acked = list(range(self.state.base, ack_number + 1))
        self.state.acked.update(newly_acked)
        self.state.base = ack_number + 1

        self._stop_timer()
        if self.state.base != self.state.next_seq:
            # Segments remain outstanding: the timer moves to the new oldest.
            self._start_timer(self.state.base, now)
        return AckResult(newly_acked=newly_acked)

    def on_timeout(self, now: float) -> list[int]:
        """Retransmit the whole outstanding range and restart the timer (GBN-04)."""
        if self._timer_started is None:
            return []
        if now - self._timer_started < self.rto:
            return []

        outstanding = list(range(self.state.base, self.state.next_seq))
        if not outstanding:
            self._stop_timer()
            return []

        self._stop_timer()
        self._start_timer(self.state.base, now)
        return outstanding

    def all_acked(self) -> bool:
        return self.state.base >= self.state.total_segments


class GbnReceiver:
    """Single ``expected_seq``, no buffering, re-ACK on anything out of order."""

    name = "GBN"

    def __init__(self, state: ReceiverTransferState):
        self.state = state

    def _cumulative_ack(self) -> int | None:
        """Highest in-order sequence received, or None if nothing has been (D5)."""
        if self.state.expected_seq == 0:
            return None
        return self.state.expected_seq - 1

    def on_data(self, seq: int, payload: bytes) -> ReceiverResult:
        if seq == self.state.expected_seq:
            self.state.expected_seq += 1
            return ReceiverResult(deliver=[(seq, payload)], ack=seq)

        # Out of order or already delivered. GBN keeps no buffer, so the packet
        # is discarded and the last in-order sequence is repeated; the sender
        # will go back to it when its timer fires.
        return ReceiverResult(ack=self._cumulative_ack(),
                              duplicate=seq < self.state.expected_seq)
