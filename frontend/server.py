"""The dashboard's HTTP server: a JSON API plus static files (T11.1).

``http.server`` from the standard library, not a web framework. The protocol
itself is standard library only, and the frontend is a presentation layer over
it — adding Flask, FastAPI or a Node toolchain to *look at* a stdlib protocol
would mean an evaluator has to install and build something before they can read
a result. ``python -m frontend`` is the whole run command, and there is no build
step: the static files are served as written (design.md §13).

Everything under ``/api/`` is read-only except the three transfer endpoints,
which start, stop and clear a run by invoking the *existing* CLI through
:mod:`frontend.runner`. No route writes to a log, a results CSV or a capture.

It binds loopback by default. The dashboard can start processes and read this
checkout's files, so it is a local tool, not a service to expose.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import config
from frontend import data
from frontend.runner import RUNNER, ValidationError, preflight

STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

#: Directories served verbatim outside the static bundle, each read-only and
#: each pinned to one extension so a route cannot be talked into serving a log.
ASSET_ROOTS = {
    "/plots/": (config.PLOT_DIR, {".png"}),
}


class ApiError(Exception):
    """A request the API refuses, carrying the status to answer with."""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _one(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name) or []
    return values[0] if values else default


def _int(query: dict[str, list[str]], name: str, default: int) -> int:
    raw = _one(query, name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ApiError(f"{name} must be a whole number, got {raw!r}") from None


def _run_key(query: dict[str, list[str]]) -> str:
    key = _one(query, "run")
    if not key:
        raise ApiError("no run was named; pass ?run=<key>")
    return key


def route_health(query) -> dict[str, Any]:
    """Enough for the UI to say the backend is reachable *and* usable (T11.3)."""
    return {
        "ok": True,
        "project": "Hybrid GBN/SR ARQ with Adaptive Switching",
        "project_root": str(config.PROJECT_ROOT),
        "inventory": data.artifact_inventory(),
        "preflight": preflight(),
        "live_lag_note": data.LIVE_LAG_NOTE,
    }


def route_config(query) -> dict[str, Any]:
    return data.configuration()


def route_runs(query) -> dict[str, Any]:
    runs = data.list_runs()
    group = _one(query, "group")
    if group:
        runs = [run for run in runs if run["group"] == group]
    limit = _int(query, "limit", 0)
    return {
        "runs": runs[:limit] if limit else runs,
        "count": len(runs),
        "groups": sorted({run["group"] for run in data.list_runs()}),
    }


def route_run(query) -> dict[str, Any]:
    return data.run_detail(_run_key(query))


def route_run_events(query) -> dict[str, Any]:
    return data.run_events(
        _run_key(query),
        endpoint=_one(query, "endpoint"),
        offset=_int(query, "offset", 0),
        limit=_int(query, "limit", 500),
        event=_one(query, "event"),
        mode=_one(query, "mode"),
        sequence=_one(query, "sequence"),
    )


def route_run_modes(query) -> dict[str, Any]:
    return data.mode_timeline(_run_key(query))


def route_run_network(query) -> dict[str, Any]:
    return data.network_view(_run_key(query))


def route_run_retransmissions(query) -> dict[str, Any]:
    return data.retransmission_view(_run_key(query))


def route_run_wireshark(query) -> dict[str, Any]:
    return data.capture_for_run(_run_key(query))


def route_experiment_runs(query) -> dict[str, Any]:
    return data.experiment_runs()


def route_aggregate(query) -> dict[str, Any]:
    return data.aggregate()


def route_figures(query) -> dict[str, Any]:
    return data.figures()


def route_captures(query) -> dict[str, Any]:
    return data.captures()


def route_demo(query) -> dict[str, Any]:
    return data.demonstration()


def route_transfer_status(query) -> dict[str, Any]:
    return RUNNER.status()


def route_transfer_start(query, body: dict[str, Any]) -> dict[str, Any]:
    return RUNNER.start(body)


def route_transfer_stop(query, body: dict[str, Any]) -> dict[str, Any]:
    return RUNNER.stop()


def route_transfer_reset(query, body: dict[str, Any]) -> dict[str, Any]:
    return RUNNER.reset()


GET_ROUTES: dict[str, Callable[[dict], Any]] = {
    "/api/health": route_health,
    "/api/config": route_config,
    "/api/runs": route_runs,
    "/api/run": route_run,
    "/api/run/events": route_run_events,
    "/api/run/modes": route_run_modes,
    "/api/run/network": route_run_network,
    "/api/run/retransmissions": route_run_retransmissions,
    "/api/run/wireshark": route_run_wireshark,
    "/api/experiments/runs": route_experiment_runs,
    "/api/experiments/aggregate": route_aggregate,
    "/api/figures": route_figures,
    "/api/captures": route_captures,
    "/api/demo": route_demo,
    "/api/transfer": route_transfer_status,
}

POST_ROUTES: dict[str, Callable[[dict, dict], Any]] = {
    "/api/transfer": route_transfer_start,
    "/api/transfer/stop": route_transfer_stop,
    "/api/transfer/reset": route_transfer_reset,
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    """Serves the API and the static bundle. One instance per request."""

    server_version = "HybridARQDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        """Quiet by default: a request line per poll would bury a real error."""
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str,
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The dashboard polls; a cached events page would show a frozen transfer.
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, message: str, status: int, detail: str = "") -> None:
        """An error the UI can show as a sentence, with the detail preserved.

        T11.14: every failure shows a readable explanation *and* keeps the
        underlying error. The ``detail`` is the traceback or the endpoint's own
        message — the UI puts it behind a disclosure rather than discarding it.
        """
        self._send_json({"error": message, "detail": detail, "status": int(status)},
                        status=status)

    # -- verbs -------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path in GET_ROUTES:
                self._send_json(GET_ROUTES[path](query))
                return
            if path.startswith("/api/"):
                raise ApiError(f"no such endpoint: {path}", HTTPStatus.NOT_FOUND)
            self._serve_file(path)
        except ApiError as exc:
            self._send_error_json(str(exc), exc.status)
        except data.DataError as exc:
            self._send_error_json(str(exc), HTTPStatus.NOT_FOUND)
        except BrokenPipeError:                       # pragma: no cover
            pass                                      # the browser navigated away
        except Exception as exc:                      # pragma: no cover - defensive
            self._send_error_json(f"{type(exc).__name__}: {exc}",
                                  HTTPStatus.INTERNAL_SERVER_ERROR,
                                  traceback.format_exc())

    do_HEAD = do_GET

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            handler = POST_ROUTES.get(path)
            if handler is None:
                raise ApiError(f"no such endpoint: {path}", HTTPStatus.NOT_FOUND)
            self._send_json(handler(query, self._body()))
        except ValidationError as exc:
            # A refused configuration is a readable sentence, not a stack trace
            # (T11.3). 422: the request was understood and rejected on content.
            self._send_error_json(str(exc), HTTPStatus.UNPROCESSABLE_ENTITY)
        except ApiError as exc:
            self._send_error_json(str(exc), exc.status)
        except Exception as exc:                      # pragma: no cover - defensive
            self._send_error_json(f"{type(exc).__name__}: {exc}",
                                  HTTPStatus.INTERNAL_SERVER_ERROR,
                                  traceback.format_exc())

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(f"request body is not JSON: {exc}") from None
        if not isinstance(payload, dict):
            raise ApiError("request body must be a JSON object")
        return payload

    # -- static ------------------------------------------------------------

    def _serve_file(self, path: str) -> None:
        """Serve the bundle, plus the read-only asset roots.

        Paths are resolved and then checked to be inside their root, so a
        ``..`` cannot walk out of the bundle and into the logs.
        """
        for prefix, (root, allowed) in ASSET_ROOTS.items():
            if path.startswith(prefix):
                self._send_from(root, path[len(prefix):], allowed)
                return
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        self._send_from(STATIC_DIR, relative, None)

    def _send_from(self, root: Path, relative: str, allowed: set[str] | None) -> None:
        if not relative:
            raise ApiError("no file named", HTTPStatus.NOT_FOUND)
        candidate = (root / relative).resolve()
        if root.resolve() not in candidate.parents and candidate != root.resolve():
            raise ApiError("path is outside the served directory", HTTPStatus.FORBIDDEN)
        if allowed is not None and candidate.suffix.lower() not in allowed:
            raise ApiError(f"{candidate.suffix or 'that file type'} is not served here",
                           HTTPStatus.FORBIDDEN)
        if not candidate.is_file():
            raise ApiError(f"not found: /{relative}", HTTPStatus.NOT_FOUND)
        content_type, _ = mimetypes.guess_type(candidate.name)
        self._send(HTTPStatus.OK, candidate.read_bytes(),
                   content_type or "application/octet-stream")


class Dashboard(ThreadingHTTPServer):
    """Threaded so a slow poll cannot block the start button."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, verbose: bool = False):
        super().__init__(address, DashboardHandler)
        self.verbose = verbose


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          verbose: bool = False) -> Dashboard:
    """Start the server on a background thread and return it (used by tests)."""
    server = Dashboard((host, port), verbose=verbose)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.thread = thread
    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dashboard for the hybrid ARQ protocol (Phase 11).")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="bind address; loopback by default, because the "
                             "dashboard can start processes and read this checkout")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="HTTP port for the dashboard, not the protocol's UDP port")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        server = Dashboard((args.host, args.port), verbose=args.verbose)
    except OSError as exc:
        print(f"error: cannot bind {args.host}:{args.port} — {exc}")
        print("Another dashboard may already be running; try --port 8081.")
        return 2

    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"Hybrid ARQ dashboard on {url}")
    print(f"  project      {config.PROJECT_ROOT}")
    print(f"  protocol UDP port {config.PORT} (Wireshark: udp.port == {config.PORT})")
    inventory = data.artifact_inventory()
    print(f"  logs         {inventory['logs']['runs']} recorded runs")
    for name in ("experiment_runs", "aggregate", "plots", "captures"):
        mark = "ok" if inventory[name]["exists"] else "missing"
        print(f"  {name:<12} {mark} ({inventory[name]['path']})")
    print("Ctrl-C to stop. The CLI is unchanged and still works on its own.")

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
