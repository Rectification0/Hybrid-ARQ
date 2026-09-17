"""Frontend tests (T11.15): the architecture holds, and deleting it changes nothing.

The Phase 11 brief makes four claims that are easy to state and easy to violate
quietly. Each one is checked here rather than asserted in a document:

1. **Deleting the frontend leaves the protocol's correctness and every recorded
   experimental result unchanged.** Demonstrated, not asserted: nothing under
   ``protocol/``, ``network/``, ``experiments/`` or the endpoints imports it, and
   the suite that proves the protocol correct never touches it.
2. **It reads recorded data.** The data layer opens logs read-only and writes no
   event log of its own, so CC-06 keeps one source per metric.
3. **It never computes protocol behaviour.** The switching policy stays in
   ``protocol/hybrid.py``; metrics come from ``metrics.py``.
4. **It never fabricates.** A missing artifact produces an explicit empty state,
   never a plausible-looking number.

The HTTP layer is exercised against a real server on an ephemeral port, because
a route that only works when called as a Python function is not a route.
"""

from __future__ import annotations

import ast
import csv
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import config
import metrics as metrics_mod
from eventlog import EVENT_COLUMNS
from frontend import data, runner, server
from receiver import Receiver, ReceiverError
from sender import Sender

from test_transfer import make_file

PROJECT_ROOT = config.PROJECT_ROOT
FRONTEND_DIR = PROJECT_ROOT / "frontend"


# ---------------------------------------------------------------------------
# Fixtures: one real transfer, logged where the frontend will find it
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def recorded_run(tmp_path_factory):
    """A real hybrid transfer under a temporary logs/ root the views can read."""
    directory = tmp_path_factory.mktemp("frontend_run")
    logs = directory / "logs"
    source = make_file(directory, size=config.SEGMENT_SIZE * 40)
    output = directory / "received.bin"

    rx = Receiver(output=output, host="127.0.0.1", port=0, run_id="uitest",
                  log_dir=logs, idle_timeout=20.0, linger=0.3)
    port = rx.bind()
    failures = []

    def receive():
        try:
            rx.run()
        except ReceiverError as exc:      # pragma: no cover - a failure is reported
            failures.append(exc)

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    tx = Sender(path=source, host="127.0.0.1", port=port, mode="gbn", window=8,
                run_id="uitest", log_dir=logs, rto=0.25)
    tx.run()
    thread.join(timeout=60)
    assert not failures, failures

    return {"logs": logs, "key": "uitest", "source": source,
            "directory": logs / "uitest"}


@pytest.fixture
def logs_root(monkeypatch, recorded_run):
    """Point the data layer at the temporary logs root for one test."""
    monkeypatch.setattr(data, "LOG_DIR", recorded_run["logs"])
    return recorded_run


@pytest.fixture
def live_server(logs_root):
    """A real dashboard on an ephemeral port, torn down afterwards."""
    instance = server.serve(host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{instance.server_address[1]}"
    try:
        yield base
    finally:
        instance.shutdown()
        instance.server_close()


def free_port() -> int:
    """An unused port, chosen the way an operator would choose one.

    The dashboard refuses port 0 on purpose: a known port is what makes the
    Wireshark filter ``udp.port == <n>`` usable, so "let the OS pick" is not an
    option the form offers.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=15) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def post(base: str, path: str, payload: dict | None = None):
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(base + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 1. Deleting the frontend changes nothing (T11.15)
# ---------------------------------------------------------------------------

PROTOCOL_SOURCES = (
    sorted((PROJECT_ROOT / "protocol").glob("*.py"))
    + sorted((PROJECT_ROOT / "network").glob("*.py"))
    + sorted((PROJECT_ROOT / "experiments").glob("*.py"))
    + [PROJECT_ROOT / "sender.py", PROJECT_ROOT / "receiver.py",
       PROJECT_ROOT / "metrics.py", PROJECT_ROOT / "eventlog.py",
       PROJECT_ROOT / "config.py"]
)


@pytest.mark.parametrize("source", PROTOCOL_SOURCES, ids=lambda p: p.name)
def test_nothing_below_the_frontend_imports_it(source):
    """The dependency arrow points one way, which is what makes deletion safe.

    If any protocol, network, experiment or endpoint module imported the
    frontend, removing the package would break the protocol — and the claim
    T11.15 has to demonstrate would simply be false.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "frontend" not in imported, f"{source.name} imports the frontend"


def test_the_protocol_test_suite_does_not_depend_on_the_frontend():
    """Every test that proves the protocol correct runs with the package gone."""
    for path in sorted((PROJECT_ROOT / "test").glob("test_*.py")):
        if path.name == "test_frontend.py":
            continue
        assert "frontend" not in path.read_text(encoding="utf-8"), \
            f"{path.name} would fail if the frontend were deleted"


def test_the_cli_surface_is_unchanged():
    """Both endpoints still accept exactly the arguments they accepted before.

    The frontend builds these command lines rather than replacing them, so a
    changed flag here would mean the dashboard had quietly forked the CLI.
    """
    import receiver as receiver_module
    import sender as sender_module

    sender_flags = {action.dest for action in sender_module.build_parser()._actions}
    receiver_flags = {action.dest for action in receiver_module.build_parser()._actions}

    assert {"file", "host", "port", "mode", "window", "run_id", "log_dir", "rto",
            "loss", "ack_loss", "rtt", "jitter", "seed", "loss_schedule"} <= sender_flags
    assert {"port", "output", "host", "run_id", "log_dir", "idle_timeout",
            "rtt", "jitter", "seed"} <= receiver_flags


def test_the_runner_invokes_the_projects_own_entry_points(logs_root, monkeypatch):
    """A UI-launched run is the same run as one typed into a terminal."""
    transfer = runner.TransferRunner(log_dir=logs_root["logs"] / "ui",
                                     source_dir=logs_root["logs"] / "ui" / "sources")
    captured = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured.setdefault("commands", []).append(command)
            self.returncode = 0
            raise RuntimeError("not actually started")

    monkeypatch.setattr(runner.subprocess, "Popen", FakePopen)
    with pytest.raises(Exception):
        transfer._start_receiver(runner.ActiveRun(
            run_id="x", key="ui/x", settings=runner.validate({"mode": "gbn", "file_kib": 8}),
            rto_s=0.2, source=Path("src"), output=Path("out"), log_dir=Path("logs")))

    command = captured["commands"][0]
    assert str(PROJECT_ROOT / "receiver.py") in command
    assert "--run-id" in command and "--log-dir" in command


# ---------------------------------------------------------------------------
# 2. It reads recorded data, and writes none of its own
# ---------------------------------------------------------------------------


def test_the_frontend_writes_no_event_log():
    """One event log per endpoint per run. A second would break CC-06.

    Two sources for one metric can disagree, and then no reading of a result is
    authoritative. The frontend therefore never constructs an EventLog and never
    opens a file for writing.
    """
    for path in sorted(FRONTEND_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "EventLog(" not in text, f"{path.name} constructs an event log"
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
                modes = [arg.value for arg in node.args[1:2]
                         if isinstance(arg, ast.Constant)]
                modes += [keyword.value.value for keyword in node.keywords
                          if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant)]
                for mode in modes:
                    assert "w" not in mode and "a" not in mode and "+" not in mode, \
                        f"{path.name} opens a file for writing: mode {mode!r}"


def test_reading_a_run_leaves_it_byte_for_byte_unchanged(logs_root):
    """RP-07: aggregation reads logs and does not modify them."""
    directory = logs_root["directory"]
    before = {path: path.read_bytes()
              for path in sorted(directory.rglob("*")) if path.is_file()}

    data.run_detail(logs_root["key"])
    data.mode_timeline(logs_root["key"])
    data.network_view(logs_root["key"])
    data.retransmission_view(logs_root["key"])
    data.run_events(logs_root["key"])

    after = {path: path.read_bytes()
             for path in sorted(directory.rglob("*")) if path.is_file()}
    assert before == after


def test_event_rows_keep_the_spec_21_columns(logs_root):
    """The view serves the recorded columns, not a reshaped subset."""
    payload = data.run_events(logs_root["key"], endpoint="sender", limit=10)
    assert payload["columns"] == EVENT_COLUMNS
    assert payload["rows"], "the recorded run has no sender rows"
    assert set(payload["rows"][0]) == set(EVENT_COLUMNS)


def test_the_live_lag_is_stated_rather_than_hidden(logs_root):
    """T11.4: events.csv is flushed every 64 rows, so a live view trails it."""
    from eventlog import FLUSH_EVERY
    assert str(FLUSH_EVERY) in data.LIVE_LAG_NOTE
    assert data.run_detail(logs_root["key"])["live_lag_note"] == data.LIVE_LAG_NOTE


# ---------------------------------------------------------------------------
# 3. It never computes protocol behaviour
# ---------------------------------------------------------------------------


def test_the_metrics_shown_are_the_ones_metrics_py_derives(logs_root):
    """T11.9: a completed run's numbers match metrics.py for the same run."""
    detail = data.run_detail(logs_root["key"])
    expected = metrics_mod.derive_from_run(logs_root["directory"])
    assert detail["derived_metrics"] == expected


def test_switch_reasons_come_from_the_log_and_are_not_recomputed(logs_root, monkeypatch):
    """T11.5: the reason is the controller's own string, read back verbatim.

    The check is structural as well as behavioural: the data layer must not
    compare an estimate against a threshold to decide what happened, because a
    threshold evaluated a second time is a second controller.
    """
    source = (FRONTEND_DIR / "data.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            assert not names & {"SWITCH_HIGH", "SWITCH_LOW", "HYSTERESIS_COUNT"}, \
                "the frontend compares against a switching threshold"

    timeline = data.mode_timeline(logs_root["key"])
    rows = [row for row in data.read_events(logs_root["directory"], "sender")
            if row["event"] == "SWITCH"]
    assert [entry["reason"] for entry in timeline["transitions"]] == \
        [row["reason"] for row in rows]


def test_a_noop_switch_is_not_counted_as_a_transition(logs_root, monkeypatch):
    """A FIXED_HYBRID_NOOP row is the control, not a mode change (metrics.py)."""
    rows = [
        {"timestamp": "0.0", "event": "SWITCH", "mode": "gbn", "reason": "START",
         "sequence": "", "ack": "", "window_size": "8", "loss_estimate": "", "rtt_ms": "",
         "bytes": "", "run_id": "x", "endpoint": "sender"},
        {"timestamp": "1.0", "event": "SWITCH", "mode": "gbn",
         "reason": f"{data.NOOP_MARKER};epoch=1", "sequence": "", "ack": "",
         "window_size": "8", "loss_estimate": "0.2", "rtt_ms": "", "bytes": "",
         "run_id": "x", "endpoint": "sender"},
    ]
    monkeypatch.setattr(data, "read_events", lambda directory, endpoint: rows if endpoint == "sender" else [])
    monkeypatch.setattr(data, "resolve_run", lambda key: Path("."))

    timeline = data.mode_timeline("anything")
    assert timeline["noop_count"] == 1
    assert timeline["switch_count"] == metrics_mod.switch_count(rows)
    assert timeline["transitions"][1]["noop"] is True


def test_the_frozen_values_are_presented_as_read_only(logs_root):
    """T11.3: a frozen decision is context, never a form field."""
    configuration = data.configuration()
    editable = set(configuration["editable"])
    assert not editable & {"switch_high", "switch_low", "hysteresis_count",
                           "segment_size", "loss_window_size", "rto_s",
                           "evaluation_interval_segments", "min_mode_residence_s"}
    shown = {entry["name"] for entry in configuration["read_only"]}
    assert {"Switch high", "Switch low", "Hysteresis", "Segment size",
            "Loss window", "RTO policy"} <= shown


def test_the_threshold_is_described_as_an_estimator_reading(logs_root):
    """Never "switch at 10% loss": D8 over-reads under GBN by four to five times."""
    caveat = data.network_view(logs_root["key"])["estimator"]["caveat"]
    assert "not a physical loss rate" in caveat
    assert "over-reads" in caveat


def test_a_frozen_field_cannot_be_set_from_the_dashboard():
    with pytest.raises(runner.ValidationError, match="cannot be set from the dashboard"):
        runner.validate({"switch_high": 0.5})


# ---------------------------------------------------------------------------
# 4. It never fabricates: absence is reported as absence
# ---------------------------------------------------------------------------


def test_a_missing_run_is_refused_rather_than_invented(logs_root):
    with pytest.raises(data.DataError, match="no events.csv"):
        data.run_detail("does-not-exist")


def test_a_key_outside_the_log_directory_is_refused(logs_root):
    """The key arrives from a URL, so it is treated as untrusted."""
    for key in ("../config.py", "../../etc/passwd"):
        with pytest.raises(data.DataError):
            data.resolve_run(key)


def test_missing_results_produce_an_empty_state_not_a_number(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "AGGREGATE_CSV", tmp_path / "absent.csv")
    monkeypatch.setattr(data, "EXPERIMENT_RUNS_CSV", tmp_path / "absent_runs.csv")
    monkeypatch.setattr(data, "PLOT_DIR", tmp_path / "no_plots")
    monkeypatch.setattr(data, "CAPTURE_DIR", tmp_path / "no_captures")

    assert data.aggregate() == {"path": str(tmp_path / "absent.csv"), "exists": False,
                                "rows": [], "systems": [], "experiments": []}
    assert data.experiment_runs()["rows"] == []
    assert data.figures()["exists"] is False
    assert data.captures()["captures"] == []


def test_a_run_with_no_verdict_is_not_reported_as_success(tmp_path, monkeypatch):
    """CC-01: absence of a FIN_ACK is not read as a passing integrity check."""
    directory = tmp_path / "logs" / "silent" / "sender"
    directory.mkdir(parents=True)
    with open(directory / "events.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(EVENT_COLUMNS)
        writer.writerow(["0.000000", "silent", "sender", "START", "", "", "gbn",
                         "8", "", "", "", ""])
    monkeypatch.setattr(data, "LOG_DIR", tmp_path / "logs")

    detail = data.run_detail("silent")
    assert detail["integrity"]["success"] is None
    assert detail["index"]["integrity_success"] is None


def test_a_capture_is_never_claimed_for_an_arbitrary_run(logs_root):
    """captures/ is a curated per-scenario set, not one capture per transfer."""
    companion = data.capture_for_run(logs_root["key"])
    assert companion["own_capture"] is None
    assert "not one capture per transfer" in companion["note"]
    assert "does not inspect packets" in companion["note"]


# ---------------------------------------------------------------------------
# Validation (T11.3) — a refusal is a sentence, not a stack trace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("request_body, expected", [
    ({"mode": "tcp"}, "must be one of"),
    ({"loss": 2.0}, "must be between"),
    ({"window": 0}, "must be between"),
    ({"port": 99999}, "must be between"),
    ({"seed": "not a number"}, "must be a whole number"),
    ({"loss_schedule": "nonsense"}, "is not t:rate"),
    ({"loss_schedule": "5:2.0"}, "rate in"),
    ({"mode": "hybrid", "file_kib": 8}, "needs room to act"),
])
def test_an_invalid_configuration_is_refused_readably(request_body, expected):
    with pytest.raises(runner.ValidationError, match=expected):
        runner.validate(request_body)


def test_a_valid_configuration_is_coerced_and_bounded():
    settings = runner.validate({"mode": "sr", "loss": "0.1", "window": "8", "file_kib": 128})
    assert settings["mode"] == "sr"
    assert settings["loss"] == pytest.approx(0.1)
    assert settings["window"] == 8
    assert settings["seed"] == data.EDITABLE_FIELDS["seed"]["default"]


def test_the_rto_is_derived_not_typed():
    """D7 is applied by the runner, so it cannot be set to an arbitrary value."""
    assert "rto" not in data.EDITABLE_FIELDS
    assert config.baseline_rto(100.0) == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


def test_every_read_route_answers(live_server, logs_root):
    key = logs_root["key"]
    for path in ("/api/health", "/api/config", "/api/runs", "/api/experiments/runs",
                 "/api/experiments/aggregate", "/api/figures", "/api/captures",
                 "/api/demo", "/api/transfer",
                 f"/api/run?run={key}", f"/api/run/events?run={key}",
                 f"/api/run/modes?run={key}", f"/api/run/network?run={key}",
                 f"/api/run/retransmissions?run={key}", f"/api/run/wireshark?run={key}"):
        status, payload = get(live_server, path)
        assert status == 200, path
        assert isinstance(payload, dict), path


def test_the_static_bundle_is_served(live_server):
    for path in ("/", "/app.css", "/js/app.js", "/js/ui.js", "/js/chart.js",
                 "/js/views/modes.js"):
        with urllib.request.urlopen(live_server + path, timeout=10) as response:
            assert response.status == 200, path
            assert response.read(), path


def test_an_unknown_endpoint_answers_with_a_readable_error(live_server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        get(live_server, "/api/nothing-here")
    assert caught.value.code == 404
    assert "no such endpoint" in json.loads(caught.value.read())["error"]


def test_a_traversal_out_of_the_bundle_is_refused(live_server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(live_server + "/js/../../config.py", timeout=10)
    assert caught.value.code in (403, 404)


def test_an_invalid_transfer_request_answers_422_with_the_reason(live_server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(live_server, "/api/transfer", {"mode": "tcp"})
    assert caught.value.code == 422
    body = json.loads(caught.value.read())
    assert "must be one of" in body["error"]
    assert "Traceback" not in body["error"]


def test_stopping_when_nothing_runs_is_refused_readably(live_server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(live_server, "/api/transfer/stop")
    assert caught.value.code == 422
    assert "no transfer is running" in json.loads(caught.value.read())["error"]


def test_health_reports_what_is_missing_rather_than_failing(live_server):
    _, payload = get(live_server, "/api/health")
    assert payload["ok"] is True
    assert set(payload["inventory"]) >= {"logs", "experiment_runs", "aggregate",
                                         "plots", "captures", "demonstration"}
    assert "checks" in payload["preflight"]


# ---------------------------------------------------------------------------
# A transfer launched the way the dashboard launches one
# ---------------------------------------------------------------------------


def test_a_dashboard_launched_transfer_really_transfers_the_file(tmp_path):
    """The whole runner path, with real subprocesses — no mocked Popen.

    This is the claim T11.3 and T11.4 rest on: a run started from the browser is
    the same run as one typed into a terminal. It spawns the project's own
    ``receiver.py`` and ``sender.py``, waits for them to finish, and then checks
    the result the way an outside reader would — from the logs they wrote, with
    ``metrics.py`` doing the derivation.
    """
    transfer = runner.TransferRunner(log_dir=tmp_path / "ui",
                                     source_dir=tmp_path / "ui" / "sources")
    status = transfer.start({
        "mode": "gbn", "file_kib": 32, "window": 8, "port": free_port(),
        "loss": 0.0, "rtt": 0.0, "seed": 20260917,
    })
    assert status["run"]["port"], "the receiver never reported a bound socket"

    deadline = time.monotonic() + 120
    while transfer.busy and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not transfer.busy, "the transfer did not finish inside two minutes"

    final = transfer.status()
    assert final["state"] == runner.STATE_COMPLETE, final["run"]["error"]

    # The evidence is the log, not the runner's memory.
    directory = tmp_path / "ui" / final["run"]["run_id"]
    derived = metrics_mod.derive_from_run(directory)
    assert derived["integrity_success"] is True
    assert derived["delivered_bytes"] == 32 * 1024
    assert derived["retransmission_count"] == 0       # no loss was configured

    # And the progress the UI showed agrees with what the log says.
    progress = final["run"]["progress"]
    assert progress["segments_delivered"] == progress["total_segments"] == 32
    assert progress["logs_present"] is True


def test_a_second_transfer_is_refused_while_one_is_running(tmp_path, monkeypatch):
    """One at a time: two would contend for the port and blur "the current run"."""
    transfer = runner.TransferRunner(log_dir=tmp_path / "ui",
                                     source_dir=tmp_path / "ui" / "sources")
    settings = {"mode": "gbn", "file_kib": 256, "window": 8,
                "port": free_port(), "rtt": 5.0}
    transfer.start(settings)
    try:
        if transfer.busy:
            with pytest.raises(runner.ValidationError, match="One transfer at a time"):
                transfer.start(settings)
    finally:
        if transfer.busy:
            transfer.stop()
        assert transfer.status()["state"] in (runner.STATE_STOPPED, runner.STATE_COMPLETE,
                                              runner.STATE_FAILED)


# ---------------------------------------------------------------------------
# Run discovery and identity (T11.11)
# ---------------------------------------------------------------------------


def test_runs_are_keyed_by_path_so_nested_log_roots_do_not_collide(tmp_path, monkeypatch):
    """A bare run_id is not unique across logs/, which nests by design."""
    for relative in ("cli_gbn", "experiments/E1_gbn_loss00_trial00", "ui/ui_hybrid_x"):
        directory = tmp_path / relative / "sender"
        directory.mkdir(parents=True)
        with open(directory / "events.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(EVENT_COLUMNS)
            writer.writerow(["0.0", Path(relative).name, "sender", "START", "", "",
                             "gbn", "8", "", "", "", ""])
    monkeypatch.setattr(data, "LOG_DIR", tmp_path)

    keys = {entry["key"] for entry in data.list_runs()}
    assert keys == {"cli_gbn", "experiments/E1_gbn_loss00_trial00", "ui/ui_hybrid_x"}
    for key in keys:
        assert data.resolve_run(key).exists()


def test_a_run_directory_is_not_descended_into(tmp_path, monkeypatch):
    """sender/ and receiver/ are the run's children, never runs themselves."""
    directory = tmp_path / "solo" / "sender"
    directory.mkdir(parents=True)
    (directory / "events.csv").write_text(",".join(EVENT_COLUMNS) + "\n", encoding="utf-8")
    monkeypatch.setattr(data, "LOG_DIR", tmp_path)
    assert [entry["key"] for entry in data.list_runs()] == ["solo"]


def test_an_experiment_row_without_local_logs_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "LOG_DIR", tmp_path)
    assert data._log_key_for(r"D:\somewhere\else\logs\experiments\E1_gbn") is None
    assert data._log_key_for("") is None


# ---------------------------------------------------------------------------
# Presentation invariants that are cheap to check and easy to break
# ---------------------------------------------------------------------------


def test_the_demonstration_presents_the_thirteen_specified_steps():
    """T11.13: §28 has thirteen steps, in order, and none is invented."""
    steps = data.demonstration()["steps"]
    assert [step["number"] for step in steps] == list(range(1, 14))
    assert "does not inspect packets" not in steps[0]["detail"]
    assert "no separate demo script" in data.demonstration()["note"]


def test_every_mode_the_ui_styles_is_a_mode_the_project_has():
    """A mode chip must not exist for a mode the protocol cannot run."""
    css = (FRONTEND_DIR / "static" / "app.css").read_text(encoding="utf-8")
    for mode in ("gbn", "sr", "hybrid", "fixed-hybrid", "saw"):
        assert f".mode-chip.mode-{mode}" in css, mode
    import sender as sender_module
    assert set(sender_module.MODE_TASKS) >= {"gbn", "sr", "hybrid", "fixed-hybrid", "saw"}


def test_the_saw_placeholder_is_not_offered_as_a_system_under_evaluation():
    """specs.md §18: the measured baselines are gbn and sr."""
    assert "saw" not in data.EDITABLE_FIELDS["mode"]["choices"]


def test_no_frontend_module_calls_the_coarse_windows_clock():
    """The same rule the endpoints follow: perf_counter, never monotonic."""
    for path in sorted(FRONTEND_DIR.rglob("*.py")):
        assert "time.monotonic" not in path.read_text(encoding="utf-8"), path.name
