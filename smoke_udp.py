"""Environment + wire-format smoke test (T0.1, T1.7).

Originally the M1 connectivity spike ``test_udp.py``, which sent plain
``TEST_PACKET_n`` strings. Per T1.7 those placeholders are gone: this now sends
**real encoded packets** in the frozen format, so the same Wireshark check that proved
connectivity in M1 now also proves the header is readable at fixed offsets
(FR-12, WS-04, WS-05).

It is deliberately not a protocol test — it asserts nothing about ARQ behavior,
sends no retransmissions, and expects no ACKs. The packet layer is covered by
test/test_packet.py; this exists to confirm the environment end to end and to
produce something to look at in Wireshark. It is named ``smoke_udp.py`` rather
than ``test_udp.py`` precisely because it is a script: under the old name pytest
matched it as a test module and imported it during collection.

Run alongside test/spike_receiver.py, not the real receiver:

    python test/spike_receiver.py      # terminal 1
    python smoke_udp.py                # terminal 2

Wireshark / tshark filter:  udp.port == 8888
"""

import argparse
import socket
import time

import config
from protocol import packet as pk
from protocol.packet import Packet, PacketType

#: Offsets come from the frozen layout in protocol/packet.py (D1). They are
#: printed with each packet so the hex pane in Wireshark can be read straight
#: off this table without a dissector.
FIELD_OFFSETS = [
    ("MAGIC", 0, 2), ("VERSION", 2, 1), ("TYPE", 3, 1), ("FLAGS", 4, 1),
    ("SEQUENCE", 5, 4), ("ACK", 9, 4), ("WINDOW", 13, 2),
    ("PAYLOAD_LENGTH", 15, 2), ("CHECKSUM", 17, 4),
]


def annotate(raw: bytes) -> str:
    """Render a packet's header as field = hex, at documented offsets."""
    parts = []
    for name, offset, size in FIELD_OFFSETS:
        parts.append(f"{name}@{offset}={raw[offset:offset + size].hex()}")
    return "  ".join(parts)


def build_demo_packets(segments: int) -> list[Packet]:
    """A miniature but realistic exchange: START, some DATA, then FIN."""
    start = pk.StartPayload(
        filename="smoketest.bin",
        filesize=segments * config.SEGMENT_SIZE,
        sha256="0" * 64,
        segment_size=config.SEGMENT_SIZE,
        mode=config.DEFAULT_MODE,
        run_id="smoketest",
    )
    packets = [Packet(type=PacketType.START, window=config.WINDOW_SIZE,
                      payload=start.to_payload())]

    for index in range(segments):
        seq = config.INITIAL_SEQUENCE + index
        # Recognizable payload so the bytes are easy to find in a capture.
        payload = f"segment {seq:04d} ".encode() + bytes(range(64))
        packets.append(Packet(type=PacketType.DATA, seq=seq,
                              window=config.WINDOW_SIZE, payload=payload))

    packets.append(Packet(type=PacketType.FIN,
                          seq=config.INITIAL_SEQUENCE + segments,
                          payload=pk.FinPayload(total_segments=segments).to_payload()))
    return packets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--segments", type=int, default=8)
    parser.add_argument("--delay", type=float, default=0.2,
                        help="seconds between packets, to keep a capture readable")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"Sending real {pk.HEADER_SIZE}-byte-header packets to "
          f"{args.host}:{args.port}  (filter: udp.port == {args.port})")

    try:
        for pkt in build_demo_packets(args.segments):
            raw = pk.encode(pkt)
            sock.sendto(raw, (args.host, args.port))
            print(f"Sent {pkt.type.name:<9} seq={pkt.seq:<4} {len(raw):>4}B  {annotate(raw)}")
            time.sleep(args.delay)
    finally:
        sock.close()


if __name__ == "__main__":
    main()
