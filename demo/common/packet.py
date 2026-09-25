import struct

# Demo wire format -- NOT the frozen D1 format in protocol/packet.py ("HA", 21 bytes).
# Kept exactly as in Assessment 7 so the recorded Wireshark screenshots still match.
MAGIC = b"ARQ1"
DATA = 1
ACK = 2
HEADER = "!4sBIHH"


class ChecksumError(ValueError):
    """Checksum mismatch -- kept distinct from a malformed packet (cf. IN-04)."""


def checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def make_data(seq, payload):
    base = struct.pack("!4sBIH", MAGIC, DATA, seq, 0) + payload
    csum = checksum(base)
    return struct.pack(HEADER, MAGIC, DATA, seq, csum, len(payload)) + payload


def make_ack(seq):
    base = struct.pack("!4sBIH", MAGIC, ACK, seq, 0)
    csum = checksum(base)
    return struct.pack(HEADER, MAGIC, ACK, seq, csum, 0)


def parse(packet):
    size = struct.calcsize(HEADER)
    if len(packet) < size:
        raise ValueError("Packet too short")
    magic, ptype, seq, received_checksum, payload_len = struct.unpack(
        HEADER, packet[:size]
    )
    if magic != MAGIC:
        raise ValueError("Invalid ARQ packet")
    payload = packet[size:]
    if len(payload) != payload_len:
        raise ValueError("Invalid payload length")
    base = struct.pack("!4sBIH", MAGIC, ptype, seq, 0) + payload
    if checksum(base) != received_checksum:
        raise ChecksumError("Checksum mismatch")
    return ptype, seq, payload
