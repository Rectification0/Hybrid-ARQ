"""The ARQ strategy interface, and the Phase 2 stop-and-wait implementation.

design.md §4 specifies that GBN and SR both sit behind one common interface so
``hybrid.py`` can swap which implementation is active without touching lifecycle
code. This module is that interface's home; ``gbn.py`` (T3.2) and ``sr.py``
(T4.5) implement it, and ``sender.py`` / ``receiver.py`` drive it generically.

Layering (design.md §1.1): a strategy knows about windows, timers and ACK rules.
It must not know which mode is "better", what a threshold is, or how a packet is
laid out in bytes. It never touches a socket or a file — it is asked what to
send and told what arrived, and answers in sequence numbers.

The transfer state is deliberately held *outside* the strategy object. Segment
payloads, which segments are acknowledged, and the send window live in
``SenderTransferState``, so a mode switch can rebuild a new strategy around the
same state with no unacknowledged segment lost (design.md §6.3, HY-05). A
strategy that stored payloads internally would make that guarantee an argument
rather than a property of the structure.

PHASE 2 SCOPE
-------------
``StopAndWaitSender`` / ``StopAndWaitReceiver`` exist so Phase 2 can transfer a
file end to end before either real ARQ mode is written (tasks.md T2.1, T2.2).
Stop-and-wait is GBN with a window of one: correct, trivially verifiable, and
not on the critical path of any experiment. It is *not* one of the three systems
under evaluation (specs.md §18) — the baselines are GBN (T3.2) and SR (T4.5),
with the hybrid built on them.

Its ACK convention — an ACK names exactly the one segment it acknowledges —
deliberately pre-empts neither D5 (GBN cumulative ACK semantics, frozen in T3.1)
nor D6 (SR per-segment semantics, frozen in T4.4). With a window of one the two
coincide, so nothing here constrains either decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Shared state and result types
# ---------------------------------------------------------------------------


@dataclass
class SenderTransferState:
    """The mode-independent core of a transfer, and precisely what survives a
    mode switch (design.md §4, §6.3)."""

    segments: list[bytes]
    window_size: int
    base: int = 0                       # oldest unacknowledged segment
    next_seq: int = 0                   # next segment never yet sent
    acked: set[int] = field(default_factory=set)

    @property
    def total_segments(self) -> int:
        return len(self.segments)

    def payload(self, seq: int) -> bytes:
        return self.segments[seq]

    def is_quiescent(self) -> bool:
        """True when nothing is in flight — the only safe point for a mode
        switch, because no packet's fate then depends on which ACK semantics
        are live (design.md §6.3)."""
        return self.base == self.next_seq


@dataclass
class ReceiverTransferState:
    """Receiver-side state that outlives a strategy instance."""

    expected_seq: int = 0               # next segment to deliver in order
    total_segments: int | None = None   # learned from FIN


@dataclass
class AckResult:
    """What an incoming ACK did to the sender's state."""

    newly_acked: list[int] = field(default_factory=list)
    duplicate: bool = False             # carried no new information
    stale: bool = False                 # referred to something already past


@dataclass
class ReceiverResult:
    """What an incoming DATA packet means for the receiver.

    ``deliver`` is always an in-order, contiguous run starting at the old
    ``expected_seq``; SR returns several at once when a gap fills, GBN and
    stop-and-wait return at most one. ``ack`` is None when nothing should be
    sent in reply.
    """

    deliver: list[tuple[int, bytes]] = field(default_factory=list)
    ack: int | None = None
    duplicate: bool = False
    buffered: bool = False              # held for later, not yet deliverable


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------


@runtime_checkable
class SenderStrategy(Protocol):
    """One mode's sending rules. Implemented by StopAndWait, GBN (T3.2), SR (T4.5)."""

    name: str

    def packets_to_send(self, now: float) -> list[int]:
        """Sequence numbers the window permits sending *now*, first time."""

    def on_ack(self, ack_number: int) -> AckResult: ...

    def on_timeout(self, now: float) -> list[int]:
        """Sequence numbers to retransmit, per this mode's rule. Empty if no
        timer has expired."""

    def all_acked(self) -> bool: ...


@runtime_checkable
class ReceiverStrategy(Protocol):
    """One mode's receiving rules."""

    name: str

    def on_data(self, seq: int, payload: bytes) -> ReceiverResult: ...


# ---------------------------------------------------------------------------
# Phase 2: stop-and-wait
# ---------------------------------------------------------------------------


class StopAndWaitSender:
    """One segment in flight at a time; retransmit it when the RTO expires.

    Ignores ``window_size`` by construction — a window is meaningful only once
    GBN lands in T3.2. The CLI still accepts and records ``--window`` so runs
    stay comparable, and ``sender.py`` says so when the value is inactive.
    """

    name = "SAW"

    def __init__(self, state: SenderTransferState, rto: float):
        self.state = state
        self.rto = rto
        self._sent_at: float | None = None      # when the current base went out

    def packets_to_send(self, now: float) -> list[int]:
        if self.all_acked() or self._sent_at is not None:
            return []
        self._sent_at = now
        self.state.next_seq = self.state.base + 1
        return [self.state.base]

    def on_ack(self, ack_number: int) -> AckResult:
        if self.all_acked():
            return AckResult(stale=True)
        if ack_number != self.state.base:
            # Either a duplicate of something already acknowledged or an ACK
            # for a segment never sent; neither may move the transfer forward.
            return AckResult(duplicate=True, stale=ack_number < self.state.base)

        acked = self.state.base
        self.state.acked.add(acked)
        self.state.base += 1
        self.state.next_seq = self.state.base
        self._sent_at = None
        return AckResult(newly_acked=[acked])

    def on_timeout(self, now: float) -> list[int]:
        if self._sent_at is None or self.all_acked():
            return []
        if now - self._sent_at < self.rto:
            return []
        self._sent_at = now
        return [self.state.base]

    def all_acked(self) -> bool:
        return self.state.base >= self.state.total_segments


class StopAndWaitReceiver:
    """Accept exactly the next expected segment; re-ACK anything already seen.

    Re-ACKing a duplicate matters even at 0% loss: if an ACK is lost the sender
    retransmits, and a silent receiver would deadlock the transfer.
    """

    name = "SAW"

    def __init__(self, state: ReceiverTransferState):
        self.state = state

    def on_data(self, seq: int, payload: bytes) -> ReceiverResult:
        if seq == self.state.expected_seq:
            self.state.expected_seq += 1
            return ReceiverResult(deliver=[(seq, payload)], ack=seq)
        if seq < self.state.expected_seq:
            return ReceiverResult(ack=seq, duplicate=True)
        # A future segment. Stop-and-wait has no receive buffer, so it is
        # dropped and left to the sender's timer; SR buffers it instead (T4.6).
        return ReceiverResult()
