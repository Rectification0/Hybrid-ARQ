import argparse, socket, time, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(line_buffering=True)  # one write per line, so the two processes never splice mid-line
from common.packet import make_data, parse, ACK
from common.channel import Channel, add_loss_args

HOST, PORT = "127.0.0.1", 5001
TOTAL, WINDOW, TIMEOUT = 10, 8, 1.0  # WINDOW = 8 matches the real project (D11)

parser = argparse.ArgumentParser()
add_loss_args(parser)
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(0.05)
packets = [make_data(i, f"DATA-{i}".encode()) for i in range(TOTAL)]
chan = Channel(sock, (HOST, PORT), "SR SENDER", args.drop, args.loss, args.seed)

base = next_seq = 0
acked = [False] * TOTAL
sent_at = [None] * TOTAL
print(f"[SR SENDER] Sending {TOTAL} packets, window={WINDOW}")

while base < TOTAL:
    while next_seq < min(base + WINDOW, TOTAL):
        chan.send(next_seq, packets[next_seq])
        # The timer starts even when the packet was dropped -- that is how it gets resent.
        sent_at[next_seq] = time.perf_counter()
        next_seq += 1
    try:
        while True:
            packet, _ = sock.recvfrom(4096)
            ptype, ack, _ = parse(packet)
            if ptype == ACK and 0 <= ack < TOTAL:
                acked[ack] = True
                while base < TOTAL and acked[base]:
                    base += 1
                print(f"[SR SENDER] ACK {ack} (individual); base={base}")
    except socket.timeout:
        pass
    except ConnectionResetError:
        pass  # Windows: ICMP port-unreachable surfaces here; normal, keep waiting
    except ValueError as e:
        print(f"[SR SENDER] Invalid ACK: {e}")

    now = time.perf_counter()
    for seq in range(base, next_seq):
        if not acked[seq] and sent_at[seq] is not None and now - sent_at[seq] >= TIMEOUT:
            print(f"[SR SENDER] TIMEOUT DATA {seq}")
            chan.send(seq, packets[seq], retx=True)
            sent_at[seq] = time.perf_counter()

print(f"[SR SENDER] Transfer complete: {chan.summary(TOTAL)}")
sock.close()
