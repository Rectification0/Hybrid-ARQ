"""Environment smoke test (M1, T0.1) — retained deliberately.

Sends ten plain TEST_PACKET_n datagrams to confirm the Python environment, the
UDP path, and Wireshark packet visibility on udp.port == 8888. It is not a
protocol test and asserts nothing about the packet format.

T1.7 replaces these placeholder strings with real encoded packets, at which
point the same capture must show a DATA header readable at fixed offsets.
Run alongside the M1 spike receiver.py, not the real receiver.
"""

import socket
import time

SERVER_IP = "127.0.0.1"
SERVER_PORT = 8888

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

for i in range(10):
    message = f"TEST_PACKET_{i}"
    sock.sendto(message.encode(), (SERVER_IP, SERVER_PORT))
    print(f"Sent: {message}")
    time.sleep(0.5)

sock.close()
