"""Sender endpoint: CLI, file reading, transfer lifecycle, orchestration.

NOT YET IMPLEMENTED — scaffold only. Implemented by T2.1.

Planned CLI: --file --host --port --mode --window
Owns the transfer state machine (design.md §7.1) and, in hybrid mode, all
switching decisions — the policy lives only on the sender; the receiver is told
which semantics to use via the MODE handshake (design.md §6.3).
"""
