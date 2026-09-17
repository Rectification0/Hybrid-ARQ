# Wireshark evidence set (T9.4, specs.md §22.1)

Recorded by `python experiments/capture_evidence.py`, which runs each transfer
through the same orchestration the experiments used and captures it on the npcap
loopback adapter. Every scenario carries a fixed seed, so a capture can be
retaken and compared rather than taken on trust (RP-03).

Display filter for all of them: `udp.port == 8888` (WS-02, WS-03).

The header is a fixed 21-byte prefix, so sequence and ACK are readable at
constant offsets without any plugin (WS-05). `tools/hybrid_arq.lua` (T9.5) names
the fields so they can be *filtered* instead:

```bash
tshark -r captures/lossy_gbn_range_retx.pcapng -X lua_script:tools/hybrid_arq.lua \
       -T fields -e frame.number -e hybridarq.type_name -e hybridarq.seq -e hybridarq.ack

# every transmission of one segment - the retransmission pattern, as a query
tshark -r captures/lossy_gbn_range_retx.pcapng -X lua_script:tools/hybrid_arq.lua \
       -Y 'hybridarq.type == 1 && hybridarq.seq == 20'

# the MODE handshake, with its JSON payload
tshark -r captures/hybrid_gbn_to_sr.pcapng -X lua_script:tools/hybrid_arq.lua \
       -Y 'hybridarq.type == 7' -T fields -e hybridarq.control
```

`resent on the wire` below counts DATA packets carrying a sequence that had
already appeared in that capture — retransmission as the *capture* shows it,
independent of what the sender's log claims.

**It is not the same number as the sender's retransmission count, and the gap is
the point of the comparison.** A segment that was dropped never reached the wire,
so resending it puts no duplicate sequence in the capture. A duplicate appears
only when a segment that *did* arrive is sent again — which is precisely what
Go-Back-N does to everything behind a loss, and precisely what Selective Repeat
does not do at all. In the two lossy captures below, taken from the same file and
the same seeded drop stream, **GBN puts 62 redundant copies on the wire and SR
puts none**.

The two runs do not see an identical *list* of drops, and the reason is worth
stating: the stream is consumed per datagram sent, so the first drops match
(segments 3, 8, 3 again) and then diverge — GBN, resending whole ranges, offers
far more datagrams to the same 10% and collects 18 drops where SR collects 10.
The condition is identical; the exposure to it is a consequence of the strategy,
which is the thing being measured.

## `clean_gbn.pcapng`

**Expected observation (WS-01, WS-02, WS-03, WS-04, WS-05):** Sequential DATA and cumulative ACKs, no retransmissions

The control: what the protocol looks like when nothing goes wrong.

- run: `gbn`, 64 KiB, loss 0%, RTT 0 ms, seed 20260917
- capture: 132 packets (ACK 64, DATA 64, FIN 1, FIN_ACK 1, START 1, START_ACK 1), 64 unique DATA sequences, **0 resent on the wire**
- transfer: ok, integrity OK, 0 mode switch(es), 0 retransmissions in the sender's log

## `lossy_gbn_range_retx.pcapng`

**Expected observation (WS-06):** One drop resends the whole outstanding range

Same file and the same derived seed as the SR capture below: one drop stream, two strategies.

- run: `gbn`, 64 KiB, loss 10%, RTT 0 ms, seed 20260917
- capture: 256 packets (ACK 126, DATA 126, FIN 1, FIN_ACK 1, START 1, START_ACK 1), 64 unique DATA sequences, **62 resent on the wire**
- transfer: ok, integrity OK, 0 mode switch(es), 80 retransmissions in the sender's log

## `lossy_sr_individual_retx.pcapng`

**Expected observation (WS-07):** Only the missing segment is resent; ACKs name one segment each

Same file and the same derived seed as the GBN capture above.

- run: `sr`, 64 KiB, loss 10%, RTT 0 ms, seed 20260917
- capture: 132 packets (ACK 64, DATA 64, FIN 1, FIN_ACK 1, START 1, START_ACK 1), 64 unique DATA sequences, **0 resent on the wire**
- transfer: ok, integrity OK, 0 mode switch(es), 10 retransmissions in the sender's log

## `hybrid_gbn_to_sr.pcapng`

**Expected observation (WS-08):** MODE exchange, then per-segment retransmission where the range retransmission of GBN was

E8's loss_rise condition: clean, then 10% loss from 8 s.

- run: `hybrid`, 1024 KiB, loss 0%, RTT 100 ms, loss schedule 8s->0.1, seed 20260917
- capture: 2102 packets (ACK 1048, DATA 1048, FIN 1, FIN_ACK 1, MODE 2, START 1, START_ACK 1), 1024 unique DATA sequences, **24 resent on the wire**, snaplen 128 B
- transfer: ok, integrity OK, 1 mode switch(es), 83 retransmissions in the sender's log

## `hybrid_sr_to_gbn.pcapng`

**Expected observation (WS-09):** A second MODE exchange returning to cumulative ACKs once loss falls away

E8's loss_fall condition: 10% loss, then clean from 20 s. Both transitions appear in this one file.

- run: `hybrid`, 1024 KiB, loss 10%, RTT 100 ms, loss schedule 20s->0, seed 20260917
- capture: 2178 packets (ACK 1085, DATA 1085, FIN 1, FIN_ACK 1, MODE 4, START 1, START_ACK 1), 1024 unique DATA sequences, **61 resent on the wire**, snaplen 128 B
- transfer: ok, integrity OK, 2 mode switch(es), 121 retransmissions in the sender's log
