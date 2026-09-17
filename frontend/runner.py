"""Launch a transfer from the dashboard by running the existing CLI (T11.3).

The frontend adds no way to move a file. It builds the same ``receiver.py`` and
``sender.py`` command lines an operator would type, starts them as subprocesses
and watches them — which is exactly what ``experiments/run_experiment.py`` does,
and this module reuses that orchestration rather than reimplementing it. The
consequence is the one T11.15 has to demonstrate: a run started from the UI is
byte-for-byte the same run as one started from a terminal, writing the same logs
through the same emitter.

Nothing here computes protocol behaviour. Progress and mode come from the
run's own ``events.csv`` via :mod:`frontend.data`; this module only knows
whether the processes are alive and what they printed.

One run at a time. Two concurrent transfers would contend for the UDP port and,
worse, would make "the current run" ambiguous everywhere else in the UI.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config
from experiments.run_experiment import (
    OrchestrationError, _await_listening, _python, _terminate, source_file,
)
from frontend import data

#: UI-launched runs land here, beside the CLI's and the matrix's rather than
#: mixed in with them, so an evaluator can tell which runs the dashboard started.
UI_LOG_DIR = config.LOG_DIR / "ui"

#: Where generated source files live. The same deterministic pseudo-random blob
#: the matrix used (``run_experiment.source_file``), so a UI run at a given size
#: transfers exactly the bytes an experimental run of that size did.
SOURCE_DIR = config.PROJECT_ROOT / "logs" / "ui" / "sources"

#: How long to wait for the receiver to report a bound socket before giving up.
BIND_TIMEOUT_S = 30.0

STATE_IDLE = "idle"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_COMPLETE = "complete"
STATE_FAILED = "failed"
STATE_STOPPED = "stopped"


class ValidationError(ValueError):
    """A configuration the dashboard refuses, with a readable reason (T11.3)."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(request: dict[str, Any]) -> dict[str, Any]:
    """Coerce and bound a transfer request against :data:`data.EDITABLE_FIELDS`.

    Every message raised here is meant to be shown to a person: an invalid
    configuration is refused with a sentence, never with a stack trace (T11.3).
    A field the table does not list is rejected rather than passed through —
    the same discipline ``config.snapshot`` applies to a recorded config, for the
    same reason.
    """
    unknown = set(request) - set(data.EDITABLE_FIELDS)
    if unknown:
        raise ValidationError(
            f"{', '.join(sorted(unknown))} cannot be set from the dashboard. "
            f"Frozen values are shown as read-only context because changing one "
            f"invalidates recorded experiments.")

    clean: dict[str, Any] = {}
    for name, spec in data.EDITABLE_FIELDS.items():
        raw = request.get(name, spec["default"])
        label = spec["label"]
        if spec["type"] == "choice":
            value = str(raw)
            if value not in spec["choices"]:
                raise ValidationError(
                    f"{label} must be one of {', '.join(spec['choices'])}, not {value!r}")
        elif spec["type"] == "int":
            value = _number(raw, label, int)
            _bound(value, spec, label)
        elif spec["type"] == "float":
            value = _number(raw, label, float)
            _bound(value, spec, label)
        else:
            value = str(raw).strip()
        clean[name] = value

    if clean["loss_schedule"]:
        _validate_schedule(clean["loss_schedule"])
    if clean["mode"] in ("hybrid", "fixed-hybrid") and clean["file_kib"] < 64:
        # Not a hard protocol limit, but a transfer this short cannot show the
        # controller doing anything: D8 needs about 110 acknowledged segments to
        # confirm a change, and 64 KiB is 64 segments. Saying so is more useful
        # than letting an evaluator conclude the controller is broken.
        raise ValidationError(
            f"{clean['mode']} needs room to act: the controller confirms a change "
            f"over roughly 110 acknowledged segments, so a file below 64 KiB "
            f"(64 segments) cannot switch. Raise the file size or pick gbn/sr.")
    return clean


def _number(raw: Any, label: str, kind):
    try:
        return kind(raw)
    except (TypeError, ValueError):
        name = "a whole number" if kind is int else "a number"
        raise ValidationError(f"{label} must be {name}, not {raw!r}") from None


def _bound(value, spec, label) -> None:
    if value < spec["min"] or value > spec["max"]:
        raise ValidationError(
            f"{label} must be between {spec['min']:g} and {spec['max']:g}, got {value:g}")


def _validate_schedule(text: str) -> None:
    """The same ``t:rate,t:rate`` grammar ``sender.py`` parses (§17.3)."""
    for chunk in text.split(","):
        moment, separator, rate = chunk.partition(":")
        if not separator:
            raise ValidationError(
                f"Loss schedule step {chunk!r} is not t:rate — for example "
                f"5:0.1,10:0.0 means 10% loss from 5 s and none from 10 s.")
        try:
            seconds, probability = float(moment), float(rate)
        except ValueError:
            raise ValidationError(
                f"Loss schedule step {chunk!r} has a non-numeric time or rate") from None
        if seconds < 0 or not 0.0 <= probability <= 1.0:
            raise ValidationError(
                f"Loss schedule step {chunk!r}: time must be >= 0 and rate in [0, 1]")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class ActiveRun:
    """One UI-launched transfer and what has been observed about it."""

    run_id: str
    key: str
    settings: dict[str, Any]
    rto_s: float
    source: Path
    output: Path
    log_dir: Path
    port: int | None = None
    state: str = STATE_STARTING
    started_wall: float = field(default_factory=time.time)
    started: float = field(default_factory=time.perf_counter)
    finished: float | None = None
    error: str | None = None
    sender_stdout: list[str] = field(default_factory=list)
    sender_stderr: list[str] = field(default_factory=list)
    receiver_stdout: list[str] = field(default_factory=list)
    receiver_stderr: list[str] = field(default_factory=list)
    command_lines: dict[str, list[str]] = field(default_factory=dict)

    @property
    def elapsed_s(self) -> float:
        end = self.finished if self.finished is not None else time.perf_counter()
        return end - self.started

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "key": self.key,
            "settings": self.settings,
            "rto_s": self.rto_s,
            "source": str(self.source),
            "output": str(self.output),
            "port": self.port,
            "state": self.state,
            "started_wall_clock": self.started_wall,
            "elapsed_s": self.elapsed_s,
            "error": self.error,
            "commands": self.command_lines,
            "stdout": {"sender": self.sender_stdout[-40:],
                       "receiver": self.receiver_stdout[-40:]},
            "stderr": {"sender": self.sender_stderr[-40:],
                       "receiver": self.receiver_stderr[-40:]},
        }


class TransferRunner:
    """Owns at most one active transfer and reports what it is doing."""

    def __init__(self, log_dir: Path = UI_LOG_DIR, source_dir: Path = SOURCE_DIR):
        self.log_dir = Path(log_dir)
        self.source_dir = Path(source_dir)
        self._lock = threading.Lock()
        self._active: ActiveRun | None = None
        self._receiver: subprocess.Popen | None = None
        self._sender: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop_requested = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active is not None and self._active.state in (
                STATE_STARTING, STATE_RUNNING)

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        """Validate, then launch receiver and sender. Returns the run's status."""
        settings = validate(request)
        with self._lock:
            if self._active is not None and self._active.state in (
                    STATE_STARTING, STATE_RUNNING):
                raise ValidationError(
                    f"{self._active.run_id} is still running. One transfer at a time: "
                    f"a second would contend for UDP port {self._active.port} and make "
                    f"'the current run' ambiguous. Stop it first.")

            run_id = (f"ui_{settings['mode']}_{time.strftime('%Y%m%d_%H%M%S')}"
                      f"_{uuid.uuid4().hex[:6]}")
            size = settings["file_kib"] * 1024
            source = source_file(self.source_dir, size)
            output = self.log_dir / "output" / f"{run_id}.bin"
            output.parent.mkdir(parents=True, exist_ok=True)
            # D7: derived from this condition's RTT and then held fixed for the
            # whole run, exactly as the matrix did — not a dashboard setting.
            rto = config.baseline_rto(settings["rtt"])
            active = ActiveRun(run_id=run_id, key=f"ui/{run_id}", settings=settings,
                               rto_s=rto, source=source, output=output,
                               log_dir=self.log_dir)
            self._active = active
            self._stop_requested = False

        self._thread = threading.Thread(target=self._drive, args=(active,), daemon=True)
        self._thread.start()

        # Wait briefly for the bind so the caller learns the port — or the
        # failure — instead of being told "starting" and left to discover that
        # the port was already held (T11.14).
        deadline = time.perf_counter() + BIND_TIMEOUT_S
        while time.perf_counter() < deadline:
            if active.port is not None or active.state in (STATE_FAILED, STATE_STOPPED):
                break
            time.sleep(0.02)
        return self.status()

    def _drive(self, active: ActiveRun) -> None:
        """Run the two processes to completion on a worker thread."""
        try:
            self._start_receiver(active)
            if self._stop_requested:
                return
            self._start_sender(active)
            self._await_completion(active)
        except OrchestrationError as exc:
            active.state, active.error = STATE_FAILED, str(exc)
        except Exception as exc:                      # pragma: no cover - defensive
            active.state, active.error = STATE_FAILED, f"{type(exc).__name__}: {exc}"
        finally:
            if active.finished is None:
                active.finished = time.perf_counter()
            self._reap()

    def _start_receiver(self, active: ActiveRun) -> None:
        settings = active.settings
        command = _python() + [
            str(config.PROJECT_ROOT / "receiver.py"),
            "--port", str(settings["port"]),
            "--output", str(active.output),
            "--host", settings["host"],
            "--run-id", active.run_id,
            "--log-dir", str(active.log_dir),
            "--rtt", str(settings["rtt"]),
            "--jitter", str(settings["jitter"]),
            "--seed", str(settings["seed"]),
        ]
        active.command_lines["receiver"] = command
        self._receiver = subprocess.Popen(
            command, cwd=str(config.PROJECT_ROOT), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            active.port = _await_listening(self._receiver, timeout=BIND_TIMEOUT_S)
        except OrchestrationError as exc:
            _terminate(self._receiver)
            # The usual cause is a port still held by an earlier run, and the
            # receiver's own stderr says which — so it is reported, not guessed.
            raise OrchestrationError(
                f"receiver could not bind {settings['host']}:{settings['port']} — {exc}"
            ) from exc
        active.receiver_stdout.append(f"listening on {settings['host']}:{active.port}")

    def _start_sender(self, active: ActiveRun) -> None:
        settings = active.settings
        command = _python() + [
            str(config.PROJECT_ROOT / "sender.py"),
            "--file", str(active.source),
            "--host", settings["host"],
            "--port", str(active.port),
            "--mode", settings["mode"],
            "--window", str(settings["window"]),
            "--run-id", active.run_id,
            "--log-dir", str(active.log_dir),
            "--rto", str(active.rto_s),
            "--loss", str(settings["loss"]),
            "--ack-loss", str(settings["ack_loss"]),
            "--rtt", str(settings["rtt"]),
            "--jitter", str(settings["jitter"]),
            "--seed", str(settings["seed"]),
        ]
        if settings["loss_schedule"]:
            command += ["--loss-schedule", settings["loss_schedule"]]
        active.command_lines["sender"] = command
        self._sender = subprocess.Popen(
            command, cwd=str(config.PROJECT_ROOT), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        active.state = STATE_RUNNING

    def _await_completion(self, active: ActiveRun) -> None:
        """Wait for both processes, then settle the run's state.

        The state is **not** decided when the sender exits. The receiver lingers
        after its FIN_ACK, and its log is flushed every ``FLUSH_EVERY`` rows and
        fsynced only on close — so a run marked complete at the sender's exit
        would be one whose receiver log is still a partial buffer with no
        ``summary.json`` beside it. The dashboard would then show a finished
        transfer over a truncated log, which is exactly the kind of disagreement
        between a screen and a recorded artifact this phase exists to avoid.
        """
        sender, receiver = self._sender, self._receiver
        assert sender is not None and receiver is not None

        failure: str | None = None
        timeout = config.EXPERIMENT_ABORT_TIMEOUT_S
        try:
            out, err = sender.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # specs.md §13: a transfer that will not finish is abandoned and
            # reported, never waited on indefinitely. Its logs are kept.
            _terminate(sender)
            out, err = sender.communicate(timeout=30)
            failure = f"abort timeout after {timeout:g}s"
        _record_output(active, "sender", out, err)

        if failure is None and sender.returncode != 0 and not self._stop_requested:
            lines = [line for line in (err or "").strip().splitlines() if line]
            # The endpoint's own message is preserved verbatim: the UI shows a
            # readable explanation *and* keeps the logged error (T11.14).
            failure = lines[0] if lines else f"sender exited {sender.returncode}"

        # The receiver exits on its own once the transfer finishes and its linger
        # expires; it is killed only if it does not. Either way its log is closed
        # before the run is called finished.
        try:
            out, err = receiver.communicate(timeout=max(30.0, config.RECEIVER_LINGER_S * 5))
        except subprocess.TimeoutExpired:
            _terminate(receiver)
            out, err = receiver.communicate(timeout=30)
            failure = failure or "receiver did not exit"
        _record_output(active, "receiver", out, err)

        if self._stop_requested:
            active.state = STATE_STOPPED
            active.error = active.error or "stopped from the dashboard"
        elif failure is not None:
            active.state = STATE_FAILED
            active.error = failure
        else:
            active.state = STATE_COMPLETE
        active.finished = time.perf_counter()

    def stop(self) -> dict[str, Any]:
        """Terminate the active transfer, keeping whatever it logged (T11.3).

        Stopping is safe precisely because the logs are append-only and flushed
        as they go: an abandoned transfer leaves a partial but valid log, which
        the run browser then shows as stopped rather than as a result.
        """
        with self._lock:
            active = self._active
            if active is None or active.state not in (STATE_STARTING, STATE_RUNNING):
                raise ValidationError("no transfer is running")
            self._stop_requested = True
            active.state = STATE_STOPPED
            active.error = "stopped from the dashboard"
        for process in (self._sender, self._receiver):
            if process is not None:
                _terminate(process)
        if self._thread is not None:
            self._thread.join(timeout=30)
        return self.status()

    def reset(self) -> dict[str, Any]:
        """Forget the finished run. Its logs stay on disk untouched (RP-07)."""
        with self._lock:
            if self._active is not None and self._active.state in (
                    STATE_STARTING, STATE_RUNNING):
                raise ValidationError("a transfer is running; stop it first")
            self._active = None
        self._reap()
        return self.status()

    def _reap(self) -> None:
        for name in ("_sender", "_receiver"):
            process = getattr(self, name)
            if process is not None and process.poll() is not None:
                setattr(self, name, None)

    # -- status ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """The live view's source of truth (T11.4).

        Lifecycle state and progress are read back out of the run's own event
        log, not out of this process's memory — which is what keeps the number
        the dashboard shows the same number ``metrics.py`` would derive, and
        keeps the UI honest when the transfer was started from a terminal
        instead.
        """
        with self._lock:
            active = self._active
        if active is None:
            return {"active": False, "state": STATE_IDLE, "run": None,
                    "progress": None, "live_lag_note": data.LIVE_LAG_NOTE}

        payload = active.as_dict()
        payload["progress"] = self._progress(active)
        return {"active": active.state in (STATE_STARTING, STATE_RUNNING),
                "state": active.state, "run": payload,
                "live_lag_note": data.LIVE_LAG_NOTE}

    def _progress(self, active: ActiveRun) -> dict[str, Any]:
        """Segment counts and current mode, tailed from ``events.csv``."""
        directory = active.log_dir / active.run_id
        try:
            sender_rows = data.read_events(directory, "sender")
            receiver_rows = data.read_events(directory, "receiver")
        except OSError:
            sender_rows, receiver_rows = [], []

        total_segments = -(-active.settings["file_kib"] * 1024 // config.SEGMENT_SIZE)
        unique_sent = len({row["sequence"] for row in sender_rows
                           if row["event"] == "SEND" and row["sequence"]})
        delivered = sum(1 for row in receiver_rows if row["event"] == "DELIVER")
        acked = sum(1 for row in sender_rows if row["event"] == "ACK")
        mode_rows = [row["mode"] for row in sender_rows if row["mode"]]
        estimates = [row["loss_estimate"] for row in sender_rows if row["loss_estimate"]]

        # design.md §7.1/§7.2 states; no new vocabulary is invented here.
        sender_state = {
            STATE_STARTING: "STARTING", STATE_RUNNING: "SENDING",
            STATE_COMPLETE: "COMPLETE", STATE_FAILED: "ERROR",
            STATE_STOPPED: "ERROR",
        }[active.state]

        return {
            "sender_state": sender_state,
            "total_segments": total_segments,
            "segments_sent": unique_sent,
            "segments_acked": acked,
            "segments_delivered": delivered,
            "retransmissions": sum(1 for row in sender_rows if row["event"] == "RETX"),
            "fraction": (delivered / total_segments) if total_segments else None,
            "current_mode": mode_rows[-1] if mode_rows else None,
            "loss_estimate": float(estimates[-1]) if estimates else None,
            "window_size": active.settings["window"],
            "sender_rows": len(sender_rows),
            "receiver_rows": len(receiver_rows),
            "logs_present": (directory / "sender" / "events.csv").exists(),
        }


#: The dashboard's single runner. Module level because there is one dashboard
#: process and one UDP port; a per-request instance could start a second transfer.
RUNNER = TransferRunner()


def _record_output(active: ActiveRun, endpoint: str, out: str | None,
                   err: str | None) -> None:
    for stream, text in (("stdout", out), ("stderr", err)):
        if not text:
            continue
        target = getattr(active, f"{endpoint}_{stream}")
        target.extend(line for line in text.splitlines() if line.strip())


def preflight() -> dict[str, Any]:
    """Whether a transfer could be launched right now, and what is missing.

    The dashboard shows this before the start button is pressed, so "backend
    reachable" is a statement about the things a run actually needs rather than
    only about the HTTP server answering (T11.3, T11.14).
    """
    checks = []
    checks.append({
        "name": "Python interpreter",
        "ok": bool(sys.executable),
        "detail": sys.executable or "unknown",
    })
    for script in ("sender.py", "receiver.py"):
        path = config.PROJECT_ROOT / script
        checks.append({"name": script, "ok": path.exists(), "detail": str(path)})
    writable = True
    detail = str(UI_LOG_DIR)
    try:
        UI_LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        writable, detail = False, f"{UI_LOG_DIR}: {exc}"
    checks.append({"name": "logs/ui writable", "ok": writable, "detail": detail})
    return {"ok": all(check["ok"] for check in checks), "checks": checks}
