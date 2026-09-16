"""Binary packet format: constants, serialization, parsing, checksum.

NOT YET IMPLEMENTED — scaffold only. Implemented by T1.1-T1.5.

Before any code is written here, three decisions must be frozen and recorded in
specs.md §16 and design.md §12:

    D1  field order, sizes, endianness, packing   (T1.1)
    D2  checksum algorithm and coverage           (T1.2)
    D3/D4  SEGMENT_SIZE and initial sequence      (T1.3)

When frozen, the ``struct`` format string and the byte-offset table belong in
this docstring — the checksum coverage in particular cannot be inferred from the
wire bytes, so it must be written down here (design.md §3.1).

Both endpoints import this module, and only this module, for the wire format, so
the two sides can never drift apart (design.md §1).
"""
