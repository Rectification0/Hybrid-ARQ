"""Sender endpoint: CLI, file reading, transfer lifecycle, orchestration (T2.1).

State machine (design.md §7.1)::

    IDLE -> STARTING -> SENDING -> FINISHING -> COMPLETE
                 |         |          |
                 +---------+----------+-------> ERROR

Every wait is bounded. specs.md §13 requires that a receiver which never appears
is *reported*, not waited on forever, and that a transfer aborts after a
configurable retry policy — so STARTING, SENDING and FINISHING each carry a
retry budget from config.py and fail with a message naming what timed out.

Layering (design.md §1.1): this module owns lifecycle, file I/O, CLI and
logging. It does not own ARQ bookkeeping — which segments may be sent, what an
ACK advances, and what a timeout retransmits all come from the strategy
(protocol/strategy.py). GBN was added in T3.2 without a line changing here, which is the
property SR (T4.5) and the hybrid (T5.4) depend on too.
"""

from __future__ import annotations

import argparse
import hashlib
import socket
import sys
import time
import uuid
from pathlib import Path

import config
from eventlog import EventLog
from protocol import packet as pk
from protocol.packet import Packet, PacketType
from network.udp import open_udp_socket
from protocol.strategy import SenderTransferState, make_sender_strategy

#: Modes the CLI accepts, mapped to the task that delivers each. A value of None
#: means implemented; anything else is named so the error says which task brings
#: it rather than "invalid choice".
MODE_TASKS = {
    "saw": None,
    "gbn": None,
    "sr": "T4.5",
    "hybrid": "T5.4",
    "fixed-hybrid": "T5.6",
}


class TransferError(Exception):
    """The transfer could not complete. Always reported, never silent."""


def segment_file(path: Path, segment_size: int) -> tuple[list[bytes], str, int]:
    """Read a file into fixed-size segments and hash it.

    Returns (segments, sha256, filesize). The hash is of the *source* file and
    travels in START, so the receiver can verify its reconstruction against it
    (IN-05). Sequence numbers are segment indices over this list (D4).
    """
    digest = hashlib.sha256()
    segments: list[bytes] = []
    total = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(segment_size)
            if not chunk:
                break
            digest.update(chunk)
            segments.append(chunk)
            total += len(chunk)
    return segments, digest.hexdigest(), total


class Sender:
    """One file transfer, from START to verified FIN_ACK."""

    def __init__(self, *, path: Path, host: str, port: int, mode: str,
                 window: int, run_id: str, log_dir=None,
                 rto: float | None = None, sock: socket.socket | None = None):
        self.path = Path(path)
        self.host = host
        self.port = port
        self.mode = mode
        self.window = window
        self.run_id = run_id
        self.rto = config.RTO_S if rto is None else rto

        self.log = EventLog(run_id, "sender", log_dir=log_dir)
        self._owns_socket = sock is None
        self.sock = sock or open_udp_socket()

        self.state = "IDLE"
        self.segments: list[bytes] = []
        self.sha256 = ""
        self.filesize = 0

        # Metrics that specs.md §20 needs and that must also be derivable from
        # events.csv alone (CC-06) — these are a cross-check, not the record.
        self.data_sent = 0
        self.data_retransmitted = 0
        self.bytes_sent = 0
        self.bytes_retransmitted = 0
        self.rtt_samples: list[float] = []

    # -- helpers -----------------------------------------------------------

    def _to(self) -> tuple[str, int]:
        return (self.host, self.port)

    def _send(self, pkt: Packet) -> int:
        raw = pk.encode(pkt)
        self.sock.sendto(raw, self._to())
        return len(raw)

    def _receive(self, timeout: float) -> Packet | None:
        """Wait up to ``timeout`` for one valid packet.

        Invalid packets are discarded and logged with the reason, then the wait
        continues on the remaining budget — a corrupted datagram must not be
        mistaken for silence (specs.md §13, IN-04).
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                raw, _ = self.sock.recvfrom(pk.MAX_DATAGRAM_SIZE)
            except socket.timeout:
                return None
            except ConnectionResetError:
                # Windows ICMP port-unreachable for a datagram we just sent (see
                # network/udp.py). Not fatal, and not a connection. Keep waiting
                # out the timeout rather than returning: giving up immediately
                # would collapse the whole retry budget into microseconds and
                # abandon a receiver that is merely slow to start.
                continue
            except OSError as exc:
                raise TransferError(f"socket error while receiving: {exc}") from exc

            try:
                return pk.decode(raw)
            except pk.ChecksumError as exc:
                self.log.emit("CHECKSUM_FAIL", mode=self.mode, reason=str(exc))
            except pk.PacketError as exc:
                self.log.emit("MALFORMED", mode=self.mode,
                              reason=f"{type(exc).__name__}: {exc}")

    def _transition(self, new_state: str) -> None:
        self.state = new_state

    # -- phases ------------------------------------------------------------

    def _handshake(self) -> None:
        """STARTING: send START, await START_ACK, within the retry budget."""
        self._transition("STARTING")
        payload = pk.StartPayload(
            filename=self.path.name,
            filesize=self.filesize,
            sha256=self.sha256,
            segment_size=config.SEGMENT_SIZE,
            mode=self.mode,
            run_id=self.run_id,
        ).to_payload()
        start = Packet(type=PacketType.START, window=self.window, payload=payload)

        for attempt in range(1, config.CONTROL_RETRY_LIMIT + 1):
            self._send(start)
            self.log.emit("START", mode=self.mode, window_size=self.window,
                          reason=f"attempt {attempt}")
            reply = self._receive(config.CONTROL_RETRY_TIMEOUT_S)
            if reply is None:
                self.log.emit("TIMEOUT", mode=self.mode, reason="START_ACK")
                continue
            if reply.type is not PacketType.START_ACK:
                continue

            ack_payload = pk.StartAckPayload.from_payload(reply.payload)
            if not ack_payload.accepted:
                raise TransferError(f"receiver refused the transfer: {ack_payload.reason}")
            if ack_payload.segment_size != config.SEGMENT_SIZE:
                raise TransferError(
                    f"segment size mismatch: sender {config.SEGMENT_SIZE}, "
                    f"receiver {ack_payload.segment_size}"
                )
            self.log.emit("START_ACK", mode=self.mode, window_size=self.window)
            return

        raise TransferError(
            f"no START_ACK from {self.host}:{self.port} after "
            f"{config.CONTROL_RETRY_LIMIT} attempts — is a receiver running?"
        )

    def _send_data(self) -> None:
        """SENDING: drive the strategy until every segment is acknowledged.

        Mode-agnostic by design: this loop asks what to send, reports what
        arrived, and asks what a timeout means. Which segments those are — one,
        a window, or a whole outstanding range — is entirely the strategy's
        business (design.md §1.1).
        """
        self._transition("SENDING")
        state = SenderTransferState(segments=self.segments, window_size=self.window)
        strategy = make_sender_strategy(self.mode, state, self.rto)

        self._sent_once = set()
        self._sent_at = {}
        self._retransmitted = set()
        self._attempts = {}

        while not strategy.all_acked():
            for seq in strategy.packets_to_send(time.monotonic()):
                self._emit_data(seq, is_retx=False)
            self._drain_timers(strategy)

            # Wait only as long as the nearest timer allows. Blocking for a full
            # RTO regardless would let a timer armed mid-wait go unnoticed until
            # the following wait ended, making a retransmission up to two RTOs
            # late — visible as inflated completion times under loss.
            pending = strategy.next_timeout(time.monotonic())
            wait = self.rto if pending is None else max(0.0, min(pending, self.rto))
            reply = self._receive(wait)
            now = time.monotonic()

            if reply is not None and reply.type is PacketType.ACK:
                result = strategy.on_ack(reply.ack, now)
                self.log.emit("ACK", sequence=reply.ack, ack=reply.ack,
                              mode=self.mode, window_size=self.window,
                              reason="DUPLICATE" if result.duplicate else None)
                for seq in result.newly_acked:
                    # Karn's rule: a retransmitted segment's ACK is ambiguous,
                    # so it never becomes an RTT sample (design.md §5.4).
                    if seq not in self._retransmitted and seq in self._sent_at:
                        self.rtt_samples.append((now - self._sent_at[seq]) * 1000.0)
                self._drain_timers(strategy)
                continue

            expired = strategy.on_timeout(now)
            if not expired:
                continue

            # One timer expiry, then the range it forces. Under GBN that range
            # is every outstanding segment, which is the cost the hybrid exists
            # to avoid — so it is logged segment by segment (GBN-05, TO-03).
            self.log.emit("TIMEOUT", sequence=expired[0], mode=self.mode,
                          window_size=self.window,
                          reason=f"RTO;outstanding={len(expired)}")
            for seq in expired:
                if self._attempts.get(seq, 0) >= config.DATA_RETRY_LIMIT:
                    raise TransferError(
                        f"segment {seq} unacknowledged after "
                        f"{config.DATA_RETRY_LIMIT} retransmissions — aborting"
                    )
                self._emit_data(seq, is_retx=True)
            self._drain_timers(strategy)

    def _drain_timers(self, strategy) -> None:
        """Log timer transitions the strategy recorded (TO-02)."""
        for event in strategy.drain_timer_events():
            self.log.emit(event.kind, sequence=event.seq, mode=self.mode,
                          window_size=self.window)

    def _emit_data(self, seq: int, *, is_retx: bool) -> None:
        payload = self.segments[seq]
        size = self._send(Packet(type=PacketType.DATA, seq=seq,
                                 window=self.window, payload=payload))
        self._sent_at[seq] = time.monotonic()
        self._attempts[seq] = self._attempts.get(seq, 0) + 1

        if is_retx or seq in self._sent_once:
            self._retransmitted.add(seq)
            self.data_retransmitted += 1
            self.bytes_retransmitted += size
            self.log.emit("RETX", sequence=seq, mode=self.mode,
                          window_size=self.window, reason="TIMEOUT")
        else:
            self._sent_once.add(seq)
            self.data_sent += 1
            self.log.emit("SEND", sequence=seq, mode=self.mode,
                          window_size=self.window)
        self.bytes_sent += size

    def _finish(self) -> pk.FinAckPayload:
        """FINISHING: send FIN, await the receiver's integrity verdict."""
        self._transition("FINISHING")
        fin = Packet(
            type=PacketType.FIN,
            seq=len(self.segments),
            payload=pk.FinPayload(total_segments=len(self.segments)).to_payload(),
        )

        for attempt in range(1, config.CONTROL_RETRY_LIMIT + 1):
            self._send(fin)
            self.log.emit("FIN", sequence=len(self.segments), mode=self.mode,
                          reason=f"attempt {attempt}")
            reply = self._receive(config.CONTROL_RETRY_TIMEOUT_S)
            if reply is None:
                self.log.emit("TIMEOUT", mode=self.mode, reason="FIN_ACK")
                continue
            if reply.type is not PacketType.FIN_ACK:
                continue
            verdict = pk.FinAckPayload.from_payload(reply.payload)
            self.log.emit("FIN_ACK", mode=self.mode,
                          reason="MATCH" if verdict.match else "HASH_MISMATCH")
            return verdict

        raise TransferError(
            f"no FIN_ACK after {config.CONTROL_RETRY_LIMIT} attempts — "
            f"transfer completed but was never confirmed"
        )

    # -- entry point -------------------------------------------------------

    def run(self) -> dict:
        """Run the transfer. Returns the metrics dict written to summary.json."""
        started = time.monotonic()
        verdict = None
        error = None
        try:
            if not self.path.is_file():
                raise TransferError(f"no such file: {self.path}")
            self.segments, self.sha256, self.filesize = segment_file(
                self.path, config.SEGMENT_SIZE)

            self._handshake()
            self._send_data()
            verdict = self._finish()

            if not verdict.match:
                raise TransferError(
                    f"integrity failure: receiver wrote {verdict.bytes_written} bytes "
                    f"with hash {verdict.sha256[:16]}..., source hash "
                    f"{self.sha256[:16]}..."
                )
            self._transition("COMPLETE")
        except TransferError as exc:
            error = str(exc)
            self._transition("ERROR")
            self.log.emit("ERROR", mode=self.mode, reason=error)
        finally:
            elapsed = time.monotonic() - started
            metrics = self._metrics(elapsed, verdict, error)
            self.log.write_summary(metrics, extra={
                "source": {
                    "filename": self.path.name,
                    "filesize": self.filesize,
                    "sha256": self.sha256,
                    "total_segments": len(self.segments),
                },
                "final_state": self.state,
            })
            self.log.close()
            if self._owns_socket:
                self.sock.close()

        if error:
            raise TransferError(error)
        return metrics

    def _metrics(self, elapsed: float, verdict, error: str | None) -> dict:
        """specs.md §20, as far as Phase 2 can populate it.

        Switch count and mode residence stay at their Phase 2 values until the
        hybrid controller exists (T5.1); T6.2 audits that every §20 metric is
        derivable from events.csv alone.
        """
        delivered = self.filesize if (verdict and verdict.match) else 0
        transmitted = self.bytes_sent or 1
        return {
            "completion_time_s": elapsed,
            "goodput_bytes_per_s": delivered / elapsed if elapsed > 0 else 0.0,
            "delivered_bytes": delivered,
            "retransmission_count": self.data_retransmitted,
            "retransmission_overhead": self.bytes_retransmitted / transmitted,
            "unique_data_packets": self.data_sent,
            "total_data_transmissions": self.data_sent + self.data_retransmitted,
            "bytes_transmitted": self.bytes_sent,
            "integrity_success": bool(verdict and verdict.match),
            "switch_count": 0,
            "gbn_residence_s": 0.0,
            "sr_residence_s": 0.0,
            "rtt_mean_ms": (sum(self.rtt_samples) / len(self.rtt_samples)
                            if self.rtt_samples else None),
            "rtt_samples": len(self.rtt_samples),
            "error": error,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a file over the hybrid ARQ protocol.")
    parser.add_argument("--file", required=True, type=Path, help="source file")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--mode", default="gbn", choices=sorted(MODE_TASKS),
                        help="ARQ mode; 'saw' and 'gbn' are implemented")
    parser.add_argument("--window", type=int, default=config.WINDOW_SIZE,
                        help="outstanding segments; ignored by --mode saw")
    parser.add_argument("--run-id", default=None, help="defaults to a generated id")
    parser.add_argument("--log-dir", default=None, type=Path)
    parser.add_argument("--rto", type=float, default=None, help="override RTO seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    pending_task = MODE_TASKS[args.mode]
    if pending_task:
        print(f"error: --mode {args.mode} is not implemented yet; it lands in "
              f"{pending_task}. Implemented today: "
              f"{', '.join(m for m, t in sorted(MODE_TASKS.items()) if t is None)}.",
              file=sys.stderr)
        return 2

    run_id = (args.run_id or
              f"{args.mode}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}")
    sender = Sender(path=args.file, host=args.host, port=args.port, mode=args.mode,
                    window=args.window, run_id=run_id, log_dir=args.log_dir,
                    rto=args.rto)
    try:
        metrics = sender.run()
    except TransferError as exc:
        print(f"transfer failed: {exc}", file=sys.stderr)
        print(f"logs: {sender.log.directory}", file=sys.stderr)
        return 1

    print(f"sent {sender.filesize} bytes in {len(sender.segments)} segments "
          f"in {metrics['completion_time_s']:.3f}s")
    print(f"goodput {metrics['goodput_bytes_per_s'] / 1024:.1f} KiB/s, "
          f"retransmissions {metrics['retransmission_count']}")
    print(f"integrity: {'OK' if metrics['integrity_success'] else 'FAILED'}")
    print(f"logs: {sender.log.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
