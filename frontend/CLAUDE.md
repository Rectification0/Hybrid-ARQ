# The frontend (Phase 11, M12)

Scoped guidance for `frontend/`. Migrated out of the root `CLAUDE.md` so it loads only
when working under this directory.

`python -m frontend` serves a dashboard at `127.0.0.1:8080` over the recorded artifacts and
the existing CLI. It is a **presentation and control layer**: it adds no capability to the
protocol, and deleting `frontend/` leaves the protocol's correctness and every recorded
result unchanged — `test/test_frontend.py` demonstrates that rather than asserting it.

Standard library only, like the protocol: `http.server` on the back, hand-written ES modules
and inline SVG on the front, no build step and nothing added to `requirements.txt`.
Architecture and rationale in `design.md` §13, recorded as F1–F6 — **not** as D-numbers and
**not** in `specs.md` §16, because a framework choice cannot invalidate a transfer that
already happened. Phase 11 introduced no new requirement IDs.

There is no database, deliberately. `events.csv` and `summary.json` already are the durable
record, and copying them into a second store would give one number two sources that can
disagree — which is what CC-06 exists to prevent. `frontend/data.py` opens them read-only
and shapes them for JSON; a test asserts no frontend module ever constructs an `EventLog` or
opens a file for writing.

Three rules govern anything added to it, each enforced by a test rather than by discipline:

1. **It reads recorded data.** No second logging path.
2. **It never computes protocol behaviour.** Switch reasons, the estimate at a transition and
   the epoch are read from the recorded rows verbatim; metrics come from `metrics.py`. A test
   parses `frontend/data.py` and fails if it compares anything against `SWITCH_HIGH`,
   `SWITCH_LOW` or `HYSTERESIS_COUNT` — a threshold evaluated a second time is a second
   controller, and the two would drift.
3. **It never fabricates.** A missing run, results file or capture produces an explicit empty
   state naming the path it looked for. A value that was not recorded renders as a dash,
   never as zero, and a run with no FIN_ACK verdict is never shown as passing (CC-01).

Frozen values appear as read-only context, never as form fields. RTO is not a field at all —
D7 derives it from the condition's RTT. Runs the dashboard starts land in `logs/ui/`, via the
project's own `sender.py` / `receiver.py` command lines, which the control panel displays.

Two limits worth knowing before changing anything there: the live view trails a transfer by
up to `FLUSH_EVERY = 64` rows and **the flush policy is not to be loosened to make it
smoother** — that would trade measurement fidelity for animation; and `captures/` holds a
curated per-*scenario* evidence set, not one capture per transfer, so nothing may imply an
arbitrary run has one. The Wireshark panel points at Wireshark and states plainly that the
dashboard does not inspect packets.
