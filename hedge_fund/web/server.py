"""The web dashboard: a small local server and the page it serves.

    aihf web                 open http://localhost:8000 in your browser
    aihf web --port 8080     somewhere else
    aihf web --no-browser    just serve

Standard library only — no web framework to install. It binds to
127.0.0.1, so only this computer can reach it, and it refuses requests
whose Host is anything but localhost (a page on another site cannot point
a hostname at it and drive it). Writes need the custom `X-AIHF` header,
which a cross-site form or script cannot send without a CORS preflight this
server never approves.

Trading goes through exactly what the CLI and the terminal app use: `tick`
and `redo` advance the fund and write its ledger, `Ledger.halt` / `resume`
flip the kill switch. A run happens on a background thread; the page polls
for it. One run per fund at a time.
"""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlparse

from hedge_fund.data.protocol import DataClient
from hedge_fund.data.source import needs_data_key
from hedge_fund.fund import Fund, FundSpec, load_spec, normalize_universe
from hedge_fund.llm import load_api_models, provider_for
from hedge_fund.paper import (
    deploy,
    Ledger,
    load_deployed,
    next_session,
    NothingDue,
    redo,
    tick,
    validate_fund_name,
)
from hedge_fund.pipeline.session import FundHalted, is_rebalance_session
from hedge_fund.web import views

STATIC_DIR = Path(__file__).resolve().parent / "static"

DataFactory = Callable[[], AbstractContextManager[DataClient]]


@contextmanager
def market_data() -> Iterator[DataClient]:
    """The data client the CLI uses: the active source (data/source.py), cached."""
    from hedge_fund.data.source import open_client, with_cache

    with open_client() as raw:
        yield with_cache(raw)


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Runs: one background thread per fund, polled by the page
# ---------------------------------------------------------------------------

class Runs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}

    def status(self, name: str) -> dict:
        with self._lock:
            return dict(self._jobs.get(name, {"state": "idle"}))

    def start(self, name: str, work: Callable[[], dict]) -> dict:
        with self._lock:
            if self._jobs.get(name, {}).get("state") == "running":
                raise ApiError(HTTPStatus.CONFLICT, f"{name} is already running")
            job = {"state": "running", "started": _now()}
            self._jobs[name] = job

        def run() -> None:
            try:
                result = work()
                update = {"state": "done", "finished": _now(), **result}
            except NothingDue as exc:
                update = {"state": "nothing_due", "finished": _now(), "message": str(exc)}
            except FundHalted as exc:
                update = {"state": "halted", "finished": _now(), "message": str(exc)}
            except Exception as exc:  # tick has already halted the fund and logged it
                update = {"state": "failed", "finished": _now(),
                          "message": f"{type(exc).__name__}: {exc}"}
            with self._lock:
                self._jobs[name] = {**job, **update}

        threading.Thread(target=run, name=f"aihf-run-{name}", daemon=True).start()
        return dict(job)

    def wait(self, name: str, timeout: float = 10.0) -> dict:
        """Block until *name*'s run ends (tests)."""
        deadline = datetime.now().timestamp() + timeout
        while datetime.now().timestamp() < deadline:
            status = self.status(name)
            if status["state"] != "running":
                return status
            threading.Event().wait(0.02)
        raise TimeoutError(name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# The application: every route, as plain methods the handler dispatches to
# ---------------------------------------------------------------------------

class Dashboard:
    def __init__(
        self,
        paper_root: Path,
        mandates_root: Path,
        *,
        data: DataFactory = market_data,
        build_fund: Callable[[FundSpec], Fund] | None = None,
        save_key: Callable[[str, str], object] | None = None,
    ) -> None:
        self.paper_root = Path(paper_root)
        self.mandates_root = Path(mandates_root)
        self.data = data
        self.build_fund = build_fund
        self.runs = Runs()
        if save_key is None:
            from hedge_fund.tui.keys import save_credential as save_key
        self.save_key = save_key

    # ---- reads ------------------------------------------------------------

    def state(self) -> dict:
        return views.app_state(self.paper_root, self.mandates_root)

    def fund(self, name: str) -> dict:
        directory = self._fund_dir(name)
        return {**views.fund_detail(directory), "run": self.runs.status(name)}

    def plan(self, name: str) -> dict:
        """What pressing Play will do, before anything trades: the session
        that is due, the decision it will execute, whether a new decision
        follows. The one read that touches the network."""
        directory = self._fund_dir(name)
        deployed = load_deployed(directory)
        self._require_keys(deployed.spec)
        state = Ledger(directory).replay(deployed.spec.capital)
        with self.data() as data:
            due = next_session(data, deployed.spec.benchmark, state.last_session)
        return {
            "due": due,
            "last_session": state.last_session,
            "can_redo": due is None and state.last_session is not None,
            "halted": state.halted,
            "executes": ({"as_of": state.pending.as_of, "targets": state.pending.final_weights}
                         if state.pending is not None and due is not None else None),
            "decides": (is_rebalance_session(due, state.last_session, deployed.spec.rebalance)
                        if due else False),
            "rebalance": deployed.spec.rebalance,
        }

    def run_status(self, name: str) -> dict:
        self._fund_dir(name)
        return self.runs.status(name)

    # ---- writes -----------------------------------------------------------

    def create_fund(self, body: dict) -> dict:
        name = str(body.get("name", "")).strip()
        try:
            validate_fund_name(name)
            universe = normalize_universe(re.split(r"[\s,]+", str(body.get("universe", ""))))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        mandate = self._mandate_path(str(body.get("mandate", "")))
        try:
            spec = load_spec(mandate)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        capital = body.get("capital")
        if capital not in (None, ""):
            try:
                capital = float(capital)
            except (TypeError, ValueError) as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "starting money must be a number") from exc
            if not 0 < capital <= 1e12:
                raise ApiError(HTTPStatus.BAD_REQUEST, "starting money must be above zero")
            spec = spec.model_copy(update={"capital": capital})
        try:
            deploy(name, spec, universe, root=self.paper_root)
        except FileExistsError as exc:
            raise ApiError(HTTPStatus.CONFLICT, f"a fund named {name!r} already exists") from exc
        return views.fund_summary(self.paper_root / name)

    def start_run(self, name: str, body: dict) -> dict:
        directory = self._fund_dir(name)
        mode = body.get("mode", "run")
        if mode not in ("run", "redo"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "mode must be 'run' or 'redo'")
        self._require_keys(load_deployed(directory).spec)
        halted = Ledger(directory).halted()
        if halted:
            raise ApiError(HTTPStatus.CONFLICT, f"{name} is halted: {halted}. Resume it first.")

        def work() -> dict:
            with self.data() as data:
                record = (redo(directory, data, build_fund=self.build_fund) if mode == "redo"
                          else tick(directory, data, build_fund=self.build_fund))
            return {"session": record.session, "nav": record.nav,
                    "traded": bool(record.executed and record.executed.fills),
                    "decided": record.decision is not None}

        return self.runs.start(name, work)

    def halt(self, name: str, body: dict) -> dict:
        directory = self._fund_dir(name)
        reason = str(body.get("reason") or "halted from the dashboard").strip()[:200]
        Ledger(directory).halt(reason)
        return views.fund_summary(directory)

    def resume(self, name: str) -> dict:
        directory = self._fund_dir(name)
        Ledger(directory).resume()
        return views.fund_summary(directory)

    def set_key(self, body: dict) -> dict:
        env_var = str(body.get("env_var", ""))
        value = str(body.get("value", "")).strip()
        if env_var not in views.allowed_key_vars():
            raise ApiError(HTTPStatus.BAD_REQUEST, f"unknown key {env_var!r}")
        if not value or any(c in value for c in "\r\n\0") or len(value) > 500:
            raise ApiError(HTTPStatus.BAD_REQUEST, "paste the key exactly as given: one line, no spaces around it")
        self.save_key(env_var, value)
        return self.state()

    def set_model(self, body: dict) -> dict:
        model_id = str(body.get("id", ""))
        if model_id not in {mid for _, mid, _ in load_api_models()} or provider_for(model_id) is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"unknown model {model_id!r}")
        self.save_key(views.MODEL_VAR, model_id)
        return self.state()

    # ---- helpers ----------------------------------------------------------

    def _fund_dir(self, name: str) -> Path:
        try:
            validate_fund_name(name)
        except ValueError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no fund named {name!r}") from exc
        directory = self.paper_root / name
        if not (directory / "fund.yaml").is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, f"no fund named {name!r}")
        return directory

    def _mandate_path(self, stem: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", stem):
            raise ApiError(HTTPStatus.BAD_REQUEST, "pick a fund plan from the list")
        path = self.mandates_root / f"{stem}.yaml"
        if not path.is_file():
            raise ApiError(HTTPStatus.BAD_REQUEST, f"no fund plan named {stem!r}")
        return path

    def _require_keys(self, spec: FundSpec) -> None:
        """Refuse a run early, by name, when a key it needs is missing: the
        Financial Datasets key only when that is the active data source, and
        the AI key only when the fund staffs an LLM analyst."""
        if needs_data_key():
            raise ApiError(HTTPStatus.PRECONDITION_REQUIRED,
                           "Add your Financial Datasets key first (Keys & model), "
                           "or remove DATA_SOURCE=financialdatasets to use free Yahoo prices.")
        uses_ai = any(views.analyst_view(m.name)["kind"] == "ai"
                      for s in spec.strategies for m in s.models)
        model = views.current_model()
        if uses_ai and model["key_env"] and not model["key_set"]:
            raise ApiError(HTTPStatus.PRECONDITION_REQUIRED,
                           f"Add your {model['provider']} key for {model['label']} first (Keys & model).")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_FUND_ROUTE = re.compile(r"^/api/funds/([^/]+)(?:/(plan|run|halt|resume))?$")


def make_handler(app: Dashboard, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"{h}:{port}" for h in ("localhost", "127.0.0.1")} | {"localhost", "127.0.0.1"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "aihf-web"

        def log_message(self, fmt: str, *args) -> None:  # quiet: the terminal is for errors
            return

        # ---- guards -------------------------------------------------------

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def _write_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return (self.headers.get("X-AIHF") == "1"
                    and (origin is None or origin in allowed_origins))

        # ---- verbs --------------------------------------------------------

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden host"})
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            self._dispatch(lambda: self._get(path))

        def do_POST(self) -> None:
            if not self._host_ok() or not self._write_ok():
                return self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            path = urlparse(self.path).path
            self._dispatch(lambda: self._post(path, self._body()))

        # ---- routes -------------------------------------------------------

        def _get(self, path: str) -> dict:
            if path == "/api/state":
                return app.state()
            match = _FUND_ROUTE.match(path)
            if match:
                name, action = match.groups()
                if action is None:
                    return app.fund(name)
                if action == "plan":
                    return app.plan(name)
                if action == "run":
                    return app.run_status(name)
            raise ApiError(HTTPStatus.NOT_FOUND, "not found")

        def _post(self, path: str, body: dict) -> dict:
            if path == "/api/funds":
                return app.create_fund(body)
            if path == "/api/keys":
                return app.set_key(body)
            if path == "/api/model":
                return app.set_model(body)
            match = _FUND_ROUTE.match(path)
            if match:
                name, action = match.groups()
                if action == "run":
                    return app.start_run(name, body)
                if action == "halt":
                    return app.halt(name, body)
                if action == "resume":
                    return app.resume(name)
            raise ApiError(HTTPStatus.NOT_FOUND, "not found")

        # ---- plumbing -----------------------------------------------------

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 100_000:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "body must be JSON") from exc
            if not isinstance(body, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
            return body

        def _dispatch(self, route: Callable[[], dict]) -> None:
            try:
                self._send_json(HTTPStatus.OK, route())
            except ApiError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except Exception as exc:  # surface it on the page, not just the terminal
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                {"error": f"{type(exc).__name__}: {exc}"})

        def _send_json(self, status: HTTPStatus, payload: dict) -> None:
            data = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, path: Path, content_type: str) -> None:
            try:
                data = path.read_bytes()
            except OSError:
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": f"missing {path.name}"})
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def make_server(app: Dashboard, port: int = 8000, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app, port))
    server.daemon_threads = True
    return server


def serve(port: int = 8000, open_browser: bool = True) -> None:
    """Run the dashboard until Ctrl+C."""
    from hedge_fund.paths import ensure_mandates_dir, PAPER_DIR

    app = Dashboard(PAPER_DIR, ensure_mandates_dir())
    try:
        server = make_server(app, port)
    except OSError as exc:
        raise SystemExit(f"Port {port} is busy ({exc}). Try: aihf web --port {port + 1}") from exc
    url = f"http://localhost:{port}"
    print(f"AI Hedge Fund dashboard running at {url}")
    print("Keep this window open while you use it. Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
