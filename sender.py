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
from protocol import hybrid
from protocol import packet as pk
from protocol.packet import Packet, PacketType
from network.simulator import Impairment
from network.udp import open_udp_socket
from protocol.strategy import SenderTransferState, make_sender_strategy

#: Modes the CLI accepts, mapped to the task that delivers each. A value of None
#: means implemented; anything else is named so the error says which task brings
#: it rather than "invalid choice".
MODE_TASKS = {
    "saw": None,
    "gbn": None,
    "sr": None,
    "hybrid": None,
    "fixed-hybrid": None,
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
                 rto: float | None = None, sock: socket.socket | None = None,
                 impairment: Impairment | None = None,
                 hybrid_settings: hybrid.HybridSettings | None = None):
        self.path = Path(path)
        self.host = host
        self.port = port
        self.mode = mode
        self.window = window
        self.run_id = run_id
        self.rto = config.RTO_S if rto is None else rto

        # ``mode`` is what the CLI asked for and what travels in START; it may
        # name a controller ("hybrid") rather than a strategy. ``active_mode`` is
        # the ARQ mode actually live on the wire, and is what every event logs —
        # a SWITCH is only readable if the rows around it name real modes.
        self.hybrid_settings = hybrid_settings
        self.controller: hybrid.HybridController | None = None
        self.active_mode = (hybrid.initial_mode() if mode in hybrid.HYBRID_MODES
                            else mode)
        self._pending_switch: hybrid.SwitchDecision | None = None

        self.log = EventLog(run_id, "sender", log_dir=log_dir)
        self._owns_socket = sock is None
        self.impairment = impairment or Impairment()
        self.sock = self.impairment.wrap(sock or open_udp_socket(), self._on_impairment)

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
        deadline = time.perf_counter() + timeout
        while True:
            remaining = deadline - time.perf_counter()
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
                self.log.emit("CHECKSUM_FAIL", mode=self.active_mode, reason=str(exc))
            except pk.PacketError as exc:
                self.log.emit("MALFORMED", mode=self.active_mode,
                              reason=f"{type(exc).__name__}: {exc}")

    def _on_impairment(self, kind: str, direction: str, raw, **detail) -> None:
        """Log what the simulator did to a packet.

        Decoding happens here rather than in the simulator so the network layer
        stays ignorant of the packet format. A DROP is a *simulated* loss and is
        deliberately a different event from CHECKSUM_FAIL, which is real
        corruption — conflating them would leave the analysis unable to tell an
        injected condition from a protocol defect (IN-04, T4.3).
        """
        sequence = None
        reason = detail.get("reason")
        if raw is not None:
            try:
                decoded = pk.decode(raw)
                sequence = decoded.seq
                reason = f"{direction};{decoded.type.name}"
            except pk.PacketError:
                reason = f"{direction};undecodable"
        self.log.emit(kind, sequence=sequence, mode=self.active_mode,
                      window_size=self.window, reason=reason)

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
            self.log.emit("START", mode=self.active_mode, window_size=self.window,
                          reason=f"attempt {attempt}")
            reply = self._receive(config.CONTROL_RETRY_TIMEOUT_S)
            if reply is None:
                self.log.emit("TIMEOUT", mode=self.active_mode, reason="START_ACK")
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
            self.log.emit("START_ACK", mode=self.active_mode, window_size=self.window)
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

        # A hybrid transfer runs a real ARQ strategy at every instant; the
        # controller only decides *which* one, and never sits in the data path.
        self.controller = hybrid.make_controller(
            self.mode, time.perf_counter(), self.hybrid_settings)
        strategy = make_sender_strategy(self.active_mode, state, self.rto)

        self._sent_once = set()
        self._sent_at = {}
        self._retransmitted = set()
        self._attempts = {}

        while not strategy.all_acked():
            if self._pending_switch is None:
                for seq in strategy.packets_to_send(time.perf_counter()):
                    self._emit_data(seq, is_retx=False)
            elif state.is_quiescent():
                # The window has drained: no packet's fate now depends on which
                # ACK semantics are live, which is the only safe moment to swap
                # them (design.md §6.3).
                strategy = self._switch_mode(strategy, state)
                continue
            # While a switch is pending, no *new* segment enters the window, but
            # timers keep being serviced under the current mode — the drain has
            # to make progress even if the last outstanding segments are lost.
            self._drain_timers(strategy)

            # Wait only as long as the nearest timer allows. Blocking for a full
            # RTO regardless would let a timer armed mid-wait go unnoticed until
            # the following wait ended, making a retransmission up to two RTOs
            # late — visible as inflated completion times under loss.
            pending = strategy.next_timeout(time.perf_counter())
            wait = self.rto if pending is None else max(0.0, min(pending, self.rto))
            reply = self._receive(wait)
            now = time.perf_counter()

            if reply is not None and reply.type is PacketType.MODE:
                # An echo from a handshake that has already been committed or
                # abandoned. Discarding it is what stops a late echo from
                # reactivating a superseded switch (HY-07).
                self.log.emit("MODE", mode=self.active_mode, reason="STALE_ECHO")
                continue

            if reply is not None and reply.type is PacketType.ACK:
                result = strategy.on_ack(reply.ack, now)
                self.log.emit("ACK", sequence=reply.ack, ack=reply.ack,
                              mode=self.active_mode, window_size=self.window,
                              loss_estimate=(self.controller.loss_estimate
                                             if self.controller else None),
                              reason="DUPLICATE" if result.duplicate else None)
                for seq in result.newly_acked:
                    # Karn's rule: a retransmitted segment's ACK is ambiguous,
                    # so it never becomes an RTT sample (design.md §5.4).
                    retransmitted = seq in self._retransmitted
                    if not retransmitted and seq in self._sent_at:
                        sample = (now - self._sent_at[seq]) * 1000.0
                        self.rtt_samples.append(sample)
                        if self.controller:
                            self.controller.on_rtt_sample(sample)
                    if self.controller:
                        self.controller.on_segment_acked(
                            seq, retransmitted=retransmitted, now=now)
                self._drain_timers(strategy)
                if self.controller and self._pending_switch is None:
                    self._pending_switch = self.controller.evaluate(now)
                continue

            expired = strategy.on_timeout(now)
            if not expired:
                continue

            # One timer expiry, then the range it forces. Under GBN that range
            # is every outstanding segment, which is the cost the hybrid exists
            # to avoid — so it is logged segment by segment (GBN-05, TO-03).
            self.log.emit("TIMEOUT", sequence=expired[0], mode=self.active_mode,
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
            self.log.emit(event.kind, sequence=event.seq, mode=self.active_mode,
                          window_size=self.window)

    def _emit_data(self, seq: int, *, is_retx: bool) -> None:
        payload = self.segments[seq]
        size = self._send(Packet(type=PacketType.DATA, seq=seq,
                                 window=self.window, payload=payload))
        self._sent_at[seq] = time.perf_counter()
        self._attempts[seq] = self._attempts.get(seq, 0) + 1

        retransmission = is_retx or seq in self._sent_once
        if retransmission:
            self._retransmitted.add(seq)
            self.data_retransmitted += 1
            self.bytes_retransmitted += size
            self.log.emit("RETX", sequence=seq, mode=self.active_mode,
                          window_size=self.window, size_bytes=size,
                          reason="TIMEOUT")
        else:
            self._sent_once.add(seq)
            self.data_sent += 1
            self.log.emit("SEND", sequence=seq, mode=self.active_mode,
                          window_size=self.window, size_bytes=size)
        self.bytes_sent += size
        if self.controller:
            self.controller.on_transmission(seq, retransmitted=retransmission)

    # -- mode switching (T5.4) ---------------------------------------------

    def _switch_mode(self, strategy, state: SenderTransferState):
        """Run the MODE handshake at a quiescent boundary (D10, design.md §6.3).

        Called only with ``state.is_quiescent()`` true, so every segment sent so
        far is acknowledged and nothing is in flight to be misread under the
        other mode's ACK convention. Returns the strategy to carry on with —
        the new one on success, the unchanged one if the handshake is abandoned.

        Under ``--mode fixed-hybrid`` the handshake still runs, but names the
        mode already in force: the control pays the drain and the exchange
        without changing semantics (T5.6).
        """
        decision = self._pending_switch
        self._pending_switch = None
        epoch = self.controller.begin_switch()
        target = (decision.target_mode if self.controller.switching_enabled
                  else self.active_mode)
        boundary = state.next_seq

        payload = pk.ModePayload(
            mode=target, effective_from_seq=boundary, epoch=epoch,
            reason=decision.reason,
        ).to_payload()
        mode_packet = Packet(type=PacketType.MODE, seq=boundary,
                             window=self.window, payload=payload)

        for attempt in range(1, config.CONTROL_RETRY_LIMIT + 1):
            self._send(mode_packet)
            self.log.emit("MODE", sequence=boundary, mode=self.active_mode,
                          window_size=self.window,
                          loss_estimate=decision.loss_estimate,
                          reason=f"REQUEST;target={target};epoch={epoch};"
                                 f"attempt {attempt}")
            deadline = time.perf_counter() + config.CONTROL_RETRY_TIMEOUT_S
            while True:
                remaining = deadline - time.perf_counter()
                reply = self._receive(remaining) if remaining > 0 else None
                if reply is None:
                    self.log.emit("TIMEOUT", sequence=boundary,
                                  mode=self.active_mode,
                                  reason=f"MODE_ECHO;attempt {attempt}")
                    break
                if reply.type is not PacketType.MODE:
                    continue        # a duplicate ACK arriving after quiescence
                echo = pk.ModePayload.from_payload(reply.payload)
                if echo.epoch != epoch or echo.mode != target:
                    self.log.emit("MODE", mode=self.active_mode,
                                  reason=f"STALE_ECHO;epoch={echo.epoch}")
                    continue
                return self._commit_switch(strategy, state, decision, target, epoch)

        # Out of retries. The transfer continues in the current mode: a failed
        # switch is a logged non-event, never a half-switched transfer
        # (specs.md §13, design.md §6.3 step 5).
        self.controller.abandon_switch(time.perf_counter())
        self.log.emit("TIMEOUT", sequence=boundary, mode=self.active_mode,
                      loss_estimate=decision.loss_estimate,
                      reason=f"MODE_HANDSHAKE_ABANDONED;target={target}")
        return strategy

    def _commit_switch(self, strategy, state: SenderTransferState,
                       decision, target: str, epoch: int):
        """The receiver echoed. Adopt the new mode and rebuild the strategy."""
        now = time.perf_counter()
        previous = self.active_mode
        self.controller.commit_switch(decision, now)

        if not self.controller.switching_enabled:
            # T5.6: the exchange happened, the semantics did not change. Logged
            # as a SWITCH so the drain cost is visible in the evidence, with a
            # reason that keeps it out of any count of real transitions.
            self.log.emit("SWITCH", sequence=state.next_seq, mode=self.active_mode,
                          window_size=self.window,
                          loss_estimate=decision.loss_estimate,
                          reason=(f"FIXED_HYBRID_NOOP;from={previous};"
                                  f"would={decision.target_mode};epoch={epoch}"))
            return strategy

        self._drain_timers(strategy)        # the old strategy's last transitions
        self.active_mode = target
        rebuilt = make_sender_strategy(target, state, self.rto)
        # Segment payloads and the acknowledgement set live in ``state``, not in
        # the strategy, so nothing unacknowledged can be lost here — the
        # invariant holds by construction rather than by argument (HY-05).
        self.log.emit("SWITCH", sequence=state.next_seq, mode=target,
                      window_size=self.window,
                      loss_estimate=decision.loss_estimate,
                      reason=f"{decision.reason};from={previous};epoch={epoch}")
        return rebuilt

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
            self.log.emit("FIN", sequence=len(self.segments), mode=self.active_mode,
                          reason=f"attempt {attempt}")
            reply = self._receive(config.CONTROL_RETRY_TIMEOUT_S)
            if reply is None:
                self.log.emit("TIMEOUT", mode=self.active_mode, reason="FIN_ACK")
                continue
            if reply.type is not PacketType.FIN_ACK:
                continue
            verdict = pk.FinAckPayload.from_payload(reply.payload)
            self.log.emit("FIN_ACK", mode=self.active_mode,
                          reason="MATCH" if verdict.match else "HASH_MISMATCH")
            return verdict

        raise TransferError(
            f"no FIN_ACK after {config.CONTROL_RETRY_LIMIT} attempts — "
            f"transfer completed but was never confirmed"
        )

    # -- entry point -------------------------------------------------------

    def run(self) -> dict:
        """Run the transfer. Returns the metrics dict written to summary.json."""
        started = time.perf_counter()
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
            self.log.emit("ERROR", mode=self.active_mode, reason=error)
        finally:
            elapsed = time.perf_counter() - started
            metrics = self._metrics(elapsed, verdict, error)
            self.log.write_summary(metrics, config_overrides=self._run_config(), extra={
                "source": {
                    "filename": self.path.name,
                    "filesize": self.filesize,
                    "sha256": self.sha256,
                    "total_segments": len(self.segments),
                },
                "final_state": self.state,
                "impairment": self.impairment.as_dict(),
                "rto_s": self.rto,
                # specs.md §10, in full, for a hybrid run; absent for a fixed
                # strategy, where there is no controller to have a state.
                "controller": (self.controller.snapshot(time.perf_counter())
                               if self.controller else None),
            })
            self.log.close()
            if self._owns_socket:
                self.sock.close()

        if error:
            raise TransferError(error)
        return metrics

    def _run_config(self) -> dict:
        """What this run actually used, for summary.json (T6.3, RP-01, RP-02).

        The seed, the impairment condition, the derived RTO and the file size all
        come from the command line or from D7's per-condition derivation, so the
        module defaults describe none of them.
        """
        impairment = self.impairment
        overrides = {
            "window_size": self.window,
            "rto_s": self.rto,
            "random_seed": impairment.seed,
            "loss_rate": impairment.loss_rate,
            "ack_loss_rate": impairment.recv_loss_rate,
            # Each direction carries half the round trip (design.md §8), so the
            # condition's RTT is twice what this endpoint was configured with.
            "rtt_ms": impairment.delay_ms * 2.0,
            "jitter_ms": impairment.jitter_ms,
            "loss_schedule": [list(step) for step in impairment.loss_schedule],
            "transfer_file_size_bytes": self.filesize,
        }
        if self.controller is not None:
            settings = self.controller.settings
            overrides.update({
                "loss_window_size": settings.loss_window_size,
                "switch_high": settings.switch_high,
                "switch_low": settings.switch_low,
                "hysteresis_count": settings.hysteresis_count,
                "evaluation_interval_segments": settings.evaluation_interval_segments,
                "min_mode_residence_s": settings.min_mode_residence_s,
            })
        return overrides

    def _metrics(self, elapsed: float, verdict, error: str | None) -> dict:
        """specs.md §20. T6.2 audits that every one is also derivable from
        events.csv alone (CC-06); these are the cross-check.

        Switch count and mode residence come from the controller when one is
        running, and are zero for a fixed strategy — which is the truth about a
        transfer that never had a second mode to be in.
        """
        delivered = self.filesize if (verdict and verdict.match) else 0
        transmitted = self.bytes_sent or 1
        controller = self.controller
        residence = (controller.stats.residence_times(time.perf_counter())
                     if controller else {})
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
            "mode": self.mode,
            "final_active_mode": self.active_mode,
            "switch_count": controller.stats.switch_count if controller else 0,
            "handshake_count": controller.stats.handshake_count if controller else 0,
            "abandoned_switches": controller.stats.abandoned_switches if controller else 0,
            "gbn_residence_s": residence.get("gbn", 0.0),
            "sr_residence_s": residence.get("sr", 0.0),
            "loss_estimate": controller.loss_estimate if controller else None,
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
                        help="ARQ mode; 'gbn' and 'sr' are the measured baselines, "
                             "'hybrid' switches between them, 'fixed-hybrid' is the "
                             "switching-overhead control, 'saw' is the Phase 2 placeholder")
    parser.add_argument("--window", type=int, default=config.WINDOW_SIZE,
                        help="outstanding segments; ignored by --mode saw")
    parser.add_argument("--run-id", default=None, help="defaults to a generated id")
    parser.add_argument("--log-dir", default=None, type=Path)
    parser.add_argument("--rto", type=float, default=None, help="override RTO seconds")

    impair = parser.add_argument_group(
        "impairment", "controlled loss and delay (network/simulator.py)")
    impair.add_argument("--loss", type=float, default=config.LOSS_RATE,
                        help="DATA loss probability on the send path")
    impair.add_argument("--ack-loss", type=float, default=config.ACK_LOSS_RATE,
                        help="ACK loss probability on the receive path")
    impair.add_argument("--rtt", type=float, default=config.RTT_MS,
                        help="emulated round-trip time in ms; half is applied here")
    impair.add_argument("--jitter", type=float, default=config.JITTER_MS,
                        help="bounded delay jitter in ms")
    impair.add_argument("--seed", type=int, default=config.RANDOM_SEED,
                        help="impairment seed; the same seed replays the same drops")
    impair.add_argument("--loss-schedule", default=None,
                        help="dynamic loss as t:rate,t:rate (seconds:probability)")
    return parser


def parse_loss_schedule(text):
    """Parse a dynamic-loss step function, e.g. 5:0.1,10:0.0 (specs.md 17.3)."""
    if not text:
        return ()
    steps = []
    for chunk in text.split(","):
        moment, separator, rate = chunk.partition(":")
        if not separator:
            raise ValueError(f"loss schedule step {chunk!r} is not t:rate")
        steps.append((float(moment), float(rate)))
    return tuple(sorted(steps))


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
    impairment = Impairment(
        loss_rate=args.loss, recv_loss_rate=args.ack_loss,
        delay_ms=args.rtt / 2.0, jitter_ms=args.jitter, seed=args.seed,
        loss_schedule=parse_loss_schedule(args.loss_schedule))
    # D7: one RTO per condition, derived from that condition RTT and then held
    # fixed for the whole run, so the three systems stay comparable within a cell.
    rto = args.rto if args.rto is not None else config.baseline_rto(args.rtt)
    sender = Sender(path=args.file, host=args.host, port=args.port, mode=args.mode,
                    window=args.window, run_id=run_id, log_dir=args.log_dir,
                    rto=rto, impairment=impairment)
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
    if sender.controller is not None:
        print(f"mode: started {config.DEFAULT_MODE.lower()}, ended "
              f"{metrics['final_active_mode']}, {metrics['switch_count']} switches "
              f"over {metrics['handshake_count']} MODE handshakes "
              f"({metrics['abandoned_switches']} abandoned)")
    print(f"integrity: {'OK' if metrics['integrity_success'] else 'FAILED'}")
    print(f"logs: {sender.log.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
