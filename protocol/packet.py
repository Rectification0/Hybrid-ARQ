"""Binary packet format: constants, serialization, parsing, checksum.

This module is the single definition of the wire format. Both endpoints import
it and only it for packing and parsing, so the two sides cannot drift apart
(design.md §1).

FROZEN DECISIONS
================

D1 — byte layout, endianness, packing (T1.1)
--------------------------------------------
Network byte order (big-endian), no implicit padding, fixed 21-byte header::

    struct format:  "!HBBBIIHHI"

    offset  size  field           type      notes
      0      2    MAGIC           uint16    0x4841, ASCII "HA"
      2      1    VERSION         uint8     1
      3      1    TYPE            uint8     PacketType
      4      1    FLAGS           uint8     reserved, 0
      5      4    SEQUENCE        uint32    DATA segment index
      9      4    ACK             uint32    acknowledgement number
     13      2    WINDOW          uint16    sender/receiver window
     15      2    PAYLOAD_LENGTH  uint16    length of PAYLOAD in bytes
     17      4    CHECKSUM        uint32    CRC-32, see D2
     21      -    PAYLOAD         bytes     PAYLOAD_LENGTH bytes

The leading "!" is explicit: it prevents platform-dependent alignment, which
would otherwise silently break a cross-machine run. MAGIC is ASCII "HA" so the
packet is identifiable in the Wireshark ASCII pane, not only the hex pane
(WS-04, WS-05).

D2 — checksum algorithm and coverage (T1.2)
--------------------------------------------
CRC-32 (``zlib.crc32``) computed over **the full 21-byte header with the
CHECKSUM field zeroed, followed by the payload**::

    CHECKSUM = crc32( header[0:17] || 00 00 00 00 || payload )

Coverage is every byte of the packet. This is deliberate and cannot be inferred
from the wire bytes, which is why it is written down here: a corrupted
SEQUENCE, TYPE or WINDOW is caught, not merely a corrupted payload. CRC-32 is
deterministic across Python versions and cheap enough to run on every packet.

It detects accidental corruption only — it is not a MAC and offers no defence
against deliberate modification, which is out of scope (specs.md §3).

D3 — maximum DATA payload (T1.3)
---------------------------------
``config.SEGMENT_SIZE`` = 1024 bytes. 21 + 1024 = 1045 bytes on the wire, well
under a 1500-byte Ethernet MTU, so no IP fragmentation confuses the capture
evidence (specs.md §22). PAYLOAD_LENGTH is 2 bytes so the *format* permits
65535; ``SEGMENT_SIZE`` is the *configured* limit and is what DATA is held to.

D4 — initial sequence number (T1.3)
------------------------------------
``config.INITIAL_SEQUENCE`` = 0. Sequence numbers are **segment indices**, not
byte offsets (SEQ-01, SEQ-02): segment n carries file bytes
``[n * SEGMENT_SIZE, (n+1) * SEGMENT_SIZE)``. Segment indexing keeps window
arithmetic, logs and Wireshark reading directly comparable to textbook GBN/SR.
Wraparound is out of scope (SEQ-04): a uint32 at 1 KiB segments covers ~4 TiB
in a single transfer.

VALIDATION ORDER
================
``decode`` performs every check before a caller can reach the payload (FR-04,
IN-02), in this order, each failure being a discard-and-log case (specs.md §13):

1. length >= HEADER_SIZE              -> ShortPacketError
2. MAGIC matches                      -> MagicError
3. VERSION supported                  -> VersionError
4. TYPE is a known value              -> UnknownTypeError
5. trailing bytes == PAYLOAD_LENGTH   -> LengthMismatchError
6. checksum recomputes                -> ChecksumError

The distinct subclasses exist so endpoints can log *why* a packet was dropped,
which is what keeps integrity failures separable from simulated loss (IN-04).
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import config

# ---------------------------------------------------------------------------
# Wire constants — frozen (D1). Changing any of these breaks compatibility with
# every capture and log already recorded.
# ---------------------------------------------------------------------------

MAGIC = 0x4841              # ASCII "HA"
VERSION = 1

_HEADER = struct.Struct("!HBBBIIHHI")
HEADER_FORMAT = _HEADER.format
HEADER_SIZE = _HEADER.size          # 21
CHECKSUM_OFFSET = 17                # CHECKSUM is the final header field

MAX_SEQUENCE = 0xFFFFFFFF
MAX_ACK = 0xFFFFFFFF
MAX_WINDOW = 0xFFFF
MAX_FLAGS = 0xFF
MAX_PAYLOAD_LENGTH = 0xFFFF         # format limit; DATA is held to SEGMENT_SIZE

#: Largest datagram the protocol can put on the wire, and therefore the read
#: size a receiving socket must use. Derived, not tunable: the tunable is
#: ``config.SEGMENT_SIZE``.
MAX_DATAGRAM_SIZE = HEADER_SIZE + config.SEGMENT_SIZE


class PacketType(IntEnum):
    """Wire values are frozen (D1); never renumber them."""

    DATA = 1
    ACK = 2
    START = 3
    START_ACK = 4
    FIN = 5
    FIN_ACK = 6
    MODE = 7


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PacketError(Exception):
    """Base for every packet-layer failure.

    Callers may catch this alone to discard-and-log; the subclass says why,
    which is what keeps integrity failures separable from simulated loss.
    """


class ShortPacketError(PacketError):
    """Fewer bytes than a complete header."""


class MagicError(PacketError):
    """MAGIC does not match — not one of our packets."""


class VersionError(PacketError):
    """VERSION is not supported by this build."""


class UnknownTypeError(PacketError):
    """TYPE is not a defined PacketType value."""


class LengthMismatchError(PacketError):
    """Trailing byte count disagrees with PAYLOAD_LENGTH (truncated or padded)."""


class ChecksumError(PacketError):
    """CRC-32 does not recompute — the packet is corrupted somewhere."""


class PayloadTooLargeError(PacketError):
    """Encode-side: payload exceeds the format or the configured limit."""


class FieldRangeError(PacketError):
    """Encode-side: a header field does not fit its width."""


class ControlPayloadError(PacketError):
    """A control payload is not the JSON object its packet type requires."""


# ---------------------------------------------------------------------------
# Packet
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Packet:
    """One protocol packet, decoded.

    Immutable, so a received packet cannot be mutated between validation and
    use.
    """

    type: PacketType
    seq: int = 0
    ack: int = 0
    window: int = 0
    flags: int = 0
    payload: bytes = b""

    def __repr__(self) -> str:  # keeps log lines and test failures readable
        return (
            f"Packet({self.type.name} seq={self.seq} ack={self.ack} "
            f"win={self.window} flags={self.flags} len={len(self.payload)})"
        )


def _check_range(name: str, value: int, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FieldRangeError(f"{name} must be an int, got {type(value).__name__}")
    if not 0 <= value <= maximum:
        raise FieldRangeError(f"{name}={value} outside 0..{maximum}")


def checksum(header_without_checksum: bytes, payload: bytes) -> int:
    """CRC-32 over the header with CHECKSUM zeroed, plus the payload (D2).

    ``header_without_checksum`` is the first ``CHECKSUM_OFFSET`` header bytes.
    The four zero bytes standing in for the checksum field are supplied here, so
    callers cannot accidentally disagree about the coverage.
    """
    return zlib.crc32(header_without_checksum + b"\x00\x00\x00\x00" + payload) & 0xFFFFFFFF


def encode(pkt: Packet) -> bytes:
    """Serialize a packet to its wire representation.

    Raises rather than emitting a packet that cannot be parsed back: a
    malformed packet must fail at its source, not silently on the far side.
    """
    ptype = PacketType(pkt.type)
    payload = pkt.payload if pkt.payload is not None else b""
    if not isinstance(payload, (bytes, bytearray)):
        raise FieldRangeError(f"payload must be bytes, got {type(payload).__name__}")
    payload = bytes(payload)

    if len(payload) > MAX_PAYLOAD_LENGTH:
        raise PayloadTooLargeError(
            f"payload {len(payload)} exceeds format limit {MAX_PAYLOAD_LENGTH}"
        )
    if ptype is PacketType.DATA and len(payload) > config.SEGMENT_SIZE:
        raise PayloadTooLargeError(
            f"DATA payload {len(payload)} exceeds SEGMENT_SIZE {config.SEGMENT_SIZE} (D3)"
        )

    _check_range("seq", pkt.seq, MAX_SEQUENCE)
    _check_range("ack", pkt.ack, MAX_ACK)
    _check_range("window", pkt.window, MAX_WINDOW)
    _check_range("flags", pkt.flags, MAX_FLAGS)

    header = _HEADER.pack(
        MAGIC, VERSION, int(ptype), pkt.flags,
        pkt.seq, pkt.ack, pkt.window, len(payload), 0,
    )
    crc = checksum(header[:CHECKSUM_OFFSET], payload)
    return header[:CHECKSUM_OFFSET] + struct.pack("!I", crc) + payload


def decode(raw: bytes) -> Packet:
    """Parse and fully validate a datagram.

    Every check runs before this returns, so no caller can reach a payload that
    has not been integrity-checked (FR-04, IN-02). Any failure raises a
    PacketError subclass naming the reason.
    """
    if len(raw) < HEADER_SIZE:
        raise ShortPacketError(f"{len(raw)} bytes, need at least {HEADER_SIZE}")

    (magic, version, type_value, flags,
     seq, ack, window, payload_len, crc) = _HEADER.unpack_from(raw, 0)

    if magic != MAGIC:
        raise MagicError(f"MAGIC 0x{magic:04X} != 0x{MAGIC:04X}")
    if version != VERSION:
        raise VersionError(f"VERSION {version} unsupported, this build speaks {VERSION}")
    try:
        ptype = PacketType(type_value)
    except ValueError:
        raise UnknownTypeError(f"TYPE {type_value} is not a defined PacketType") from None

    payload = raw[HEADER_SIZE:]
    if len(payload) != payload_len:
        raise LengthMismatchError(
            f"PAYLOAD_LENGTH says {payload_len}, {len(payload)} bytes present"
        )

    expected = checksum(raw[:CHECKSUM_OFFSET], payload)
    if crc != expected:
        raise ChecksumError(f"CHECKSUM 0x{crc:08X} != computed 0x{expected:08X}")

    return Packet(type=ptype, seq=seq, ack=ack, window=window, flags=flags, payload=payload)


# ---------------------------------------------------------------------------
# Control payloads (T1.5)
# ---------------------------------------------------------------------------
#
# START, START_ACK, FIN, FIN_ACK and MODE carry metadata the header cannot
# express. They are encoded as a compact JSON object in the payload:
# self-describing, readable in the Wireshark payload pane without a dissector,
# and cheap because these packets are rare (a handful per transfer).
#
# Keys are sorted and separators are compact, so the same logical payload always
# produces identical bytes — one less source of variation between runs (RP-01).


def _dump(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load(payload: bytes, required: tuple[str, ...]) -> dict[str, Any]:
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPayloadError(f"payload is not UTF-8 JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise ControlPayloadError(f"expected a JSON object, got {type(obj).__name__}")
    missing = [key for key in required if key not in obj]
    if missing:
        raise ControlPayloadError(f"missing required key(s): {', '.join(missing)}")
    return obj


@dataclass(frozen=True)
class StartPayload:
    """START: everything the receiver needs before the first DATA arrives.

    ``sha256`` is the hash of the source file, compared against the
    reconstructed output at FIN time (IN-05).
    """

    filename: str
    filesize: int
    sha256: str
    segment_size: int
    mode: str
    run_id: str

    _REQUIRED = ("filename", "filesize", "sha256", "segment_size", "mode", "run_id")

    def to_payload(self) -> bytes:
        return _dump({
            "filename": self.filename,
            "filesize": self.filesize,
            "sha256": self.sha256,
            "segment_size": self.segment_size,
            "mode": self.mode,
            "run_id": self.run_id,
        })

    @classmethod
    def from_payload(cls, payload: bytes) -> StartPayload:
        obj = _load(payload, cls._REQUIRED)
        return cls(
            filename=str(obj["filename"]),
            filesize=int(obj["filesize"]),
            sha256=str(obj["sha256"]),
            segment_size=int(obj["segment_size"]),
            mode=str(obj["mode"]),
            run_id=str(obj["run_id"]),
        )


@dataclass(frozen=True)
class StartAckPayload:
    """START_ACK: the receiver accepts (or refuses) the transfer.

    ``segment_size`` is echoed so a disagreement is caught at setup rather than
    surfacing later as a corrupted file.
    """

    accepted: bool
    segment_size: int
    reason: str = ""

    _REQUIRED = ("accepted", "segment_size")

    def to_payload(self) -> bytes:
        return _dump({
            "accepted": self.accepted,
            "segment_size": self.segment_size,
            "reason": self.reason,
        })

    @classmethod
    def from_payload(cls, payload: bytes) -> StartAckPayload:
        obj = _load(payload, cls._REQUIRED)
        return cls(
            accepted=bool(obj["accepted"]),
            segment_size=int(obj["segment_size"]),
            reason=str(obj.get("reason", "")),
        )


@dataclass(frozen=True)
class FinPayload:
    """FIN: the sender believes every segment is acknowledged."""

    total_segments: int

    _REQUIRED = ("total_segments",)

    def to_payload(self) -> bytes:
        return _dump({"total_segments": self.total_segments})

    @classmethod
    def from_payload(cls, payload: bytes) -> FinPayload:
        obj = _load(payload, cls._REQUIRED)
        return cls(total_segments=int(obj["total_segments"]))


@dataclass(frozen=True)
class FinAckPayload:
    """FIN_ACK: the receiver reports the integrity verdict.

    ``match`` false is a reported integrity failure, never silent corruption
    and never presented as success (IN-05, CC-01).
    """

    sha256: str
    match: bool
    bytes_written: int

    _REQUIRED = ("sha256", "match", "bytes_written")

    def to_payload(self) -> bytes:
        return _dump({
            "sha256": self.sha256,
            "match": self.match,
            "bytes_written": self.bytes_written,
        })

    @classmethod
    def from_payload(cls, payload: bytes) -> FinAckPayload:
        obj = _load(payload, cls._REQUIRED)
        return cls(
            sha256=str(obj["sha256"]),
            match=bool(obj["match"]),
            bytes_written=int(obj["bytes_written"]),
        )


@dataclass(frozen=True)
class ModePayload:
    """MODE: the mode-switch handshake (D10, implemented in T5.4).

    ``effective_from_seq`` pins the boundary explicitly and ``epoch`` guards
    against a stale echo arriving late and reactivating a superseded switch —
    an echo whose epoch is not current is discarded and logged (HY-07).
    """

    mode: str
    effective_from_seq: int
    epoch: int
    reason: str = ""

    _REQUIRED = ("mode", "effective_from_seq", "epoch")

    def to_payload(self) -> bytes:
        return _dump({
            "mode": self.mode,
            "effective_from_seq": self.effective_from_seq,
            "epoch": self.epoch,
            "reason": self.reason,
        })

    @classmethod
    def from_payload(cls, payload: bytes) -> ModePayload:
        obj = _load(payload, cls._REQUIRED)
        return cls(
            mode=str(obj["mode"]),
            effective_from_seq=int(obj["effective_from_seq"]),
            epoch=int(obj["epoch"]),
            reason=str(obj.get("reason", "")),
        )


#: Maps a control packet type to the payload class that parses it, so endpoints
#: can dispatch without a chain of if-statements.
CONTROL_PAYLOADS: dict[PacketType, Any] = {
    PacketType.START: StartPayload,
    PacketType.START_ACK: StartAckPayload,
    PacketType.FIN: FinPayload,
    PacketType.FIN_ACK: FinAckPayload,
    PacketType.MODE: ModePayload,
}
