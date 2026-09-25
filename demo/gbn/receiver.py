import socket, io, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if isinstance(sys.stdout, io.TextIOWrapper):  # always true at runtime; narrows TextIO for the type checker
    sys.stdout.reconfigure(line_buffering=True)  # one write per line, so the two processes never splice mid-line
from common.packet import DATA, ChecksumError, make_ack, parse

HOST, PORT = "127.0.0.1", 5000

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((HOST, PORT))
expected = 0
print(f"[GBN RECEIVER] Listening on {HOST}:{PORT}")

while True:
    try:
        packet, addr = sock.recvfrom(4096)
    except ConnectionResetError:
        continue  # Windows: ICMP port-unreachable surfaces here; normal, keep waiting
    try:
        ptype, seq, payload = parse(packet)
    except ChecksumError as e:
        print(f"[GBN RECEIVER] CHECKSUM_FAIL: {e}")
        continue
    except ValueError as e:
        print(f"[GBN RECEIVER] MALFORMED: {e}")
        continue
    if ptype != DATA:
        continue

    if seq == expected:
        print(f"[GBN RECEIVER] DELIVER DATA {seq}")
        sock.sendto(make_ack(seq), addr)
        print(f"[GBN RECEIVER] ACK {seq} sent")
        expected += 1
    elif seq < expected:
        last = expected - 1
        print(f"[GBN RECEIVER] DATA {seq} duplicate -> ACK {last}")
        sock.sendto(make_ack(last), addr)
    else:
        last = expected - 1
        print(f"[GBN RECEIVER] DATA {seq} out of order -> discarded; ACK {last}")
        if last >= 0:
            sock.sendto(make_ack(last), addr)
