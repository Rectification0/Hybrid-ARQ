"""M1 spike listener — a smoke-test tool, NOT the real receiver.

This is the throwaway UDP listener from milestone M1 (T0.1): it proved Python
UDP sockets work and that packets are visible in Wireshark on udp.port == 8888.
Per T1.7 it no longer prints raw strings — it decodes the frozen packet format
and prints the parsed header, so the terminal output can be compared directly
against what Wireshark shows at the same offsets.

It is still a spike: no windows, no ACKs, no retransmission, no file writing,
no state machine. The real receiver is written in T2.2 at the project root
(``receiver.py``) and owns all of that. Nothing should import this module.

    python test/spike_receiver.py      # terminal 1
    python test_udp.py                 # terminal 2
"""

import argparse
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                   # noqa: E402
from protocol import packet as pk               # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--idle-timeout", type=float, default=0.0,
                        help="exit after this many idle seconds (0 = listen forever)")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))
    if args.idle_timeout > 0:
        sock.settimeout(args.idle_timeout)

    print(f"Listening on {args.host}:{args.port} (spike listener — not the real receiver)")

    received = 0
    rejected = 0
    try:
        while True:
            try:
                raw, address = sock.recvfrom(pk.MAX_DATAGRAM_SIZE)
            except socket.timeout:
                break

            try:
                pkt = pk.decode(raw)
            except pk.PacketError as exc:
                # Exactly how the real receiver will behave: discard and log the
                # reason, so integrity failures stay distinguishable from loss.
                rejected += 1
                print(f"REJECT {type(exc).__name__}: {exc}  ({len(raw)}B from {address[0]})")
                continue

            received += 1
            detail = ""
            payload_class = pk.CONTROL_PAYLOADS.get(pkt.type)
            if payload_class is not None:
                try:
                    detail = f"  {payload_class.from_payload(pkt.payload)}"
                except pk.ControlPayloadError as exc:
                    detail = f"  <bad control payload: {exc}>"

            print(f"{pkt.type.name:<9} seq={pkt.seq:<4} ack={pkt.ack:<4} "
                  f"win={pkt.window:<4} payload={len(pkt.payload):>4}B{detail}")
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        print(f"\n{received} accepted, {rejected} rejected")


if __name__ == "__main__":
    main()
