import socket, io, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if isinstance(sys.stdout, io.TextIOWrapper):  # always true at runtime; narrows TextIO for the type checker
    sys.stdout.reconfigure(line_buffering=True)  # one write per line, so the two processes never splice mid-line
from common.packet import DATA, ChecksumError, make_ack, parse

HOST, PORT = "127.0.0.1", 5001
WINDOW, TOTAL = 8, 10  # must match sr/sender.py

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((HOST, PORT))
base = 0
buffer = {}
print(f"[SR RECEIVER] Listening on {HOST}:{PORT}")

while base < TOTAL:
    try:
        packet, addr = sock.recvfrom(4096)
    except ConnectionResetError:
        continue  # Windows: ICMP port-unreachable surfaces here; normal, keep waiting
    try:
        ptype, seq, payload = parse(packet)
    except ChecksumError as e:
        print(f"[SR RECEIVER] CHECKSUM_FAIL: {e}")
        continue
    except ValueError as e:
        print(f"[SR RECEIVER] MALFORMED: {e}")
        continue
    if ptype != DATA:
        continue

    if seq < base:
        print(f"[SR RECEIVER] DATA {seq} duplicate -> ACK {seq}")
        sock.sendto(make_ack(seq), addr)
        continue

    if base <= seq < base + WINDOW:
        if seq not in buffer:
            buffer[seq] = payload
            print(f"[SR RECEIVER] DATA {seq} received and buffered")
        sock.sendto(make_ack(seq), addr)
        print(f"[SR RECEIVER] ACK {seq} sent")
        while base in buffer:
            buffer.pop(base)
            print(f"[SR RECEIVER] DELIVER DATA {base}")
            base += 1
    else:
        print(f"[SR RECEIVER] DATA {seq} outside receive window -> ignored")

print("[SR RECEIVER] Transfer complete")
sock.close()
