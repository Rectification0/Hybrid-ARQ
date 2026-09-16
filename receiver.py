"""Receiver endpoint: bind, validate, reconstruct, verify (T2.2).

State machine (design.md §7.2)::

    LISTENING -> INITIALIZING -> RECEIVING -> FINALIZING -> COMPLETE
                       |            |            |
                       +------------+------------+--------> ERROR

Two idempotency rules make the lifecycle survive a lost control packet, which
matters long before any loss is simulated — a duplicate is cheaper to handle
than a deadlock:

* a repeated START for a run already in progress is re-ACKed, because the first
  START_ACK may have been lost;
* a repeated FIN is answered with the same FIN_ACK, for the same reason.

An idle timeout bounds every wait: nothing arriving for a configured interval
aborts and reports rather than blocking forever (specs.md §13).
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
from network.simulator import Impairment
from network.udp import open_udp_socket
from protocol.strategy import ReceiverTransferState, make_receiver_strategy


class ReceiverError(Exception):
    """The receiver could not complete. Always reported, never silent."""


class DeliveryOrderError(ReceiverError):
    """A strategy asked to deliver a segment out of order.

    This is a bug in the strategy, not a network condition: delivery is
    contiguous by contract. It aborts the transfer rather than writing a file
    with a hole in it, because silent corruption is the one outcome that is
    never acceptable (CC-01).
    """


class OutputWriter:
    """Writes delivered segments to the output file, once each.

    The duplicate guard lives here rather than in a strategy so it holds
    **independently of ARQ mode** (T2.3, SEQ-05, CC-03). GBN and SR suppress
    duplicates by different rules and a hybrid transfer switches between them
    mid-flight; a single highest-delivered check underneath both is what
    guarantees a replayed DATA packet can never be written twice, no matter
    which mode was active when it arrived.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = open(self.path, "wb")
        self._highest_delivered = -1
        self._digest = hashlib.sha256()
        self.bytes_written = 0
        self.segments_written = 0

    def write(self, seq: int, payload: bytes) -> bool:
        """Write one segment. Returns False if it was a duplicate (nothing written)."""
        if seq <= self._highest_delivered:
            return False
        if seq != self._highest_delivered + 1:
            raise DeliveryOrderError(
                f"segment {seq} delivered after {self._highest_delivered}; "
                f"delivery must be contiguous"
            )
        self._handle.write(payload)
        self._digest.update(payload)
        self._highest_delivered = seq
        self.bytes_written += len(payload)
        self.segments_written += 1
        return True

    @property
    def highest_delivered(self) -> int:
        return self._highest_delivered

    def finalize(self) -> str:
        """Flush and return the SHA-256 of what was actually written."""
        self._handle.flush()
        return self._digest.hexdigest()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()


class Receiver:
    """One transfer's worth of receiving, from START to FIN_ACK."""

    def __init__(self, *, output: Path, host: str = config.HOST,
                 port: int = config.PORT, run_id: str | None = None,
                 log_dir=None, idle_timeout: float | None = None,
                 linger: float | None = None,
                 impairment: Impairment | None = None):
        self.output = Path(output)
        self.host = host
        self.port = port
        self.run_id = run_id or "pending"
        self.log_dir = log_dir
        self.idle_timeout = (config.RECEIVER_IDLE_TIMEOUT_S
                             if idle_timeout is None else idle_timeout)
        self.linger = config.RECEIVER_LINGER_S if linger is None else linger

        self.impairment = impairment or Impairment()
        self.sock = self.impairment.wrap(open_udp_socket(), self._on_impairment)
        self.state = "LISTENING"
        self.log: EventLog | None = None
        self.writer: OutputWriter | None = None
        self.start_info: pk.StartPayload | None = None
        self.mode = "saw"
        self.window_size = 1
        self.duplicates = 0
        self.integrity_success = False

    # -- setup -------------------------------------------------------------

    def bind(self) -> int:
        """Bind and return the bound port. Separate from run() so a caller can
        learn an ephemeral port before the sender starts."""
        self.sock.bind((self.host, self.port))
        self.port = self.sock.getsockname()[1]
        return self.port

    def _send(self, pkt: Packet, address) -> None:
        self.sock.sendto(pk.encode(pkt), address)

    def _receive(self, timeout: float):
        """Wait for one valid packet. Invalid ones are logged and skipped."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, None
            self.sock.settimeout(remaining)
            try:
                raw, address = self.sock.recvfrom(pk.MAX_DATAGRAM_SIZE)
            except socket.timeout:
                return None, None
            except ConnectionResetError:
                # An ACK bounced off a sender that has already exited. Harmless
                # on UDP (see network/udp.py); keep waiting out the timeout.
                continue
            except OSError as exc:
                raise ReceiverError(f"socket error while receiving: {exc}") from exc

            try:
                return pk.decode(raw), address
            except pk.ChecksumError as exc:
                # Corrupted in flight: discard and rely on retransmission
                # (specs.md §13). Logged distinctly from a simulated DROP so the
                # two never blur together in the analysis (IN-04).
                self._log("CHECKSUM_FAIL", reason=str(exc))
            except pk.PacketError as exc:
                self._log("MALFORMED", reason=f"{type(exc).__name__}: {exc}")

    def _log(self, event: str, **fields) -> None:
        if self.log is not None:
            self.log.emit(event, mode=self.mode, **fields)

    def _on_impairment(self, kind: str, direction: str, raw, **detail) -> None:
        """Log a simulated drop, distinctly from real corruption (IN-04, T4.3)."""
        sequence = None
        reason = detail.get("reason")
        if raw is not None:
            try:
                decoded = pk.decode(raw)
                sequence = decoded.seq
                reason = f"{direction};{decoded.type.name}"
            except pk.PacketError:
                reason = f"{direction};undecodable"
        self._log(kind, sequence=sequence, reason=reason)

    # -- phases ------------------------------------------------------------

    def _await_start(self):
        """LISTENING: wait for a valid START and open the log it names."""
        while True:
            pkt, address = self._receive(self.idle_timeout)
            if pkt is None:
                raise ReceiverError(
                    f"no START within {self.idle_timeout:.0f}s — giving up")
            if pkt.type is not PacketType.START:
                continue

            info = pk.StartPayload.from_payload(pkt.payload)
            self.run_id = info.run_id or self.run_id
            self.log = EventLog(self.run_id, "receiver", log_dir=self.log_dir)
            self.mode = info.mode
            self.window_size = max(1, pkt.window)
            self._log("START", window_size=pkt.window,
                      reason=f"{info.filename} {info.filesize}B")
            return info, address

    def _initialize(self, info: pk.StartPayload, address) -> None:
        """INITIALIZING: validate, allocate output, reply START_ACK."""
        self.state = "INITIALIZING"
        if info.segment_size != config.SEGMENT_SIZE:
            self._send(Packet(
                type=PacketType.START_ACK,
                payload=pk.StartAckPayload(
                    accepted=False, segment_size=config.SEGMENT_SIZE,
                    reason=(f"segment size mismatch: sender {info.segment_size}, "
                            f"receiver {config.SEGMENT_SIZE}"),
                ).to_payload(),
            ), address)
            raise ReceiverError(
                f"refused transfer: sender segment size {info.segment_size} != "
                f"{config.SEGMENT_SIZE}")

        self.start_info = info
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.writer = OutputWriter(self.output)
        self._ack_start(address)

    def _ack_start(self, address) -> None:
        """Reply START_ACK. Idempotent: a repeated START gets the same answer."""
        self._send(Packet(
            type=PacketType.START_ACK,
            payload=pk.StartAckPayload(accepted=True,
                                       segment_size=config.SEGMENT_SIZE).to_payload(),
        ), address)
        self._log("START_ACK")

    def _receive_loop(self, address):
        """RECEIVING: deliver in order, ACK, until FIN arrives."""
        self.state = "RECEIVING"
        transfer = ReceiverTransferState()
        # The sender's START names the mode, so both sides run the same ACK
        # semantics without the receiver ever deciding anything (design.md §1).
        strategy = make_receiver_strategy(self.mode, transfer, self.window_size)

        while True:
            pkt, source = self._receive(self.idle_timeout)
            if pkt is None:
                raise ReceiverError(
                    f"idle for {self.idle_timeout:.0f}s with "
                    f"{self.writer.segments_written} segments written — giving up")
            address = source or address

            if pkt.type is PacketType.DATA:
                result = strategy.on_data(pkt.seq, pkt.payload)
                for seq, payload in result.deliver:
                    if self.writer.write(seq, payload):
                        self._log("DELIVER", sequence=seq)
                    else:
                        # The strategy thought this was new but the writer had
                        # already committed it. The writer is the authority.
                        self.duplicates += 1
                        self._log("DUPLICATE", sequence=seq, reason="ALREADY_WRITTEN")
                if result.duplicate:
                    self.duplicates += 1
                    self._log("DUPLICATE", sequence=pkt.seq, reason="RETRANSMISSION")
                if result.ack is not None:
                    self._send(Packet(type=PacketType.ACK, ack=result.ack,
                                      seq=pkt.seq), address)
                    self._log("ACK", sequence=pkt.seq, ack=result.ack)

            elif pkt.type is PacketType.START:
                # The first START_ACK was lost; answer again rather than let the
                # sender exhaust its retry budget against a silent receiver.
                self._log("START", reason="DUPLICATE")
                self._ack_start(address)

            elif pkt.type is PacketType.FIN:
                return pkt, address

    def _finalize(self, fin: Packet, address) -> pk.FinAckPayload:
        """FINALIZING: hash the output, compare, reply FIN_ACK."""
        self.state = "FINALIZING"
        info = pk.FinPayload.from_payload(fin.payload)
        self._log("FIN", sequence=fin.seq,
                  reason=f"total_segments={info.total_segments}")

        written_hash = self.writer.finalize()
        expected = self.start_info.sha256
        self.integrity_success = (written_hash == expected
                                  and self.writer.segments_written == info.total_segments)

        verdict = pk.FinAckPayload(
            sha256=written_hash,
            match=self.integrity_success,
            bytes_written=self.writer.bytes_written,
        )
        fin_ack = Packet(type=PacketType.FIN_ACK, seq=fin.seq,
                         payload=verdict.to_payload())
        self._send(fin_ack, address)
        self._log("FIN_ACK", reason="MATCH" if self.integrity_success else "HASH_MISMATCH")

        # Linger briefly: if this FIN_ACK is lost the sender repeats its FIN,
        # and answering is much cheaper than letting it fail a completed transfer.
        deadline = time.monotonic() + self.linger
        while time.monotonic() < deadline:
            pkt, source = self._receive(max(0.0, deadline - time.monotonic()))
            if pkt is not None and pkt.type is PacketType.FIN:
                self._send(fin_ack, source or address)
                self._log("FIN_ACK", reason="REPEAT")
        return verdict

    # -- entry point -------------------------------------------------------

    def run(self) -> dict:
        started = time.monotonic()
        error = None
        verdict = None
        try:
            info, address = self._await_start()
            self._initialize(info, address)
            fin, address = self._receive_loop(address)
            verdict = self._finalize(fin, address)
            if not self.integrity_success:
                raise ReceiverError(
                    f"integrity failure: wrote {self.writer.bytes_written} bytes, "
                    f"hash {verdict.sha256[:16]}... != expected "
                    f"{self.start_info.sha256[:16]}...")
            self.state = "COMPLETE"
        except ReceiverError as exc:
            error = str(exc)
            self.state = "ERROR"
            self._log("ERROR", reason=error)
        finally:
            elapsed = time.monotonic() - started
            if self.writer is not None:
                self.writer.close()
            metrics = self._metrics(elapsed, error)
            if self.log is not None:
                self.log.write_summary(metrics, extra={
                    "output": str(self.output),
                    "expected_sha256": self.start_info.sha256 if self.start_info else None,
                    "final_state": self.state,
                    "impairment": self.impairment.as_dict(),
                })
                self.log.close()
            self.sock.close()

        if error:
            raise ReceiverError(error)
        return metrics

    def _metrics(self, elapsed: float, error: str | None) -> dict:
        written = self.writer.bytes_written if self.writer else 0
        return {
            "completion_time_s": elapsed,
            "bytes_written": written,
            "segments_written": self.writer.segments_written if self.writer else 0,
            "goodput_bytes_per_s": written / elapsed if elapsed > 0 else 0.0,
            "duplicates_suppressed": self.duplicates,
            "integrity_success": self.integrity_success,
            "error": error,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Receive a file over the hybrid ARQ protocol.")
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--output", required=True, type=Path, help="destination file")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--run-id", default=None,
                        help="normally taken from the sender's START")
    parser.add_argument("--log-dir", default=None, type=Path)
    parser.add_argument("--idle-timeout", type=float, default=None)
    impair = parser.add_argument_group("impairment")
    impair.add_argument("--rtt", type=float, default=config.RTT_MS,
                        help="emulated round-trip time in ms; half is applied here")
    impair.add_argument("--jitter", type=float, default=config.JITTER_MS)
    impair.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Half the RTT on each direction, so a round trip totals the target
    # (design.md 8). The receiver carries the ACK half.
    receiver = Receiver(output=args.output, host=args.host, port=args.port,
                        run_id=args.run_id or f"recv_{uuid.uuid4().hex[:6]}",
                        log_dir=args.log_dir, idle_timeout=args.idle_timeout,
                        impairment=Impairment(delay_ms=args.rtt / 2.0,
                                              jitter_ms=args.jitter, seed=args.seed))
    port = receiver.bind()
    print(f"listening on {args.host}:{port}, writing {args.output}")

    try:
        metrics = receiver.run()
    except ReceiverError as exc:
        print(f"receive failed: {exc}", file=sys.stderr)
        if receiver.log is not None:
            print(f"logs: {receiver.log.directory}", file=sys.stderr)
        return 1

    print(f"wrote {metrics['bytes_written']} bytes in "
          f"{metrics['segments_written']} segments")
    print(f"integrity: {'OK — hash matches source' if metrics['integrity_success'] else 'FAILED'}")
    print(f"logs: {receiver.log.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
