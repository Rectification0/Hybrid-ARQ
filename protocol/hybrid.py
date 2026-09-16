"""Hybrid controller: condition monitoring and mode selection.

NOT YET IMPLEMENTED — scaffold only. Implemented by T5.1-T5.6.

Do not start this module until T3.7 and T4.8 pass for both baselines. The
controller must never be debugged at the same time as the mechanism underneath
it (source specification §41, tasks.md ordering rule).

Owns: the statistics collector (specs.md §10), the frozen loss estimator (D8),
the dual-threshold rule with hysteresis (D9), and the MODE handshake (D10).
Knows nothing about byte layout or file I/O (design.md §1.1).
"""
