import random
import time

# Sender-side send path: simulated loss plus the counters for the end-of-run summary.
# Loss is decided *before* sendto(), so a dropped DATA packet never reaches the wire --
# in Wireshark it shows up as a gap in the sequence numbers, not as a frame.


def add_loss_args(parser):
    parser.add_argument("--drop", type=int, default=None,
                        help="drop the first transmission of this sequence number")
    parser.add_argument("--loss", type=float, default=0.0,
                        help="drop each DATA transmission (first or RETX) with this probability")
    parser.add_argument("--seed", type=int, default=1,
                        help="RNG seed for --loss, so a run is repeatable")


class Channel:
    def __init__(self, sock, addr, tag, drop=None, loss=0.0, seed=1):
        if not 0.0 <= loss < 1.0:
            raise SystemExit(f"--loss must be in [0, 1), got {loss}")
        self.sock, self.addr, self.tag = sock, addr, tag
        self.drop, self.loss = drop, loss
        # Own seeded RNG: the drop pattern is a function of the seed alone (cf. RP-03).
        # A probability draw, not an every-Nth counter, so loss is never phase-locked
        # to GBN's deterministic retransmission pattern.
        self.rng = random.Random(seed)
        self.sends = self.retx = self.drops = 0
        self.started = time.perf_counter()  # perf_counter, never monotonic (see CLAUDE.md)

    def _lose(self, seq):
        if self.drop == seq:
            self.drop = None  # --drop N hits the first transmission of N only
            return True
        return self.loss > 0 and self.rng.random() < self.loss

    def send(self, seq, packet, retx=False):
        if retx:
            self.retx += 1
        else:
            self.sends += 1
        if self._lose(seq):
            self.drops += 1
            print(f"[{self.tag}] DROP DATA {seq} (simulated{', RETX' if retx else ''})")
            return
        self.sock.sendto(packet, self.addr)
        print(f"[{self.tag}] {'RETX' if retx else 'SEND'} DATA {seq}")

    def summary(self, total):
        on_wire = self.sends + self.retx - self.drops
        elapsed = time.perf_counter() - self.started
        return (f"{total} segments, {self.sends} SEND, {self.retx} RETX, "
                f"{self.drops} DROP, {on_wire} DATA on the wire, {elapsed:.2f} s")
