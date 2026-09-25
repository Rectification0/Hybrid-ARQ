# Demo plan: bringing `sampleprotocols` into Hybrid-ARQ

Status: **approved and implemented** in `demo/`. §8 records the decisions, and §9 records
where the build differs from the proposal.

## 1. What this is

`C:\Users\Ishaan Gupta\sampleprotocols` is the small GBN/SR demo shown in Assessment 7
(`Assessment7_25BDS0157ReReRe.pdf`): 10 hard-coded packets, one intentional drop via
`--drop N`, console output and a Wireshark view. It is not under git.

The goal is to keep it as a **teaching demo** inside this repo, lightly modified so its
vocabulary and constants match the real project. It shows the ARQ mechanism in
about 250 lines, and the real implementation (`protocol/`, `sender.py`, `receiver.py`)
remains what produces the recorded evidence.

**No new GitHub repo is needed.** It becomes a folder in this one.

## 2. Where it goes

```
Hybrid-ARQ/
  demo/
    README.md              # merged from sampleprotocols/README.md + wireshark_guide.md
    common/__init__.py
    common/packet.py
    common/channel.py      # new: --drop / --loss / --seed and the summary counters
    gbn/__init__.py
    gbn/sender.py
    gbn/receiver.py
    sr/__init__.py
    sr/sender.py
    sr/receiver.py
    run_gbn.py
    run_sr.py
    wireshark/arq1.lua     # new: optional dissector, see §5.7
```

`__pycache__/` is not copied, and the sample's own `.gitignore` isn't needed because the
repo already ignores it.

## 3. Isolation rules

These keep the demo from blurring into the evaluated system:

| Rule | Why |
| --- | --- |
| `demo/` imports nothing from `protocol/`, `network/`, `config.py`, etc. | It stays a self-contained, readable snippet; changing a frozen value in `config.py` can't silently change the demo. |
| Nothing in the repo imports `demo/`. | Same one-way rule as `frontend/`. |
| Demo output is never used as evidence (no `logs/`, `RESULTS.md`, graphs). | Its wire format (`ARQ1`, 13-byte header) is **not** the frozen D1 format (`HA`, 21-byte header). |
| Ports stay 5000 (GBN) / 5001 (SR), never 8888. | Wireshark filters for demo and real protocol never overlap. |
| Not added to `tasks.md` phases. | It's outside the spec's scope (`specs.md` §30 positioning is unaffected). |

`pytest.ini` already pins test collection to `test/`, so the demo won't be picked up by
the test suite.

## 4. What stays the same

- Packet layout in `common/packet.py`: `MAGIC = b"ARQ1"`, header `"!4sBIHH"`, the 16-bit
  ones'-complement checksum, `make_data` / `make_ack` / `parse`. This is what the PDF's
  Wireshark screenshots show (51-byte DATA frames, 45-byte ACK frames), so changing it
  would make the PDF and the demo disagree.
- GBN semantics: cumulative ACK, out-of-order discarded, whole window retransmitted on
  timeout.
- SR semantics: individual ACK, out-of-order buffered, only the timed-out segment
  retransmitted.
- `--drop N` still works exactly as before.
- `run_gbn.py` / `run_sr.py` still start the receiver, run the sender, then terminate the
  receiver.

## 5. Proposed modifications

### 5.1 Window 8 instead of 4 (aligns with D11)

```python
# before (gbn/sender.py, sr/sender.py, sr/receiver.py)
TOTAL, WINDOW, TIMEOUT = 10, 4, 1.0
# after
TOTAL, WINDOW, TIMEOUT = 10, 8, 1.0
```

`TOTAL` stays 10 (decision §8.2), so every DATA frame is 51 bytes, as in the PDF. With
`--drop 2`, GBN's timeout now resends DATA 2..9, the whole rest of the window.

### 5.2 `time.perf_counter()` instead of `time.monotonic()`

This affects every timer in both senders. The project bans `monotonic` because on Windows
it is `GetTickCount64` with 15.6 ms resolution (see `CLAUDE.md`, "Logging is the
product"). The demo's 1 s timeout isn't really affected, but keeping the same rule means
nobody copying from the demo brings the banned call back.

### 5.3 Log labels use the `events.csv` vocabulary

The console lines keep their `[GBN SENDER]` style prefix, but the verb matches the real
event names, so a viewer can compare demo output with a real `events.csv` directly:

| Current text | New text |
| --- | --- |
| `DATA 3 sent` | `SEND DATA 3` |
| `RETRANSMIT DATA 3` | `RETX DATA 3` |
| `*** INTENTIONAL DROP DATA 2 ***` | `DROP DATA 2 (simulated)` |
| `TIMEOUT DATA 2 -> retransmitting 2..5` | `TIMEOUT DATA 2 -> RETX 2..9` |
| `DATA 2 received -> delivered` (GBN) | `DELIVER DATA 2` |
| `cumulative ACK 4 received; base=5` | `ACK 4 (cumulative); base=5` |
| `individual ACK 4 received` + `window base=2` | `ACK 4 (individual); base=2` (one line) |
| `DATA 3 out of order -> discarded; ACK 1` | `DATA 3 out of order -> discarded; ACK 1` (unchanged) |
| `DATA 2 delivered` | `DELIVER DATA 2` |
| `Invalid packet: Checksum mismatch` | `CHECKSUM_FAIL: Checksum mismatch` (other parse errors: `MALFORMED: ...`) |

`SEND` vs `RETX` being distinct and `DROP` vs `CHECKSUM_FAIL` being distinct mirrors
CC-06 / IN-04 in the real project.

### 5.4 Optional seeded random loss

Add two flags to both senders, alongside `--drop`:

```
--loss P      drop each first transmission with probability P (default 0 = off)
--seed S      RNG seed for --loss (default 1), so a run is repeatable
```

- It uses its own `random.Random(seed)`, so the drop sequence depends only on the seed
  (same idea as RP-03).
- It applies to first transmissions **and** retransmissions, using a probability draw
  rather than a fixed counter. The project's gotcha list warns that loss phase-locked
  to retransmissions produces artifacts.
- `--drop N` keeps its current meaning and can be combined with `--loss`.

This is the one change that adds behaviour. It is what makes the GBN-vs-SR contrast
visible at the loss rates the real evaluation cares about. Running both demos with
`--loss 0.2 --seed 2` gives many more `RETX` lines for GBN than for SR, which is the whole
reason the hybrid exists.

### 5.5 Print a one-line summary at the end

Each sender's last line becomes (numbers illustrative):

```
[GBN SENDER] Transfer complete: 10 segments, 10 SEND, 8 RETX, 1 DROP, 17 DATA on the wire, 1.06 s
```

These are the same counters `metrics.py` derives for the real system, so a demo audience
can compare GBN and SR as numbers, not by scrolling.

### 5.6 `demo/README.md`

Merge the sample's `README.md` and `wireshark_guide.md` into one file, with these updates:

- run commands from the repo root (`python demo/run_gbn.py --drop 2`)
- the new flags
- a short "How this relates to the real project" section: which file in `protocol/`
  implements the same idea, and why the wire formats differ
- the Windows loopback capture note from the top-level `CLAUDE.md` (npcap loopback
  adapter), with filters `udp.port == 5000` / `udp.port == 5001`
- what appears where: the terminal explains *why* (dropped, discarded, buffered), and
  Wireshark shows only what was actually sent, so a dropped packet is a gap in Wireshark
- loading the dissector (§5.7) and the `arq1.*` display filters

### 5.7 Wireshark dissector for the demo format (optional to load)

Without help, Wireshark lists every demo packet as plain `UDP ... Len=19`, and you have to
read the sequence number out of the hex pane. `demo/wireshark/arq1.lua` (about 40 lines)
teaches Wireshark the `ARQ1` layout so the packet list reads like the terminal:

| Column | Without dissector | With dissector |
| --- | --- | --- |
| Protocol | `UDP` | `ARQ1` |
| Info | `59790 → 5000 Len=19` | `DATA seq=2 len=6` / `ACK seq=4` |

What it does:

- Registers on UDP ports **5000 and 5001 only**, so it never touches the real protocol
  on 8888 or any other traffic.
- Decodes the header `"!4sBIHH"` into named fields: `arq1.magic`, `arq1.type`
  (1 = DATA, 2 = ACK), `arq1.seq`, `arq1.checksum`, `arq1.len`, plus the payload.
- Checks the checksum the same way `common/packet.py` does, and marks a mismatch as a
  Wireshark expert warning, so a corrupted packet looks different from a dropped one.
- Ignores packets that don't start with `ARQ1`, so it can't mislabel stray traffic.
- The new fields work as display filters:
  - `arq1.type == 1 && arq1.seq == 2` shows every transmission of DATA 2, which is a
    one-line way to see "GBN resent it, and so did SR".
  - `arq1.type == 2` shows ACKs only, where GBN's repeated cumulative ACKs are easy to
    see.

How to load it (no install step that changes Wireshark permanently):

```bash
# one-off, from the repo root
"/c/Program Files/Wireshark/Wireshark.exe" -X lua_script:demo/wireshark/arq1.lua
"/c/Program Files/Wireshark/tshark" -X lua_script:demo/wireshark/arq1.lua \
    -r captures/demo_gbn.pcapng -Y arq1
```

To load it every time instead, copy it into Wireshark's personal Lua plugins folder
(**Help → About Wireshark → Folders → Personal Lua Plugins**) and press
**Ctrl+Shift+L** to reload.

Limits, stated in the README:

- It decodes only the demo format. The real protocol (`HA` magic, 21-byte header, port
  8888) is not covered, and adding that would be a separate decision.
- A packet dropped by `--drop` / `--loss` still never appears. The dissector labels what
  was sent; it can't show what wasn't.

## 6. Out of scope (unless you ask)

- **`run_hybrid.py`** (GBN that drains its window and switches to SR mid-run). It would be
  a real addition rather than a small change, and a simplified switch risks contradicting
  the careful handshake in `design.md` §6.3. If added later, it should be labelled as a
  sketch, not as how the real hybrid works.
- Real file transfer, CLI port options, or the `events.csv` / `summary.json` logging.
  That's what the real sender and receiver are for.
- Any change to `protocol/`, `config.py`, tests or the frozen decisions.

## 7. How it will be checked

1. `python demo/run_gbn.py` and `python demo/run_sr.py`: clean run, 10 `DELIVER` lines,
   0 `RETX`.
2. `python demo/run_gbn.py --drop 2`: `TIMEOUT DATA 2 -> RETX 2..9`, DATA 3–9 discarded.
3. `python demo/run_sr.py --drop 2`: only `RETX DATA 2`, DATA 3–9 buffered then
   delivered.
4. `--loss 0.2 --seed 2` on both: completes, GBN shows more `RETX` than SR, and repeating
   with the same seed gives the same drop pattern.
5. `pytest` still passes, since nothing outside `demo/` changes.
6. A tshark capture on port 5000 shows 51-byte DATA and 45-byte ACK frames, matching the
   PDF.
7. The same capture read with `-X lua_script:demo/wireshark/arq1.lua -Y arq1` lists the
   sequence of `DATA seq=N` / `ACK seq=N` lines that matches the terminal output. A run
   with `--drop 2` shows the gap at seq 2 and then its retransmission, with no checksum
   warnings.

## 8. Decisions for you

1. Folder name: **`demo/`**.
2. `TOTAL`: **10**, matching the PDF screenshots.
3. §5.4 `--loss` / `--seed`: **included**.
4. **Committed** to the repo.

## 9. As built: differences from the proposal

- **`common/channel.py` added.** The drop logic, the `SEND` / `RETX` / `DROP` lines and
  the summary counters live in one small class that both senders use. The alternative was
  two copies that could drift apart.
- **`ChecksumError(ValueError)` added to `common/packet.py`.** That's how receivers tell
  `CHECKSUM_FAIL` from `MALFORMED` without matching on message text. It's still a
  `ValueError`, so the error handling is unchanged, and the wire format is untouched.
- **`ConnectionResetError` is caught in every receive loop**, the same Windows gotcha as
  in `network/udp.py`. The original crashed if a peer's port had already closed.
- **Senders catch an unparseable ACK** and print it instead of crashing.
- **Each script line-buffers stdout**, and the run scripts use absolute paths. Unbuffered
  output let sender and receiver lines splice mid-line in the shared terminal, and the
  relative paths only worked when run from inside the folder.
- **`run_sr.py` waits for the SR receiver to exit**, which it does once all segments are
  delivered, so its `Transfer complete` line isn't cut off.
- **README example uses `--loss 0.2 --seed 2`.** `--loss 0.1 --seed 1` drops only DATA 8
  and 9, where GBN and SR behave the same.

### Verification (§7)

- Checks 1–4: clean and `--drop 2` runs deliver 10/10. GBN shows `RETX 2..9`, and SR
  shows only `RETX DATA 2`. `--loss 0.1 --seed 1` repeats the same drops across runs.
  At `--loss 0.2 --seed 2`: GBN 21 RETX, 25 DATA on the wire, 4.25 s; SR 3 RETX, 10,
  2.08 s.
- Check 5: `pytest`: 667 passed.
- Checks 6–7: captured on the npcap loopback adapter with tshark 4.6.8. DATA is 51 bytes
  and ACK is 45. With the dissector, the Info column matches the terminal sequence, there
  are no expert warnings on a clean capture, and `arq1.type == 1 && arq1.seq == 2`
  selects exactly one frame per transmission of DATA 2.
- Failure path: a payload byte flipped gives `CHECKSUM_FAIL` in the terminal and
  *Checksum mismatch* in Wireshark. A truncated datagram gives `MALFORMED` and a
  length-mismatch warning.
