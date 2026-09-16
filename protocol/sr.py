"""Selective Repeat sender and receiver strategy (T4.4-T4.6).

FROZEN DECISION
===============

D6 — SR ACK semantics (T4.4, specs.md §16.6, SEQ-03)
-----------------------------------------------------
**An ACK acknowledges exactly the one segment it names, and nothing else.**
``ACK n`` means "segment n arrived" — it says nothing about n-1. Duplicate ACKs
are idempotent. ``send_base`` advances past the contiguous run of acknowledged
segments, so it can sit still while segments beyond it are acknowledged
individually.

This is deliberately *not* D5. Under GBN, ``ACK n`` means "0..n arrived"; under
SR it means "n arrived". The same four bytes on the wire mean different things
in the two modes, which is exactly why a mode switch cannot happen with packets
in flight: an ACK sent under one convention and read under the other would
silently acknowledge segments that never arrived. The quiescent-boundary
handshake in T5.4 exists for this reason (design.md §6.3).

Because each ACK stands alone, a lost ACK is **not** repaired by the next one —
unlike GBN, where the following cumulative ACK covers it. It is repaired by that
segment's own timer expiring and the segment being sent again. That is the cost
SR pays for not retransmitting the whole window.

MECHANISM
=========
Sender (specs.md §8.1): ``send_base``, ``next_seq``, ``window_size``, and **one
timer per outstanding segment** — the essential structural difference from GBN.
A timeout retransmits only the segment that timed out and restarts only that
timer (SR-10). Payloads live in the shared transfer state, not in the strategy,
so they survive a mode switch (design.md §4).

Receiver (specs.md §8.2): ``expected_seq`` plus an out-of-order buffer for
segments within ``[expected_seq, expected_seq + window_size)``. Each accepted
segment is ACKed individually and buffered; when a gap fills, the whole
contiguous run is delivered at once (SR-08).

WINDOW CONSTRAINT (SR-10 corollary)
====================================
SR requires both windows to be no larger than half the sequence space: beyond
that, a retransmission of an old segment is indistinguishable from a new one.
With a 4-byte sequence number and wraparound out of scope (SEQ-04) this cannot
bite today, but it is asserted so a future wraparound change fails loudly
instead of corrupting a file quietly.
"""

from __future__ import annotations

from protocol.packet import MAX_SEQUENCE
from protocol.strategy import (AckResult, ReceiverResult, ReceiverTransferState,
                               SenderTransferState, TimerTracker)

#: Half the sequence space. A window above this breaks SR's ability to tell a
#: retransmission from a new segment once sequence numbers wrap.
MAX_SR_WINDOW = (MAX_SEQUENCE + 1) // 2


def _check_window(window_size: int) -> None:
    if window_size < 1:
        raise ValueError(f"window_size must be >= 1, got {window_size}")
    if window_size > MAX_SR_WINDOW:
        raise ValueError(
            f"SR window {window_size} exceeds half the sequence space "
            f"({MAX_SR_WINDOW}); a retransmission would be indistinguishable "
            f"from a new segment"
        )


class SrSender(TimerTracker):
    """Per-segment ACK tracking, per-segment timers, single-segment retransmission."""

    name = "SR"

    def __init__(self, state: SenderTransferState, rto: float):
        super().__init__()
        _check_window(state.window_size)
        self.state = state
        self.rto = rto
        #: seq -> deadline. One entry per segment in flight (SR-01), which is
        #: what lets a timeout name a single segment instead of a range.
        self._deadlines: dict[int, float] = {}

    # -- timers ------------------------------------------------------------

    def _arm(self, seq: int, now: float) -> None:
        self._deadlines[seq] = now + self.rto
        self.note_timer_start(seq)

    def _disarm(self, seq: int) -> None:
        if self._deadlines.pop(seq, None) is not None:
            self.note_timer_stop(seq)

    def next_timeout(self, now: float) -> float | None:
        if not self._deadlines:
            return None
        return max(0.0, min(self._deadlines.values()) - now)

    # -- strategy interface ------------------------------------------------

    def packets_to_send(self, now: float) -> list[int]:
        limit = min(self.state.base + self.state.window_size,
                    self.state.total_segments)
        fresh = list(range(self.state.next_seq, limit))
        for seq in fresh:
            self._arm(seq, now)
        self.state.next_seq = limit
        return fresh

    def on_ack(self, ack_number: int, now: float) -> AckResult:
        """Acknowledge exactly one segment (D6, SR-01).

        ``send_base`` then advances over whatever contiguous run is now
        complete, which may be nothing at all — acknowledging segment 5 while 3
        is still missing moves no boundary.
        """
        if ack_number < self.state.base:
            return AckResult(duplicate=True, stale=True)
        if ack_number >= self.state.next_seq:
            return AckResult(duplicate=True, stale=True)   # never sent
        if ack_number in self.state.acked:
            return AckResult(duplicate=True)               # idempotent

        self.state.acked.add(ack_number)
        self._disarm(ack_number)

        while self.state.base in self.state.acked:
            self.state.base += 1
        return AckResult(newly_acked=[ack_number])

    def on_timeout(self, now: float) -> list[int]:
        """Return only the segments whose own timers expired (SR-10).

        The contrast with GBN is the whole point of the comparison: GBN answers
        this question with the entire outstanding range.
        """
        expired = sorted(seq for seq, deadline in self._deadlines.items()
                         if now >= deadline)
        for seq in expired:
            self._deadlines[seq] = now + self.rto
            self.note_timer_stop(seq)
            self.note_timer_start(seq)
        return expired

    def all_acked(self) -> bool:
        return self.state.base >= self.state.total_segments

    # -- inspection (used by tests and, later, the hybrid statistics) -------

    @property
    def outstanding(self) -> list[int]:
        return sorted(self._deadlines)


class SrReceiver:
    """Receive window, out-of-order buffering, individual ACKs, gap-fill delivery."""

    name = "SR"

    def __init__(self, state: ReceiverTransferState, window_size: int):
        _check_window(window_size)
        self.state = state
        self.window_size = window_size
        #: seq -> payload, for segments received ahead of the gap (SR-06).
        self.buffer: dict[int, bytes] = {}

    def on_data(self, seq: int, payload: bytes) -> ReceiverResult:
        expected = self.state.expected_seq

        if seq < expected:
            # Already delivered. Re-ACK it: the sender only learns this segment
            # arrived from its own ACK, so a lost one would otherwise have it
            # retransmitting until the retry budget runs out (SR-09).
            return ReceiverResult(ack=seq, duplicate=True)

        if seq >= expected + self.window_size:
            # Beyond the receive window. Deliberately *not* ACKed: under D6 an
            # ACK asserts possession of that exact segment, so acknowledging one
            # that was never accepted would let the sender retire a segment the
            # receiver does not have.
            return ReceiverResult()

        if seq in self.buffer:
            return ReceiverResult(ack=seq, duplicate=True)

        self.buffer[seq] = payload                       # SR-05, SR-06

        delivered: list[tuple[int, bytes]] = []
        while self.state.expected_seq in self.buffer:
            ready = self.state.expected_seq
            delivered.append((ready, self.buffer.pop(ready)))
            self.state.expected_seq += 1                 # SR-08

        return ReceiverResult(deliver=delivered, ack=seq,      # SR-07
                              buffered=not delivered)

    @property
    def buffered(self) -> list[int]:
        return sorted(self.buffer)
