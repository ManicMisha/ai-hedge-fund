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
import os
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

from hedge_fund import alerts, intel
from hedge_fund.briefing import latest_briefing, write_briefing
from hedge_fund.autoplay import catch_up
from hedge_fund.fomo import Desk, event_alert
from hedge_fund.fomo import leaderboard as fomo_board
from hedge_fund.data.protocol import DataClient
from hedge_fund.fund import Fund, FundSpec, load_spec, normalize_universe
from hedge_fund.llm import load_api_models, provider_for
from hedge_fund.paper import (
    deploy,
    Ledger,
    list_deployed,
    load_deployed,
    next_session,
    NothingDue,
    redo,
    tick,
    validate_fund_name,
)
from hedge_fund.pipeline.session import FundHalted, is_rebalance_session
from hedge_fund.settings import load_settings, save_settings
from hedge_fund.web import backtests, views

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
        push: Callable[[dict], None] | None = None,
        backtest_fund_builder: Callable[[FundSpec], Fund] | None = None,
        fomo_sources: dict | None = None,
        llm_factory: Callable[[], object] | None = None,
    ) -> None:
        self.paper_root = Path(paper_root)
        self.mandates_root = Path(mandates_root)
        home = self.paper_root.parent  # ~/.hedge-fund in real use; a temp folder in tests
        self.research_root = home / "research"
        self.settings_path = home / "settings.json"
        self.sent_path = home / "alerts-sent.json"
        self.data = data
        self.build_fund = build_fund
        self.push = push or alerts.push_phone
        self.runs = Runs()
        self.autoplay = AutoPlay(self)
        self.lab = backtests.BacktestRunner(self.research_root, data, backtest_fund_builder)
        self.fomo = FomoRunner(self, home / "fomo", fomo_sources or {})
        self.briefings_root = home / "briefings"
        self.llm_factory = llm_factory
        self._briefing_job: dict = {"state": "idle"}
        self._flags_cache: tuple[float, list[str]] = (0.0, [])
        if save_key is None:
            from hedge_fund.tui.keys import save_credential as save_key
        self.save_key = save_key

    # ---- reads ------------------------------------------------------------

    def state(self) -> dict:
        return {**views.app_state(self.paper_root, self.mandates_root),
                "autoplay": {**self.autoplay.status(), "enabled": self.settings()["autoplay"]},
                "fomo_card": {**self.fomo.card(), "enabled": self.settings()["fomo"]["enabled"]},
                "phone_ready": alerts.ntfy_target() is not None}

    def fund(self, name: str) -> dict:
        directory = self._fund_dir(name)
        return {**views.fund_detail(directory, self.research_root), "run": self.runs.status(name)}

    def settings(self) -> dict:
        return load_settings(self.settings_path)

    def alerts(self) -> dict:
        rows = alerts.all_alerts(list_deployed(self.paper_root), research_dir=self.research_root) + self.fomo.alert_rows()
        rows.sort(key=lambda a: (a["session"], a["id"]), reverse=True)
        return {"alerts": rows[:100],
                "settings": self.settings()["alerts"], "phone_ready": alerts.ntfy_target() is not None}

    def stock_flags(self) -> list[str]:
        """The stock side's Market Shield, shared with the FOMO desk: if any
        fund's latest plan was cut because the S&P 500 is below its 200-day
        average, crypto copies are halved too. Cached for ten minutes."""
        import time as _time
        at, flags = self._flags_cache
        if _time.time() - at < 600:
            return flags
        flags = []
        for directory in list_deployed(self.paper_root):
            try:
                records = Ledger(directory).records()
            except Exception:
                continue
            decision = next((r.decision for r in reversed(records) if r.decision is not None), None)
            if decision is not None and any(c.limit == "market_filter" for c in decision.clamps):
                flags = ["S&P 500 below its 200-day average (stock desk's Market Shield)"]
                break
        self._flags_cache = (_time.time(), flags)
        return flags

    def team(self) -> dict:
        """Everything the Team room shows: the board, every desk's money, the briefing."""
        funds = [views.fund_summary(d) for d in list_deployed(self.paper_root)]
        desk = self.fomo.status()
        with self.fomo._lock:
            events = list(self.fomo.desk().state["events"])
        board = intel.board(list_deployed(self.paper_root), events, desk.get("risk_off"))
        live = [f for f in funds if not f.get("error")]
        capital = sum(f["capital"] for f in live) + desk["capital"]
        value = sum(f["nav"] for f in live) + desk["nav"]
        return {**board, "capital": capital, "value": value, "return_pct": value / capital - 1 if capital else 0.0,
                "desks": [{"name": f["name"], "kind": "stocks", "nav": f["nav"], "return_pct": f["return_pct"], "lead": f.get("lead")} for f in live]
                + [{"name": "FOMO desk", "kind": "crypto", "nav": desk["nav"], "return_pct": desk["return_pct"],
                    "lead": desk["return_pct"] - desk["sol_return_pct"] if desk.get("sol_return_pct") is not None else None}],
                "briefing": latest_briefing(self.briefings_root), "briefing_job": dict(self._briefing_job),
                "model": views.current_model()}

    def start_briefing(self) -> dict:
        if self._briefing_job.get("state") == "running":
            raise ApiError(HTTPStatus.CONFLICT, "The manager is already writing a briefing.")
        model = views.current_model()
        if self.llm_factory is None and model["key_env"] and not model["key_set"]:
            raise ApiError(HTTPStatus.PRECONDITION_REQUIRED, f"Add your {model['provider']} key for {model['label']} first (Keys & model).")
        self._briefing_job = {"state": "running", "started": _now()}

        def work() -> None:
            try:
                from hedge_fund.llm import make_llm
                llm = (self.llm_factory or (lambda: make_llm(timeout=120)))()
                team = self.team()
                funds = [views.fund_summary(d) for d in list_deployed(self.paper_root)]
                write_briefing(llm, team, funds, self.fomo.status(), self.briefings_root)
                self._briefing_job = {"state": "done", "finished": _now()}
            except Exception as exc:
                self._briefing_job = {"state": "failed", "message": f"{type(exc).__name__}: {exc}"[:300]}

        threading.Thread(target=work, name="aihf-briefing", daemon=True).start()
        return {"briefing_job": dict(self._briefing_job)}

    def fomo_status(self) -> dict:
        return {**self.fomo.status(), "settings": self.settings()["fomo"],
                "bitquery_set": bool(os.environ.get("BITQUERY_TOKEN", "").strip()),
                "rpc_custom": bool(os.environ.get("SOLANA_RPC_URL", "").strip())}

    def fomo_settings(self, body: dict) -> dict:
        self.update_settings({"fomo": body})
        self.fomo.configure(self.settings()["fomo"])
        if body.get("enabled") is True:
            self.fomo.poke()
        return self.fomo_status()

    def fomo_run(self) -> dict:
        self.fomo.poke(force=True)
        return self.fomo_status()

    def fomo_reset(self, body: dict) -> dict:
        capital = body.get("capital")
        if capital is not None:
            self.fomo_settings({"capital": capital})
        self.fomo.reset()
        return self.fomo_status()

    def backtest_list(self) -> dict:
        return {"status": self.lab.status(), "runs": backtests.list_summaries(self.research_root)}

    def backtest(self, run_id: str) -> dict:
        try:
            return backtests.load_summary(self.research_root, run_id)
        except (OSError, ValueError) as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, "no such backtest") from exc

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
            try:
                with self.data() as data:
                    record = (redo(directory, data, build_fund=self.build_fund) if mode == "redo"
                              else tick(directory, data, build_fund=self.build_fund))
            finally:
                self.send_alerts(directory)
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
        if env_var == "SOLANA_RPC_URL" and not re.fullmatch(r"https://[^\s]+", value):
            raise ApiError(HTTPStatus.BAD_REQUEST, "paste the full https:// address from your Solana data provider")
        if env_var == views.NTFY_VAR and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value):
            raise ApiError(HTTPStatus.BAD_REQUEST, "a topic is 8 to 64 letters, numbers, dashes or underscores")
        if env_var == views.SEC_VAR and not re.fullmatch(r"[^@\s][^@]*\s+\S+@\S+\.\S+", value):
            raise ApiError(HTTPStatus.BAD_REQUEST, "the SEC wants your name and email, like: Jane Smith jane@example.com")
        self.save_key(env_var, value)
        return self.state()

    def set_model(self, body: dict) -> dict:
        model_id = str(body.get("id", ""))
        if model_id not in {mid for _, mid, _ in load_api_models()} or provider_for(model_id) is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"unknown model {model_id!r}")
        self.save_key(views.MODEL_VAR, model_id)
        return self.state()

    def update_settings(self, body: dict) -> dict:
        try:
            saved = save_settings(body, self.settings_path)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        if body.get("autoplay") is True:
            self.autoplay.poke()  # check straight away, not in half an hour
        return {"settings": saved, **self.state()}

    def test_alert(self) -> dict:
        if alerts.ntfy_target() is None:
            raise ApiError(HTTPStatus.PRECONDITION_REQUIRED, "Add a phone alert topic first.")
        try:
            self.push({"title": "Test alert from your AI hedge fund", "level": "info",
                       "body": "Phone alerts work. You'll get pings like this for insider buys, pump alarms and big days."})
        except Exception as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"Couldn't send: {exc}") from exc
        return {"sent": True}

    def check_now(self) -> dict:
        self.autoplay.poke(force=True)
        return self.state()

    def start_backtest(self, body: dict) -> dict:
        mandate = self._mandate_path(str(body.get("mandate", "")))
        try:
            spec = load_spec(mandate)
            universe = normalize_universe(re.split(r"[\s,]+", str(body.get("universe", ""))))
            start, end = backtests.window(int(body.get("years", 0)))
        except (ValueError, TypeError) as exc:
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
        self._require_keys(spec)
        uses_ai = any(views.analyst_view(m.name)["kind"] == "ai" for s in spec.strategies for m in s.models)
        if uses_ai and body.get("confirm_ai") is not True:
            raise ApiError(HTTPStatus.CONFLICT, "This plan has AI analysts, so the backtest makes paid AI calls. Confirm the cost first.")
        try:
            return self.lab.start(spec, universe, start, end, mandate.stem)
        except RuntimeError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc

    def cancel_backtest(self) -> dict:
        return self.lab.cancel()

    # ---- background work --------------------------------------------------

    def send_alerts(self, directory: Path) -> list[dict]:
        """Phone pings for anything new; failures never break a run."""
        try:
            return alerts.dispatch(directory, send=self.push if self.settings()["alerts"]["phone"] and
                                   alerts.ntfy_target() else None, sent_path=self.sent_path,
                                   settings=self.settings(), research_dir=self.research_root)
        except Exception:
            return []

    def catch_up_work(self, directory: Path) -> dict:
        try:
            with self.data() as data:
                out = catch_up(directory, data, build_fund=self.build_fund)
        finally:
            self.send_alerts(directory)
        if out["error"] and not out["played"]:
            raise RuntimeError(out["error"])
        return out

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
        """Refuse a run early, by name, when a key it needs is missing."""
        problem = views.missing_keys(spec)
        if problem:
            raise ApiError(HTTPStatus.PRECONDITION_REQUIRED, problem)


# ---------------------------------------------------------------------------
# Auto-play: every fund caught up after each US close, while the app is open
# ---------------------------------------------------------------------------

class AutoPlay:
    """A timer thread. Every half hour (and when switched on) it plays each
    fund's due days, one fund at a time, through the same runs the Play
    button uses, so the page shows progress exactly as for a manual play."""

    def __init__(self, app: "Dashboard", interval: float = 1800.0) -> None:
        self.app, self.interval = app, interval
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._force = False
        self._state: dict = {"checking": False, "last_check": None, "next_check": None, "results": []}
        self._thread: threading.Thread | None = None

    def status(self) -> dict:
        with self._lock:
            return {**self._state, "results": list(self._state["results"])}

    def start(self, first_delay: float = 20.0) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, args=(first_delay,), name="aihf-autoplay", daemon=True)
            self._thread.start()

    def poke(self, force: bool = False) -> None:
        self._force = self._force or force
        self._wake.set()

    def _loop(self, first_delay: float) -> None:
        self._wake.wait(first_delay)
        while True:
            self._wake.clear()
            force, self._force = self._force, False
            if force or self.app.settings()["autoplay"]:
                self.check()
            with self._lock:
                self._state["next_check"] = datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() + self.interval, timezone.utc).isoformat(timespec="seconds")
            self._wake.wait(self.interval)

    def check(self) -> list[dict]:
        with self._lock:
            if self._state["checking"]:
                return []
            self._state["checking"] = True
        results = []
        try:
            for directory in list_deployed(self.app.paper_root):
                name = directory.name
                row: dict = {"fund": name}
                try:
                    if self.app.runs.status(name)["state"] == "running":
                        row.update(status="busy")
                    elif (frozen := Ledger(directory).halted()):
                        row.update(status="frozen", message=frozen)
                    elif (problem := views.missing_keys(load_deployed(directory).spec)):
                        row.update(status="skipped", message=problem)
                    else:
                        self.app.runs.start(name, lambda d=directory: self.app.catch_up_work(d))
                        job = self.app.runs.wait(name, timeout=6 * 3600)
                        played = job.get("played", 0)
                        row.update(status="failed" if job["state"] == "failed" else "played" if played else "up_to_date",
                                   played=played, session=job.get("session"), message=job.get("message") or job.get("error"))
                except Exception as exc:  # one broken fund must not stop the others
                    row.update(status="failed", message=f"{type(exc).__name__}: {exc}")
                results.append(row)
        finally:
            with self._lock:
                self._state.update(checking=False, last_check=_now(), results=results)
        return results


# ---------------------------------------------------------------------------
# FOMO desk: find FOMO's top traders, follow them, paper-copy their trades
# ---------------------------------------------------------------------------

class FomoRunner:
    """Every minute while switched on: save any finished 3-hour window of
    FOMO trades (Bitquery) and re-pick the traders to follow, then read the
    followed wallets' new trades (Solana) and copy them on paper."""

    def __init__(self, app: "Dashboard", root: Path, sources: dict, interval: float = 60.0) -> None:
        self.app, self.root, self.sources, self.interval = app, root, sources, interval
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._force = False
        self._desk: Desk | None = None
        self._busy = False
        self._thread: threading.Thread | None = None

    def desk(self) -> Desk:
        if self._desk is None:
            config = {k: v for k, v in self.app.settings()["fomo"].items() if k != "enabled"}
            sources = {"market_flags": self.app.stock_flags, **self.sources}
            self._desk = Desk(self.root, config, **sources)
        return self._desk

    def configure(self, settings: dict) -> None:
        with self._lock:
            self.desk().config.update({k: v for k, v in settings.items() if k != "enabled"})

    def reset(self) -> None:
        with self._lock:
            self.desk().reset(self.app.settings()["fomo"]["capital"])

    def status(self) -> dict:
        with self._lock:
            return {**self.desk().status(), "busy": self._busy}

    def card(self) -> dict:
        """The few numbers the home screen shows, without rebuilding the leaderboard."""
        with self._lock:
            desk = self.desk()
            nav = desk._nav()
            capital = desk.state["capital"]
            return {"nav": round(nav, 2), "return_pct": nav / capital - 1 if capital else 0.0,
                    "followed": len(desk.state["followed"])}

    def alert_rows(self) -> list[dict]:
        with self._lock:
            events = list(self.desk().state["events"])
        rows = []
        for e in events:
            a = event_alert(e)
            if a:
                rows.append({**a, "id": f"fomo:{e['t']}:{e.get('mint', '')}:{e['kind']}", "fund": "FOMO desk",
                             "session": e["t"][:10], "label": "FOMO copy", "ticker": e.get("symbol")})
        return rows

    def start(self, first_delay: float = 10.0) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, args=(first_delay,), name="aihf-fomo", daemon=True)
            self._thread.start()

    def poke(self, force: bool = False) -> None:
        self._force = self._force or force
        self._wake.set()

    def _loop(self, first_delay: float) -> None:
        self._wake.wait(first_delay)
        while True:
            self._wake.clear()
            force, self._force = self._force, False
            if force or self.app.settings()["fomo"]["enabled"]:
                try:
                    self.cycle()
                except Exception:  # never let the timer die; errors show on the page
                    pass
            self._wake.wait(self.interval)

    def cycle(self) -> list[dict]:
        with self._lock:
            if self._busy:
                return []
            self._busy = True
        try:
            with self._lock:
                desk = self.desk()
            if fomo_board.due_windows(desk.store) or desk.state["last_collect"] is None:
                desk.collect()
            events = desk.poll()
        finally:
            with self._lock:
                self._busy = False
        settings = self.app.settings()
        if settings["alerts"]["phone"] and "fomo" in settings["alerts"]["kinds"] and alerts.ntfy_target():
            for event in events[:5]:
                alert = event_alert(event)
                if alert:
                    try:
                        self.app.push(alert)
                    except Exception:
                        break
        return events


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_FUND_ROUTE = re.compile(r"^/api/funds/([^/]+)(?:/(plan|run|halt|resume))?$")
_BACKTEST_ROUTE = re.compile(r"^/api/backtests/([A-Za-z0-9][A-Za-z0-9._-]*)$")


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
            if path == "/api/alerts":
                return app.alerts()
            if path == "/api/backtests":
                return app.backtest_list()
            if path == "/api/fomo":
                return app.fomo_status()
            if path == "/api/team":
                return app.team()
            bt = _BACKTEST_ROUTE.match(path)
            if bt:
                return app.backtest(bt.group(1))
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
            if path == "/api/settings":
                return app.update_settings(body)
            if path == "/api/alerts/test":
                return app.test_alert()
            if path == "/api/autoplay/check":
                return app.check_now()
            if path == "/api/backtests":
                return app.start_backtest(body)
            if path == "/api/backtests/cancel":
                return app.cancel_backtest()
            if path == "/api/fomo/settings":
                return app.fomo_settings(body)
            if path == "/api/fomo/run":
                return app.fomo_run()
            if path == "/api/fomo/reset":
                return app.fomo_reset(body)
            if path == "/api/team/briefing":
                return app.start_briefing()
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
    app.autoplay.start()
    app.fomo.start()
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
