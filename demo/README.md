# Teaching demo: Go-Back-N and Selective Repeat in ~250 lines

A minimal GBN / SR pair over UDP that shows the ARQ mechanism in the terminal and in
Wireshark. It began as the Assessment 7 sample (`sampleprotocols/`) and was lightly
modified to match this project's constants and log vocabulary. The change list is in
`demo_plan.md` at the repo root.

**This is not the system under evaluation.** The real implementation is `protocol/`,
`sender.py` and `receiver.py`. Its results are in `RESULTS.md`, and nothing here feeds
them. The demo imports nothing from the rest of the repo, and nothing imports the demo.

Standard library only. Python 3.9+.

## Run

From the repo root:

```bash
python demo/run_gbn.py                        # clean transfer, 10 segments
python demo/run_gbn.py --drop 2               # drop the first transmission of DATA 2
python demo/run_sr.py  --drop 2
python demo/run_gbn.py --loss 0.2 --seed 2    # seeded random loss
python demo/run_sr.py  --loss 0.2 --seed 2
```

Each `run_*.py` starts the receiver, runs the sender, then stops the receiver, and both
print to the same terminal. To run them in two terminals instead, start
`python demo/gbn/receiver.py` first, then `python demo/gbn/sender.py --drop 2` (same for
`sr/`).

| Flag | Meaning |
| --- | --- |
| `--drop N` | Drop the **first** transmission of DATA N. Its retransmission goes through. |
| `--loss P` | Drop each DATA transmission, first send or RETX, with probability P, in `[0, 1)`. |
| `--seed S` | Seed for `--loss` (default 1). The same seed gives the same drop decisions. |

Loss is applied by the sender **before** `sendto()`, to DATA only. ACKs are never dropped.

## What to look for

| | GBN (port 5000) | SR (port 5001) |
| --- | --- | --- |
| ACK meaning | cumulative: "everything up to N" | individual: "exactly N" |
| Out-of-order DATA | discarded, last in-order ACK repeated | buffered, ACKed individually |
| On timeout | resend the whole window (`RETX 2..9`) | resend only the segment that timed out |

With `--drop 2`, the last line of each run shows the difference as numbers:

```
[GBN SENDER] Transfer complete: 10 segments, 10 SEND, 8 RETX, 1 DROP, 17 DATA on the wire, 1.06 s
[SR SENDER]  Transfer complete: 10 segments, 10 SEND, 1 RETX, 1 DROP, 10 DATA on the wire, 1.06 s
```

With `--loss 0.2 --seed 2`, the gap grows. In a sample run GBN sent 21 RETX (25 DATA on
the wire, 4.25 s) and SR sent 3 RETX (10 on the wire, 2.08 s). That cost of GBN under
loss is what the real project's hybrid controller switches on. Some seeds happen to drop
only the last segments, where the two behave the same (e.g. `--loss 0.1 --seed 1`).

The drop decisions come from the sender's own seeded RNG, so a given seed repeats the
same pattern on loopback. The printed times vary by a few milliseconds.

### Log vocabulary

The terminal uses the same event names as the real project's `events.csv`:

| Line | Meaning |
| --- | --- |
| `SEND DATA n` | first transmission, put on the wire |
| `RETX DATA n` | retransmission, put on the wire |
| `DROP DATA n (simulated)` | the loss simulator ate it. Nothing was sent. `(simulated, RETX)` if it was a retransmission. |
| `TIMEOUT DATA n` | the retransmission timer for n expired |
| `ACK n (cumulative)` / `ACK n (individual)` | sender received an ACK. `base=` is the new window base. |
| `DELIVER DATA n` | receiver handed n to the application in order |
| `CHECKSUM_FAIL` / `MALFORMED` | receiver rejected a datagram. Kept distinct from `DROP`, which means simulated loss. |

The summary counts `SEND` and `RETX` as attempts. `DATA on the wire` = SEND + RETX −
DROP, which is the number of DATA frames Wireshark will show.

## Wireshark

Wireshark doesn't run the protocol. It independently shows the datagrams that actually
crossed the network.

1. Capture on **Adapter for loopback traffic capture** (npcap loopback adapter, Windows).
   Start the capture *before* running the demo.
2. Filter: `udp.port == 5000` (GBN) or `udp.port == 5001` (SR).
3. DATA frames are 51 bytes (13-byte header + 6-byte payload `DATA-n` + 32 bytes of
   loopback/IP/UDP headers). ACKs are 45 bytes.

From the command line:

```bash
"/c/Program Files/Wireshark/tshark" -i '\Device\NPF_Loopback' -f "udp port 5000" \
    -a duration:10 -w captures/demo_gbn.pcapng
```

**Terminal vs Wireshark.** The terminal explains *why* something happened: dropped,
discarded, buffered. Wireshark shows only what was sent. A dropped packet never reaches
the wire, so in Wireshark it is a **gap** (DATA 0, 1, 3, 4, … with no 2) until its
retransmission appears. The two views agree on order and sequence numbers. They don't
share timestamps (the terminal prints none).

### Dissector: packet names instead of `Len=19`

Without help, Wireshark lists every demo packet as plain UDP. `wireshark/arq1.lua` decodes
the demo format:

| Column | Without | With |
| --- | --- | --- |
| Protocol | `UDP` | `ARQ1` |
| Info | `60817 → 5000 Len=19` | `DATA seq=2 len=6` / `ACK seq=4` |

Load it for one session:

```bash
"/c/Program Files/Wireshark/Wireshark.exe" -X lua_script:demo/wireshark/arq1.lua
"/c/Program Files/Wireshark/tshark" -X lua_script:demo/wireshark/arq1.lua \
    -r captures/demo_gbn.pcapng -Y arq1
```

To load it every time instead, copy it into the folder shown under **Help → About
Wireshark → Folders → Personal Lua Plugins** and press **Ctrl+Shift+L** to reload.

Useful display filters once it is loaded:

| Filter | Shows |
| --- | --- |
| `arq1` | demo packets only |
| `arq1.type == 1 && arq1.seq == 2` | every transmission of DATA 2 |
| `arq1.type == 2` | ACKs only. GBN's repeated `ACK seq=1` is easy to spot here. |
| `_ws.expert` | packets whose checksum or length is wrong |

The dissector checks each checksum with the same algorithm as `common/packet.py`. A
corrupted packet gets a *Checksum mismatch* warning, so it looks different from a dropped
one, which leaves no frame at all.

It registers on UDP 5000 and 5001 only and ignores anything that doesn't start with
`ARQ1`. The real protocol (magic `HA`, 21-byte header, port 8888) isn't decoded.

## Packet format

The demo format, kept as it was in Assessment 7:

```
"!4sBIHH"  big-endian, 13 bytes
magic "ARQ1" (4) | type 1=DATA 2=ACK (1) | seq (4) | checksum (2) | payload length (2) | payload
```

The checksum is a 16-bit ones'-complement sum over magic, type, seq, a zeroed checksum
field and the payload.

## How it relates to the real project

| Idea | Demo | Real implementation |
| --- | --- | --- |
| Packet encode / decode | `common/packet.py` | `protocol/packet.py` (frozen D1 / D2 layout) |
| GBN sender and receiver rules | `gbn/sender.py`, `gbn/receiver.py` | `protocol/gbn.py` (D5: ACK = highest in-order) |
| SR sender and receiver rules | `sr/sender.py`, `sr/receiver.py` | `protocol/sr.py` (D6: ACK = exactly the segment named) |
| Simulated loss | `common/channel.py` | `network/simulator.py` (`Impairment.wrap()`, a socket wrapper seeded per direction) |
| Window | `WINDOW = 8` | `config.WINDOW_SIZE = 8` (D11) |
| Timeout | fixed 1.0 s | RTO = max(4 × RTT, 200 ms) per condition (D7) |
| Switching between GBN and SR | none | `protocol/hybrid.py` + the MODE handshake (`design.md` §6.3) |
| Output | terminal lines | `events.csv` + `summary.json`, from which every metric is derived |

The demo sends 10 fixed packets (`DATA-0` … `DATA-9`). The real system transfers a file,
verifies it end to end, and logs every event.
