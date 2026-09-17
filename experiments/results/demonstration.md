# Demonstration rehearsal (T10.6, specs.md §28)

Produced by `python experiments/demonstrate.py`, which performs the thirteen
steps of specs.md §28 in order and records what each one produced. The
condition, the seed and the schedule are fixed in the script, so this is a
rehearsal rather than a selection: whatever the run did is what appears below.

**Condition.** 1024 KiB, window 8, RTT 100 ms, RTO 0.4 s, seed 20260917. Loss 0% to start, 10% from 6 s, 0% from 18 s.

## The thirteen steps

| # | Step (specs.md §28) | What the rehearsal produced |
| --- | --- | --- |
| 1 | Start receiver | started by the harness immediately before each sender, and its bound port read back before the sender is launched |
| 2 | Start Wireshark capture | tshark on \Device\NPF_Loopback, filter `udp port 8888`, snaplen 128 B so every header is kept |
| 3 | Start a hybrid transfer | 1024 KiB, window 8, RTT 100 ms, RTO 0.4 s, seed 20260917 |
| 4 | Show GBN under clean conditions | the transfer opens in GBN (config.DEFAULT_MODE) and stays there for the clean phase |
| 5 | Introduce sustained loss | loss schedule step to 10% at 6 s; the log records LOSS_CHANGE at 6.12 s |
| 6 | Show the controller detecting it | loss estimate reaches 0.74 at the moment of the switch, against SWITCH_HIGH = 0.1 |
| 7 | Show the transition to SR | SWITCH to sr at 11.80 s, 5.67 s after the change, reason MODE_THRESHOLD;from=gbn;epoch=1 |
| 8 | Show selective retransmission in Wireshark | 88 RETX rows in the log; in the capture, filter `hybridarq.type == 1 && hybridarq.seq == <n>` to see one segment resent alone rather than a whole range |
| 9 | Restore low loss | schedule step back to 0% at 18 s; LOSS_CHANGE recorded at 18.00 s |
| 10 | Show SR -> GBN after hysteresis | SWITCH back to gbn at 19.75 s, 1.75 s after the restore, with HYSTERESIS_COUNT = 3 confirmations |
| 11 | Stop capture | demonstration.pcapng written (263 KiB) |
| 12 | Verify hashes | receiver's SHA-256 matches the source: OK (1048576 bytes delivered) |
| 13 | Compare with pure GBN and pure SR | GBN: 25.6 s, 168 retx; SR: 23.8 s, 33 retx; hybrid: 24.9 s, 88 retx |

## Step 13 in full — the same condition, seed and file for all three

| System | Completion | Goodput | Retransmissions | Overhead | Switches | Integrity |
| --- | --- | --- | --- | --- | --- | --- |
| pure GBN | 25.6 s | 40.0 KiB/s | 168 | 0.141 | 0 | OK |
| pure SR | 23.8 s | 43.1 KiB/s | 33 | 0.031 | 0 | OK |
| hybrid | 24.9 s | 41.2 KiB/s | 88 | 0.079 | 2 | OK |

## What the rehearsal shows, and what it does not

The hybrid detected the change, switched, and switched back, and the file
arrived intact. On completion time it is 1.03x pure GBN and
0.96x pure SR on this condition, with 88 retransmissions against GBN's 168 and SR's 33.

**The margin here is small, and the reason is worth saying out loud in the
demonstration rather than leaving for someone to notice.** This condition is
lossy for only 12 s of a ~25 s transfer, so GBN spends most of the run in
the regime it is good at and its penalty is diluted; the hybrid additionally pays
two window drains that neither baseline pays. The large separations are in the
sustained-loss cells of the matrix, not here: at 10% and 20% loss the hybrid runs
at 1.56x and 2.00x pure GBN (`experiments.md` §2).

What this rehearsal is evidence *for* is the mechanism: that the controller
detects a change it was not told about, negotiates a mode change mid-transfer
without losing a segment, comes back when the condition reverts, and delivers a
file whose hash matches. The performance claim belongs to the matrix.

The capture is `demonstration.pcapng`.

```bash
tshark -r captures/demonstration.pcapng -X lua_script:tools/hybrid_arq.lua \
       -Y 'hybridarq.type == 7' -T fields -e frame.time_relative -e hybridarq.control
```

prints the MODE handshakes — the transition as packets rather than as a log line.
