import argparse, socket, time, io, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if isinstance(sys.stdout, io.TextIOWrapper):  # always true at runtime; narrows TextIO for the type checker
    sys.stdout.reconfigure(line_buffering=True)  # one write per line, so the two processes never splice mid-line
from common.packet import make_data, parse, ACK
from common.channel import Channel, add_loss_args

HOST, PORT = "127.0.0.1", 5000
TOTAL, WINDOW, TIMEOUT = 10, 8, 1.0  # WINDOW = 8 matches the real project (D11)

parser = argparse.ArgumentParser()
add_loss_args(parser)
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(0.10)
packets = [make_data(i, f"DATA-{i}".encode()) for i in range(TOTAL)]
chan = Channel(sock, (HOST, PORT), "GBN SENDER", args.drop, args.loss, args.seed)

base = next_seq = 0
timer = None
print(f"[GBN SENDER] Sending {TOTAL} packets, window={WINDOW}")

while base < TOTAL:
    while next_seq < min(base + WINDOW, TOTAL):
        chan.send(next_seq, packets[next_seq])
        if base == next_seq:
            timer = time.perf_counter()
        next_seq += 1

    try:
        packet, _ = sock.recvfrom(4096)
        ptype, ack, _ = parse(packet)
        if ptype == ACK and ack >= base:
            base = ack + 1
            print(f"[GBN SENDER] ACK {ack} (cumulative); base={base}")
            timer = None if base == next_seq else time.perf_counter()
    except socket.timeout:
        pass
    except ConnectionResetError:
        pass  # Windows: ICMP port-unreachable surfaces here; normal, keep waiting
    except ValueError as e:
        print(f"[GBN SENDER] Invalid ACK: {e}")

    if timer is not None and base < next_seq and time.perf_counter() - timer >= TIMEOUT:
        print(f"[GBN SENDER] TIMEOUT DATA {base} -> RETX {base}..{next_seq-1}")
        for seq in range(base, next_seq):
            chan.send(seq, packets[seq], retx=True)
        timer = time.perf_counter()

print(f"[GBN SENDER] Transfer complete: {chan.summary(TOTAL)}")
sock.close()
