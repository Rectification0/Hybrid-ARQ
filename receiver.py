"""M1 CONNECTIVITY SPIKE — SUPERSEDED. Not the real receiver.

This is the throwaway UDP listener from milestone M1 (T0.1): it proved Python
UDP sockets work and that packets are visible in Wireshark on udp.port == 8888.
It understands no packet format, performs no validation, and writes no file.

The real receiver replaces this file at this path in T2.2: CLI, packet
validation, in-order write, hash comparison, receiver state machine
(design.md §7.2). Until then, nothing should import or build on this module.

Its counterpart, test_udp.py, is kept deliberately as the environment smoke test.
"""

import socket

SERVER_IP = "127.0.0.1"
SERVER_PORT = 8888

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((SERVER_IP, SERVER_PORT))

print(f"Listening on {SERVER_IP}:{SERVER_PORT}")

while True:
    data, address = sock.recvfrom(1024)
    print(f"Received: {data.decode()}")
