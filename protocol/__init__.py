"""Protocol layer for the hybrid GBN/SR ARQ implementation.

Module map (design.md §2, §1.1 layering rule):

    packet.py   wire format: constants, serialization, parsing, checksum  (T1.*)
    gbn.py      Go-Back-N sender/receiver state and retransmission        (T3.*)
    sr.py       Selective Repeat state, buffering, single retransmission  (T4.*)
    hybrid.py   condition monitoring and mode selection                   (T5.*)

Layering rule: nothing in this package reads files, parses CLI arguments, or
owns transfer lifecycle — that belongs to sender.py / receiver.py.
"""
