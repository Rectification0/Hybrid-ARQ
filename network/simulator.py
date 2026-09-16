"""Controlled network impairment: loss, one-way delay, jitter (T4.1-T4.3).

``ImpairedSocket`` wraps a UDP socket and applies impairment on the way past.
It is a **wrapper, never a branch inside protocol code** (design.md §8): with no
impairment configured the protocol path is byte-identical to an unimpaired run,
which is what makes the 0%-loss control condition trustworthy.

Application-level impairment rather than ``tc``/netem because it is portable to
Windows and reproducible from a recorded seed (specs.md §1, RP-03).

DIRECTION MODEL
===============
Impairment is applied **per direction**, named from the perspective of the
socket being wrapped:

* ``loss_rate``       — an outgoing datagram is dropped instead of sent
* ``recv_loss_rate``  — an incoming datagram is discarded instead of returned
* ``delay_ms``        — one-way delay on outgoing datagrams
* ``jitter_ms``       — bounded uniform offset on that delay

Wrapping the **sender** with ``loss_rate`` gives DATA loss, and ``recv_loss_rate``
gives ACK loss (specs.md §25.3) — a discarded incoming ACK is indistinguishable
from one the receiver never managed to send. Both can therefore be driven from
one process with one seed, which is what keeps an experiment reproducible.

For a round trip of ``RTT``, set ``delay_ms = RTT/2`` on **both** endpoints, so
each direction carries half (design.md §8). Wrapping only the sender gives a
half-RTT path; the experiment runner (T8.2) wraps both.

Loss applies to every packet type, control included. That is the honest model —
a network does not know a START from a DATA — and it is what exercises the
control retry budgets from T2.4 under load.

REPRODUCIBILITY
===============
Each direction draws from its **own** ``random.Random``, derived from the base
seed and never the global RNG (RP-03). Separate streams matter: if send and
receive shared one generator, the drop sequence would depend on the interleaving
of ACK arrivals, and a rerun of the same seed would diverge. Per-direction
streams make the forward drop sequence a pure function of the seed.

``random.Random`` is seeded with a string, which CPython hashes with SHA-512 —
stable across processes and runs, unlike ``hash()``.
"""

from __future__ import annotations

import heapq
import itertools
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

#: Called as ``on_event(kind, direction, raw, **detail)``. ``kind`` is "DROP" or
#: "LOSS_CHANGE"; the endpoint decodes ``raw`` and logs. Keeping the decode on
#: the endpoint side keeps packet-format knowledge out of the network layer.
EventHook = Callable[..., None]


@dataclass(frozen=True)
class Impairment:
    """A condition's impairment settings, as recorded in summary.json."""

    loss_rate: float = 0.0
    recv_loss_rate: float = 0.0
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    seed: int = 0
    loss_schedule: tuple[tuple[float, float], ...] = ()

    def is_noop(self) -> bool:
        """True when wrapping would change nothing observable."""
        return (self.loss_rate == 0.0 and self.recv_loss_rate == 0.0
                and self.delay_ms == 0.0 and self.jitter_ms == 0.0
                and not self.loss_schedule)

    def wrap(self, sock, on_event: EventHook | None = None):
        """Return an impaired view of ``sock``, or ``sock`` itself if inert.

        Returning the raw socket for a no-op condition is deliberate: the clean
        baseline must not run through any extra code path that the impaired runs
        do not, or the comparison measures the wrapper as well as the protocol.
        """
        if self.is_noop():
            return sock
        return ImpairedSocket(
            sock, loss_rate=self.loss_rate, recv_loss_rate=self.recv_loss_rate,
            delay_ms=self.delay_ms, jitter_ms=self.jitter_ms, seed=self.seed,
            loss_schedule=self.loss_schedule, on_event=on_event)

    def as_dict(self) -> dict:
        return {
            "loss_rate": self.loss_rate,
            "recv_loss_rate": self.recv_loss_rate,
            "delay_ms": self.delay_ms,
            "jitter_ms": self.jitter_ms,
            "seed": self.seed,
            "loss_schedule": [list(step) for step in self.loss_schedule],
        }


class ImpairedSocket:
    """A UDP socket with loss, delay and jitter applied in passing.

    Implements the subset of the socket interface the endpoints use, so it
    substitutes for one without either endpoint knowing.
    """

    def __init__(self, sock, *, loss_rate: float = 0.0, recv_loss_rate: float = 0.0,
                 delay_ms: float = 0.0, jitter_ms: float = 0.0, seed: int = 0,
                 loss_schedule: Sequence[tuple[float, float]] = (),
                 on_event: EventHook | None = None):
        for name, value in (("loss_rate", loss_rate), ("recv_loss_rate", recv_loss_rate)):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {value}")
        if delay_ms < 0 or jitter_ms < 0:
            raise ValueError("delay_ms and jitter_ms must be non-negative")
        if jitter_ms > delay_ms and delay_ms == 0 and jitter_ms > 0:
            # Jitter with no base delay would have to produce negative delays to
            # be symmetric; clamping at zero would bias every sample upward.
            raise ValueError("jitter_ms requires a non-zero delay_ms")

        self._sock = sock
        self._loss_rate = loss_rate
        self._recv_loss_rate = recv_loss_rate
        self._delay_s = delay_ms / 1000.0
        self._jitter_s = jitter_ms / 1000.0
        self._schedule = tuple(sorted(loss_schedule))
        self._on_event = on_event

        self._send_rng = random.Random(f"{seed}/send")
        self._recv_rng = random.Random(f"{seed}/recv")
        self._jitter_rng = random.Random(f"{seed}/jitter")

        self._t0 = time.perf_counter()
        self._active_loss = self._scheduled_rate_at(0.0)
        self._timeout: float | None = None

        self._queue: list[tuple[float, int, bytes, object]] = []
        self._tiebreak = itertools.count()
        self._cond = threading.Condition()
        self._pump_thread: threading.Thread | None = None
        self._closed = False

        self.dropped_sending = 0
        self.dropped_receiving = 0
        self.delayed = 0

    # -- loss schedule (T4.2) ---------------------------------------------

    def _scheduled_rate_at(self, elapsed: float) -> float:
        rate = self._loss_rate
        for moment, value in self._schedule:
            if elapsed >= moment:
                rate = value
            else:
                break
        return rate

    def current_loss_rate(self) -> float:
        """The send-direction loss rate in force now, following the schedule.

        A change is announced through ``on_event`` so it lands in the event log
        and a dynamic-loss run can be read back afterwards (T4.2, §17.3).
        """
        if not self._schedule:
            return self._loss_rate
        rate = self._scheduled_rate_at(time.perf_counter() - self._t0)
        if rate != self._active_loss:
            previous, self._active_loss = self._active_loss, rate
            self._emit("LOSS_CHANGE", "send", None,
                       reason=f"{previous:.4f}->{rate:.4f}")
        return rate

    # -- socket interface --------------------------------------------------

    def sendto(self, data: bytes, address) -> int:
        rate = self.current_loss_rate()
        if rate > 0.0 and self._send_rng.random() < rate:
            self.dropped_sending += 1
            self._emit("DROP", "send", data)
            return len(data)            # the caller sees a normal send

        delay = self._next_delay()
        if delay <= 0.0:
            return self._sock.sendto(data, address)

        self.delayed += 1
        self._schedule_send(data, address, delay)
        return len(data)

    def recvfrom(self, bufsize: int):
        deadline = None if self._timeout is None else time.perf_counter() + self._timeout
        while True:
            if deadline is not None:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise socket.timeout("timed out")
                # Re-arm on every pass: a discarded datagram must not silently
                # extend the caller's timeout into a second full wait.
                self._sock.settimeout(remaining)

            data, address = self._sock.recvfrom(bufsize)

            if self._recv_loss_rate > 0.0 and self._recv_rng.random() < self._recv_loss_rate:
                self.dropped_receiving += 1
                self._emit("DROP", "recv", data)
                continue
            return data, address

    def settimeout(self, value) -> None:
        self._timeout = value
        self._sock.settimeout(value)

    def gettimeout(self):
        return self._timeout

    def bind(self, address) -> None:
        self._sock.bind(address)

    def getsockname(self):
        return self._sock.getsockname()

    def fileno(self) -> int:
        return self._sock.fileno()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        if self._pump_thread is not None:
            # Let anything already scheduled go out; a transfer's last ACK
            # should not vanish because the socket closed a millisecond early.
            self._pump_thread.join(timeout=2.0)
        self._sock.close()

    # -- delayed delivery --------------------------------------------------

    def _next_delay(self) -> float:
        if self._delay_s <= 0.0:
            return 0.0
        if self._jitter_s <= 0.0:
            return self._delay_s
        offset = self._jitter_rng.uniform(-self._jitter_s, self._jitter_s)
        return max(0.0, self._delay_s + offset)

    def _schedule_send(self, data: bytes, address, delay: float) -> None:
        """Queue a datagram for later sending.

        A queue and a pump thread rather than ``time.sleep`` in ``sendto``:
        blocking the caller would stall the entire sender for the delay of one
        packet, which at a 500 ms RTT would serialise a window that is supposed
        to be in flight together (design.md §8).
        """
        with self._cond:
            heapq.heappush(self._queue,
                           (time.perf_counter() + delay, next(self._tiebreak), data, address))
            if self._pump_thread is None:
                self._pump_thread = threading.Thread(
                    target=self._pump, name="ImpairedSocket-delay", daemon=True)
                self._pump_thread.start()
            self._cond.notify()

    def _pump(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait(0.2)
                if not self._queue:
                    return                      # closed and drained
                release, _, data, address = self._queue[0]
                now = time.perf_counter()
                if release > now:
                    self._cond.wait(release - now)
                    continue
                heapq.heappop(self._queue)
            try:
                self._sock.sendto(data, address)
            except OSError:
                return                          # socket closed under us

    # -- events ------------------------------------------------------------

    def _emit(self, kind: str, direction: str, raw: bytes | None, **detail) -> None:
        if self._on_event is not None:
            self._on_event(kind, direction, raw, **detail)
