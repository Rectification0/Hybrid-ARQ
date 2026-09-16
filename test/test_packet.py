"""Packet-layer unit tests (T1.6, specs.md §25.1).

Coverage required by the task: encode/decode round trip including empty and
maximum payload; checksum generation and validation; rejection of short,
bad-MAGIC, bad-VERSION, unknown-TYPE, length-mismatch and corrupted packets;
sequence-number handling. Every PacketError subclass has at least one test.
"""

import json
import struct

import pytest

import config
from protocol import packet as pk
from protocol.packet import Packet, PacketType


def data_packet(seq=0, payload=b"payload", ack=0, window=8, flags=0):
    return Packet(type=PacketType.DATA, seq=seq, ack=ack, window=window,
                  flags=flags, payload=payload)


# ---------------------------------------------------------------------------
# Frozen format (T1.1, T1.3)
# ---------------------------------------------------------------------------


def test_header_layout_is_frozen():
    # If any of these change, every recorded capture and log becomes unreadable.
    assert pk.HEADER_FORMAT == "!HBBBIIHHI"
    assert pk.HEADER_SIZE == 21
    assert pk.CHECKSUM_OFFSET == 17
    assert pk.MAGIC == 0x4841
    assert pk.VERSION == 1


def test_magic_is_ascii_ha_on_the_wire():
    # Readable in the Wireshark ASCII pane, not only the hex pane (WS-04).
    raw = pk.encode(data_packet())
    assert raw[0:2] == b"HA"


def test_packet_type_wire_values_are_frozen():
    assert [int(t) for t in PacketType] == [1, 2, 3, 4, 5, 6, 7]
    assert PacketType.DATA == 1 and PacketType.MODE == 7


def test_header_fields_sit_at_the_documented_offsets():
    # The offset table in the packet.py docstring is what a Wireshark reader
    # uses; this pins it to the code.
    raw = pk.encode(data_packet(seq=0x11223344, ack=0x55667788, window=0x99AA, flags=0x5A))
    assert struct.unpack_from("!H", raw, 0)[0] == pk.MAGIC
    assert raw[2] == pk.VERSION
    assert raw[3] == PacketType.DATA
    assert raw[4] == 0x5A
    assert struct.unpack_from("!I", raw, 5)[0] == 0x11223344
    assert struct.unpack_from("!I", raw, 9)[0] == 0x55667788
    assert struct.unpack_from("!H", raw, 13)[0] == 0x99AA
    assert struct.unpack_from("!H", raw, 15)[0] == len(b"payload")


def test_max_datagram_stays_under_ethernet_mtu():
    # D3: no IP fragmentation may appear in the captures (specs.md §22).
    assert pk.MAX_DATAGRAM_SIZE == pk.HEADER_SIZE + config.SEGMENT_SIZE
    assert pk.MAX_DATAGRAM_SIZE == 1045
    assert pk.MAX_DATAGRAM_SIZE < 1500


# ---------------------------------------------------------------------------
# Round trip (T1.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ptype", list(PacketType))
def test_round_trip_every_packet_type(ptype):
    pkt = Packet(type=ptype, seq=7, ack=6, window=8, flags=1, payload=b"x")
    assert pk.decode(pk.encode(pkt)) == pkt


@pytest.mark.parametrize("payload", [
    b"",                                    # empty
    b"\x00",                                # a single zero byte
    b"a" * 1,
    b"\x00\xff" * 100,                      # binary, not text
    bytes(range(256)) * 4,                  # every byte value
    b"z" * config.SEGMENT_SIZE,             # maximum DATA payload
])
def test_round_trip_payload_sizes(payload):
    pkt = data_packet(seq=3, payload=payload)
    out = pk.decode(pk.encode(pkt))
    assert out.payload == payload
    assert len(out.payload) == len(payload)


def test_round_trip_preserves_every_header_field():
    pkt = Packet(type=PacketType.ACK, seq=123456, ack=654321, window=4096,
                 flags=0xFF, payload=b"")
    out = pk.decode(pk.encode(pkt))
    assert (out.type, out.seq, out.ack, out.window, out.flags) == \
           (PacketType.ACK, 123456, 654321, 4096, 0xFF)


def test_encoded_length_is_header_plus_payload():
    for size in (0, 1, 100, config.SEGMENT_SIZE):
        raw = pk.encode(data_packet(payload=b"q" * size))
        assert len(raw) == pk.HEADER_SIZE + size


def test_decoded_packet_is_immutable():
    out = pk.decode(pk.encode(data_packet()))
    with pytest.raises(Exception):
        out.seq = 99


# ---------------------------------------------------------------------------
# Sequence numbers (T1.3, SEQ-01, SEQ-02)
# ---------------------------------------------------------------------------


def test_initial_sequence_is_zero_and_segment_indexed():
    assert config.INITIAL_SEQUENCE == 0


@pytest.mark.parametrize("seq", [0, 1, 255, 256, 65535, 65536, 2**31, pk.MAX_SEQUENCE])
def test_sequence_numbers_round_trip_across_the_whole_range(seq):
    assert pk.decode(pk.encode(data_packet(seq=seq))).seq == seq


@pytest.mark.parametrize("field,value", [
    ("seq", pk.MAX_SEQUENCE + 1),
    ("seq", -1),
    ("ack", pk.MAX_ACK + 1),
    ("window", pk.MAX_WINDOW + 1),
    ("flags", pk.MAX_FLAGS + 1),
    ("flags", -1),
])
def test_out_of_range_header_field_is_refused_at_encode(field, value):
    pkt = Packet(type=PacketType.DATA, **{field: value})
    with pytest.raises(pk.FieldRangeError):
        pk.encode(pkt)


def test_non_integer_sequence_is_refused():
    with pytest.raises(pk.FieldRangeError):
        pk.encode(Packet(type=PacketType.DATA, seq="4"))


# ---------------------------------------------------------------------------
# Checksum (T1.2, IN-01)
# ---------------------------------------------------------------------------


def test_checksum_is_written_into_the_header():
    raw = pk.encode(data_packet())
    stored = struct.unpack_from("!I", raw, pk.CHECKSUM_OFFSET)[0]
    assert stored == pk.checksum(raw[:pk.CHECKSUM_OFFSET], raw[pk.HEADER_SIZE:])
    assert stored != 0


def test_checksum_covers_the_payload():
    raw = bytearray(pk.encode(data_packet(payload=b"payload")))
    raw[pk.HEADER_SIZE] ^= 0x01
    with pytest.raises(pk.ChecksumError):
        pk.decode(bytes(raw))


@pytest.mark.parametrize("offset,bit", [
    (offset, bit) for offset in range(pk.HEADER_SIZE) for bit in range(8)
])
def test_single_bit_flip_anywhere_in_the_header_is_detected(offset, bit):
    """D2's whole point: coverage is every header field, not just the payload.

    A flipped bit in SEQUENCE must be caught, not silently accepted as a
    different segment. Which subclass fires depends on the field — MAGIC has its
    own check, PAYLOAD_LENGTH fails the length check — but nothing gets through.
    """
    raw = bytearray(pk.encode(data_packet(seq=0x0F0F0F0F, ack=0x33333333,
                                          window=0x0F0F, flags=0x0F,
                                          payload=b"corruptible")))
    raw[offset] ^= 1 << bit
    with pytest.raises(pk.PacketError):
        pk.decode(bytes(raw))


def test_every_header_field_is_covered_by_the_checksum_specifically():
    """The fields with no dedicated validity check must be caught by CRC alone."""
    # TYPE and VERSION have their own checks; MAGIC and PAYLOAD_LENGTH too.
    # FLAGS, SEQUENCE, ACK, WINDOW and CHECKSUM itself are CRC-only.
    crc_only_offsets = [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 17, 18, 19, 20]
    for offset in crc_only_offsets:
        raw = bytearray(pk.encode(data_packet(seq=0x0F0F0F0F, ack=0x33333333,
                                              window=0x0F0F, flags=0x0F)))
        raw[offset] ^= 0x01
        with pytest.raises(pk.ChecksumError):
            pk.decode(bytes(raw))


def test_checksum_differs_for_different_sequence_numbers():
    a = pk.encode(data_packet(seq=1))
    b = pk.encode(data_packet(seq=2))
    assert a[pk.CHECKSUM_OFFSET:pk.HEADER_SIZE] != b[pk.CHECKSUM_OFFSET:pk.HEADER_SIZE]


def test_encoding_is_deterministic():
    # Same packet, same bytes — captures and logs stay comparable between runs.
    pkt = data_packet(seq=9, payload=b"stable")
    assert pk.encode(pkt) == pk.encode(pkt)


# ---------------------------------------------------------------------------
# Rejection paths — one test per PacketError subclass (T1.6, FR-04)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("length", [0, 1, pk.HEADER_SIZE - 1])
def test_short_packet_rejected(length):
    with pytest.raises(pk.ShortPacketError):
        pk.decode(pk.encode(data_packet())[:length])


def test_bad_magic_rejected():
    raw = bytearray(pk.encode(data_packet()))
    raw[0:2] = b"XX"
    with pytest.raises(pk.MagicError):
        pk.decode(bytes(raw))


def test_unrelated_traffic_on_the_port_is_rejected_as_bad_magic():
    # A stray datagram from some other program must not be mistaken for ours.
    with pytest.raises(pk.MagicError):
        pk.decode(b"TEST_PACKET_0" + b"\x00" * 20)


def test_bad_version_rejected():
    raw = bytearray(pk.encode(data_packet()))
    raw[2] = pk.VERSION + 1
    with pytest.raises(pk.VersionError):
        pk.decode(bytes(raw))


@pytest.mark.parametrize("type_value", [0, 8, 99, 255])
def test_unknown_type_rejected(type_value):
    raw = bytearray(pk.encode(data_packet()))
    raw[3] = type_value
    with pytest.raises(pk.UnknownTypeError):
        pk.decode(bytes(raw))


def test_truncated_payload_rejected_as_length_mismatch():
    raw = pk.encode(data_packet(payload=b"0123456789"))
    with pytest.raises(pk.LengthMismatchError):
        pk.decode(raw[:-3])


def test_padded_payload_rejected_as_length_mismatch():
    raw = pk.encode(data_packet(payload=b"0123456789"))
    with pytest.raises(pk.LengthMismatchError):
        pk.decode(raw + b"extra")


def test_corrupted_checksum_field_rejected():
    raw = bytearray(pk.encode(data_packet()))
    raw[pk.CHECKSUM_OFFSET] ^= 0xFF
    with pytest.raises(pk.ChecksumError):
        pk.decode(bytes(raw))


def test_data_payload_over_segment_size_refused():
    with pytest.raises(pk.PayloadTooLargeError):
        pk.encode(data_packet(payload=b"x" * (config.SEGMENT_SIZE + 1)))


def test_payload_over_format_limit_refused():
    with pytest.raises(pk.PayloadTooLargeError):
        pk.encode(Packet(type=PacketType.START, payload=b"x" * (pk.MAX_PAYLOAD_LENGTH + 1)))


def test_non_bytes_payload_refused():
    with pytest.raises(pk.FieldRangeError):
        pk.encode(Packet(type=PacketType.DATA, payload="text"))


def test_every_packet_error_subclass_is_catchable_as_the_base():
    subclasses = [
        pk.ShortPacketError, pk.MagicError, pk.VersionError, pk.UnknownTypeError,
        pk.LengthMismatchError, pk.ChecksumError, pk.PayloadTooLargeError,
        pk.FieldRangeError, pk.ControlPayloadError,
    ]
    for cls in subclasses:
        assert issubclass(cls, pk.PacketError)


def test_decode_reveals_no_payload_when_validation_fails():
    """FR-04/IN-02: a corrupted packet must never hand its payload to a caller."""
    raw = bytearray(pk.encode(data_packet(payload=b"SECRET-CORRUPTED-DATA")))
    raw[pk.HEADER_SIZE + 2] ^= 0xFF
    with pytest.raises(pk.PacketError) as caught:
        pk.decode(bytes(raw))
    assert "SECRET" not in str(caught.value)


# ---------------------------------------------------------------------------
# Control payloads (T1.5)
# ---------------------------------------------------------------------------


CONTROL_CASES = [
    (pk.StartPayload, pk.StartPayload(filename="input.bin", filesize=1048576,
                                      sha256="a" * 64, segment_size=1024,
                                      mode="GBN", run_id="loss05_trial03")),
    (pk.StartAckPayload, pk.StartAckPayload(accepted=True, segment_size=1024)),
    (pk.StartAckPayload, pk.StartAckPayload(accepted=False, segment_size=1024,
                                            reason="segment size mismatch")),
    (pk.FinPayload, pk.FinPayload(total_segments=1024)),
    (pk.FinAckPayload, pk.FinAckPayload(sha256="b" * 64, match=True,
                                        bytes_written=1048576)),
    (pk.FinAckPayload, pk.FinAckPayload(sha256="c" * 64, match=False,
                                        bytes_written=1000)),
    (pk.ModePayload, pk.ModePayload(mode="SR", effective_from_seq=512, epoch=2,
                                    reason="MODE_THRESHOLD")),
]


@pytest.mark.parametrize("cls,obj", CONTROL_CASES)
def test_control_payload_round_trip(cls, obj):
    assert cls.from_payload(obj.to_payload()) == obj


@pytest.mark.parametrize("cls,obj", CONTROL_CASES)
def test_control_payload_survives_a_full_packet_round_trip(cls, obj):
    ptype = next(t for t, c in pk.CONTROL_PAYLOADS.items() if c is cls)
    wire = pk.encode(Packet(type=ptype, payload=obj.to_payload()))
    assert cls.from_payload(pk.decode(wire).payload) == obj


def test_control_payload_encoding_is_deterministic():
    obj = pk.StartPayload("f", 1, "h", 1024, "GBN", "r")
    assert obj.to_payload() == obj.to_payload()
    assert json.loads(obj.to_payload().decode()) == {
        "filename": "f", "filesize": 1, "sha256": "h",
        "segment_size": 1024, "mode": "GBN", "run_id": "r",
    }


def test_every_control_packet_type_has_a_payload_class():
    control_types = {PacketType.START, PacketType.START_ACK, PacketType.FIN,
                     PacketType.FIN_ACK, PacketType.MODE}
    assert set(pk.CONTROL_PAYLOADS) == control_types


@pytest.mark.parametrize("bad", [
    b"",                            # empty
    b"not json at all",
    b"[1,2,3]",                     # JSON, but not an object
    b'{"total_segments"',           # truncated
    b"\xff\xfe\xfd",                # not UTF-8
])
def test_malformed_control_payload_rejected(bad):
    with pytest.raises(pk.ControlPayloadError):
        pk.FinPayload.from_payload(bad)


def test_control_payload_missing_key_rejected():
    with pytest.raises(pk.ControlPayloadError) as caught:
        pk.StartPayload.from_payload(b'{"filename":"f"}')
    assert "filesize" in str(caught.value)


def test_start_payload_fits_in_one_segment():
    # Control packets must not need segmentation of their own.
    obj = pk.StartPayload(filename="a" * 255, filesize=10 * 1024 * 1024,
                          sha256="d" * 64, segment_size=1024, mode="hybrid",
                          run_id="dynamic_loss_e8_trial05")
    assert len(obj.to_payload()) <= config.SEGMENT_SIZE
