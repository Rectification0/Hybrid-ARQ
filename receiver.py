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
from protocol import hybrid
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

        # The log cannot be opened until a START names the run, but packets can
        # arrive — and be rejected — before that, and a receiver that never gets
        # a START must still leave a log behind (T6.1, M8). Events observed
        # early are held here with the time they happened and replayed onto the
        # same timeline once the log exists.
        self._t0 = time.perf_counter()
        self._pending_events: list[tuple[float, str, dict]] = []
        self.writer: OutputWriter | None = None
        self.start_info: pk.StartPayload | None = None
        # ``requested_mode`` is what the sender's START named — possibly a
        # controller ("hybrid"); ``mode`` is the ARQ mode actually live, which is
        # what builds the strategy and what every event logs.
        self.requested_mode = "saw"
        self.mode = "saw"
        self.window_size = 1
        self.duplicates = 0
        self.integrity_success = False
        # The hash this receiver computed over what it actually wrote. It goes
        # out in the FIN_ACK payload, but T11.4 needs it written down too: a
        # dashboard that shows "source hash vs received hash" side by side has
        # nowhere to read the second one from otherwise. A logging addition of
        # the same kind as the T6.2 ``bytes`` column, not a protocol change.
        self.received_sha256: str | None = None
        self.mode_epoch = 0
        self.switch_count = 0

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
        deadline = time.perf_counter() + timeout
        while True:
            remaining = deadline - time.perf_counter()
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
        if self.log is None:
            # Before the log exists, hold the event with the mode and the moment
            # it happened rather than dropping it (T6.1).
            self._pending_events.append((time.perf_counter(), event,
                                         dict(fields, mode=self.mode)))
            return
        self.log.emit(event, mode=self.mode, **fields)

    def _open_log(self, run_id: str) -> None:
        """Open the log on the receiver's own timeline and replay what preceded it.

        ``t0`` is the moment this receiver started, not the moment the log was
        created, so a rejected packet that arrived before START keeps its real
        timestamp instead of appearing at zero.
        """
        self.run_id = run_id
        self.log = EventLog(run_id, "receiver", log_dir=self.log_dir, t0=self._t0)
        for moment, event, fields in self._pending_events:
            self.log.emit(event, timestamp=moment - self._t0, **fields)
        self._pending_events.clear()

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
                self._log("TIMEOUT", reason=f"START;idle {self.idle_timeout:.1f}s")
                raise ReceiverError(
                    f"no START within {self.idle_timeout:.0f}s — giving up")
            if pkt.type is not PacketType.START:
                continue

            info = pk.StartPayload.from_payload(pkt.payload)
            self._open_log(info.run_id or self.run_id)
            self.requested_mode = info.mode
            # A hybrid transfer starts in config.DEFAULT_MODE. Both endpoints
            # read the same config, so the starting mode needs no field on the
            # wire and the two sides cannot disagree about where it began.
            self.mode = (hybrid.initial_mode()
                         if info.mode in hybrid.HYBRID_MODES else info.mode)
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
                self._log("TIMEOUT", sequence=transfer.expected_seq,
                          reason=(f"RECEIVE;idle {self.idle_timeout:.1f}s;"
                                  f"written={self.writer.segments_written}"))
                raise ReceiverError(
                    f"idle for {self.idle_timeout:.0f}s with "
                    f"{self.writer.segments_written} segments written — giving up")
            address = source or address

            if pkt.type is PacketType.DATA:
                result = strategy.on_data(pkt.seq, pkt.payload)
                for seq, payload in result.deliver:
                    if self.writer.write(seq, payload):
                        # The payload byte count is what makes delivered bytes,
                        # and therefore goodput, derivable from the log (CC-06).
                        self._log("DELIVER", sequence=seq, size_bytes=len(payload))
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

            elif pkt.type is PacketType.MODE:
                strategy = self._handle_mode(pkt, strategy, transfer, address)

            elif pkt.type is PacketType.FIN:
                return pkt, address

    def _handle_mode(self, pkt: Packet, strategy, transfer: ReceiverTransferState,
                     address):
        """The receiving half of the MODE handshake (T5.4, D10, HY-04).

        Returns the strategy to carry on with. The receiver never *decides* a
        switch — it validates one, adopts it, and echoes. Not echoing is how it
        refuses: the sender runs out of retries and continues in the current
        mode, which leaves both sides in one consistent mode rather than half
        switched (specs.md §13, design.md §6.3).
        """
        request = pk.ModePayload.from_payload(pkt.payload)

        if request.epoch < self.mode_epoch:
            # A MODE from a superseded handshake, arriving late. Discarding it is
            # exactly what the epoch counter exists for (HY-07).
            self._log("MODE", sequence=request.effective_from_seq,
                      reason=f"STALE;epoch={request.epoch}<{self.mode_epoch}")
            return strategy

        if request.epoch == self.mode_epoch and self.mode_epoch > 0:
            # The switch already happened and the echo was lost. Answer again,
            # idempotently: re-running the switch would be wrong, staying silent
            # would strand a sender that has already drained its window.
            if request.mode != self.mode:
                self._log("MODE", sequence=request.effective_from_seq,
                          reason=f"REFUSED;epoch {request.epoch} reused for "
                                 f"{request.mode} while in {self.mode}")
                return strategy
            self._echo_mode(request, address, "REPEAT")
            return strategy

        refusal = self._mode_refusal(request, strategy, transfer)
        if refusal is not None:
            self._log("MODE", sequence=request.effective_from_seq,
                      reason=f"REFUSED;{refusal}")
            return strategy

        previous = self.mode
        self.mode = request.mode
        self.mode_epoch = request.epoch
        if request.mode != previous:
            self.switch_count += 1
        # Rebuilt around the *same* transfer state, so ``expected_seq`` and
        # everything already written carry across untouched (HY-06, HY-07).
        rebuilt = make_receiver_strategy(self.mode, transfer, self.window_size)
        self._log("SWITCH", sequence=request.effective_from_seq,
                  window_size=self.window_size,
                  reason=f"{request.reason or 'MODE_REQUEST'};from={previous};"
                         f"epoch={request.epoch}")
        self._echo_mode(request, address, "ACCEPTED")
        return rebuilt

    def _mode_refusal(self, request: pk.ModePayload, strategy,
                      transfer: ReceiverTransferState) -> str | None:
        """Why this MODE request cannot be honoured, or None if it can.

        The quiescence check is not defensive padding: the sender only sends
        MODE once every segment it has sent is acknowledged, so the receiver
        must already have delivered exactly up to ``effective_from_seq`` with an
        empty buffer. If it has not, the two sides disagree about the transfer's
        position, and switching ACK semantics on top of that disagreement is the
        one thing guaranteed to corrupt the file.
        """
        if request.mode not in hybrid.ARQ_MODES:
            return f"unknown mode {request.mode!r}"
        if transfer.expected_seq != request.effective_from_seq:
            return (f"not quiescent: expected_seq={transfer.expected_seq}, "
                    f"effective_from_seq={request.effective_from_seq}")
        buffered = getattr(strategy, "buffer", None)
        if buffered:
            return f"receive buffer holds {sorted(buffered)}"
        return None

    def _echo_mode(self, request: pk.ModePayload, address, disposition: str) -> None:
        """Echo the request back verbatim; the epoch is what the sender matches on."""
        self._send(Packet(
            type=PacketType.MODE,
            seq=request.effective_from_seq,
            payload=pk.ModePayload(
                mode=request.mode,
                effective_from_seq=request.effective_from_seq,
                epoch=request.epoch,
                reason="ECHO",
            ).to_payload(),
        ), address)
        self._log("MODE", sequence=request.effective_from_seq,
                  reason=f"{disposition};echo;epoch={request.epoch}")

    def _finalize(self, fin: Packet, address) -> pk.FinAckPayload:
        """FINALIZING: hash the output, compare, reply FIN_ACK."""
        self.state = "FINALIZING"
        info = pk.FinPayload.from_payload(fin.payload)
        self._log("FIN", sequence=fin.seq,
                  reason=f"total_segments={info.total_segments}")

        written_hash = self.writer.finalize()
        self.received_sha256 = written_hash
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
        deadline = time.perf_counter() + self.linger
        while time.perf_counter() < deadline:
            pkt, source = self._receive(max(0.0, deadline - time.perf_counter()))
            if pkt is not None and pkt.type is PacketType.FIN:
                self._send(fin_ack, source or address)
                self._log("FIN_ACK", reason="REPEAT")
        return verdict

    # -- entry point -------------------------------------------------------

    def run(self) -> dict:
        started = time.perf_counter()
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
            elapsed = time.perf_counter() - started
            if self.writer is not None:
                self.writer.close()
            metrics = self._metrics(elapsed, error)
            if self.log is None:
                # No START ever arrived, so nothing named the run. A run that
                # produced no log at all could not be explained afterwards, so
                # one is opened under the fallback run id and whatever was
                # observed — the idle timeout, any rejected packet — is written
                # out (T6.1, M8: a log for *every* run, with no manual steps).
                self._open_log(self.run_id)
            self.log.write_summary(metrics, config_overrides=self._run_config(), extra={
                "output": str(self.output),
                "expected_sha256": self.start_info.sha256 if self.start_info else None,
                "received_sha256": self.received_sha256,
                "final_state": self.state,
                "impairment": self.impairment.as_dict(),
            })
            self.log.close()
            self.sock.close()

        if error:
            raise ReceiverError(error)
        return metrics

    def _run_config(self) -> dict:
        """What this receiver actually ran under, for summary.json (T6.3).

        The receiver carries the ACK half of the emulated round trip and learns
        the file size from START, so both differ from the module defaults.
        """
        impairment = self.impairment
        return {
            "port": self.port,
            "window_size": self.window_size,
            "receiver_idle_timeout_s": self.idle_timeout,
            "random_seed": impairment.seed,
            "loss_rate": impairment.loss_rate,
            "ack_loss_rate": impairment.recv_loss_rate,
            "rtt_ms": impairment.delay_ms * 2.0,
            "jitter_ms": impairment.jitter_ms,
            "loss_schedule": [list(step) for step in impairment.loss_schedule],
            "transfer_file_size_bytes": (self.start_info.filesize
                                         if self.start_info else None),
            "default_mode": config.DEFAULT_MODE,
        }

    def _metrics(self, elapsed: float, error: str | None) -> dict:
        written = self.writer.bytes_written if self.writer else 0
        return {
            "completion_time_s": elapsed,
            "bytes_written": written,
            "segments_written": self.writer.segments_written if self.writer else 0,
            "goodput_bytes_per_s": written / elapsed if elapsed > 0 else 0.0,
            "duplicates_suppressed": self.duplicates,
            "integrity_success": self.integrity_success,
            "requested_mode": self.requested_mode,
            "final_mode": self.mode,
            "switch_count": self.switch_count,
            "mode_epoch": self.mode_epoch,
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
