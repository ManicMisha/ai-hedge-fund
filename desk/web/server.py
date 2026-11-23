"""The desk page.

    python -m desk web               http://localhost:8787
    python -m desk web --port 9000   somewhere else

Read-only for now: it shows what the desk is, which settings are still empty
and which agents are waiting on them. It changes nothing. Later stages add the
paper account, tickets and results, still behind the same rules.

It binds to 127.0.0.1, so only this computer can reach it, and it refuses any
request whose Host header is not localhost, so a web page elsewhere cannot
point a hostname at it. GET only.
"""

from __future__ import annotations

import json
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from desk import config
from desk.params import AGENT_BY_KEY, AGENTS, GROUPS, KIND_HINTS, PARAMS, CHOICE

STATIC = Path(__file__).resolve().parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".woff2": "font/woff2", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}

# What each later stage adds to the page. Shown as locked panels so the owner
# can see what is coming and what unlocks it.
STAGES = [
    {"n": 1, "name": "Settings and this page", "done": True},
    {"n": 2, "name": "Message formats", "done": False},
    {"n": 3, "name": "Risk engine", "done": False},
    {"n": 4, "name": "Paper account and ledger", "done": False},
    {"n": 5, "name": "Agents, /desk-cycle and /desk-summary", "done": False},
    {"n": 6, "name": "Data sources", "done": False},
    {"n": 7, "name": "Approvals through GitHub", "done": False},
    {"n": 8, "name": "Results report", "done": False},
]


def _shown(raw) -> str:
    if raw is None:
        return ""
    return config.display_value(raw)


def state() -> dict:
    """Everything the page shows, as JSON. Never includes anything secret: the config holds no keys."""
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        return {"ok": False, "error": str(exc), "stages": STAGES}
    params = []
    for p in PARAMS:
        s = cfg.states[p.name]
        params.append(
            {
                "name": p.name,
                "sets": p.sets.replace("`", ""),
                "group": p.group,
                "limit": p.limit,
                "status": s.status,
                "problem": s.problem,
                "value": _shown(s.raw) if s.status != "empty" else "",
                "format": ("One of: " + ", ".join(p.choices) + ".") if p.kind == CHOICE else KIND_HINTS[p.kind],
                "used_by": [AGENT_BY_KEY[k].name for k in p.used_by],
            }
        )
    agents = []
    for a in AGENTS:
        names = config.placeholders(config.template(a.key))
        waiting = [n for n in names if cfg.states[n].status != "filled"]
        agents.append(
            {
                "key": a.key,
                "name": a.name,
                "job": a.core_job,
                "produces": a.produces,
                "access": a.access,
                "order_tools": a.order_tools,
                "needs": len(names),
                "waiting": waiting,
            }
        )
    return {
        "ok": True,
        "config_path": "config/desk.yaml",
        "filled": cfg.filled_count,
        "total": len(PARAMS),
        "invalid": len(cfg.invalid),
        "groups": list(GROUPS),
        "params": params,
        "agents": agents,
        "stages": STAGES,
    }


def make_handler(port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "desk"

        def log_message(self, fmt: str, *args) -> None:  # keep the terminal quiet
            pass

        def do_GET(self) -> None:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain; charset=utf-8")
                return
            path = urlparse(self.path).path
            if path == "/api/state":
                body = json.dumps(state()).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json")
                return
            if path in ("/", "/index.html"):
                target = STATIC / "index.html"
            elif path.startswith("/static/"):
                target = (STATIC / path[len("/static/"):]).resolve()
                if STATIC.resolve() not in target.parents:
                    self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
                    return
            else:
                self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
                return
            if not target.is_file() or target.suffix not in TYPES:
                self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
                return
            self._send(HTTPStatus.OK, target.read_bytes(), TYPES[target.suffix])

        def _send(self, status: HTTPStatus, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def make_server(port: int = 8787) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(port))


def serve(port: int = 8787, open_browser: bool = True) -> None:
    server = make_server(port)
    url = f"http://localhost:{port}"
    print(f"FOMO desk page at {url}  (paper trading only; this computer only)")
    print("Keep this window open while you use it. Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
