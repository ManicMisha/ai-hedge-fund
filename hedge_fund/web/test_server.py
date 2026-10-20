"""The web dashboard — reads, runs, the kill switch, keys, and the guards."""

import json
import socket
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime

import pytest
import yaml

from hedge_fund.backtesting.test_fund import FakeAnalyst, FakeDataClient, SERIES
from hedge_fund.data import sessions
from hedge_fund.fund import Fund, FundSpec
from hedge_fund.paper import deploy, Ledger
from hedge_fund.web.server import Dashboard, make_server


@pytest.fixture(autouse=True)
def registered_fakes(monkeypatch):
    from hedge_fund.signals import ALPHA_MODEL_REGISTRY
    monkeypatch.setitem(ALPHA_MODEL_REGISTRY, "a", FakeAnalyst)


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("FINANCIAL_DATASETS_API_KEY", "fd-secret-value")
    monkeypatch.setenv("HEDGE_FUND_LLM_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")


@pytest.fixture
def clock(monkeypatch):
    def set_today(day: str, hour: int = 12):
        y, m, d = map(int, day.split("-"))
        class Clock:
            @staticmethod
            def now(tz):
                return datetime(y, m, d, hour, tzinfo=tz)
        monkeypatch.setattr(sessions, "datetime", Clock)
    return set_today


SPEC = dict(schema_version=2, name="paper-fund",
            strategies=[{"name": "solo", "models": [{"name": "a"}], "blend": {"mode": "long_short"}}],
            risk={"max_position_pct": 1, "max_gross_exposure": 1}, capital=100_000)


def build_fund(spec):
    return Fund(spec, models={"solo": [FakeAnalyst("a", {"AAPL": 1})]})


@contextmanager
def fake_data():
    yield FakeDataClient(SERIES)


@pytest.fixture
def dash(tmp_path):
    mandates = tmp_path / "mandates"
    mandates.mkdir()
    (mandates / "solo.yaml").write_text(yaml.safe_dump(SPEC))
    saved = {}
    app = Dashboard(tmp_path / "paper", mandates, data=fake_data, build_fund=build_fund,
                    save_key=lambda var, value: saved.__setitem__(var, value))
    app.saved = saved
    return app


@pytest.fixture
def http(dash):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = make_server(dash, port)
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(method, path, body=None, headers=None):
        hdrs = {"Host": f"localhost:{port}", "X-AIHF": "1", "Content-Type": "application/json"}
        hdrs.update(headers or {})
        hdrs = {k: v for k, v in hdrs.items() if v is not None}
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if path.startswith("/api") else raw.decode())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    yield call
    server.shutdown()
    server.server_close()


def _ticked_fund(dash, clock, ticks=("2024-06-04", "2024-06-05", "2024-06-06")):
    from hedge_fund.paper import tick
    directory = deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=dash.paper_root)
    for today in ticks:
        clock(today)
        tick(directory, FakeDataClient(SERIES), build_fund=build_fund)
    return directory


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

def test_state_lists_funds_and_plans_and_never_returns_a_key(dash, clock):
    _ticked_fund(dash, clock)
    state = dash.state()
    assert state["data_key_set"] is True
    assert state["model"]["id"] == "claude-opus-5-5" and state["model"]["key_set"] is True
    assert [m["file"] for m in state["mandates"]] == ["solo"]
    assert state["mandates"][0]["strategies"][0]["analysts"][0]["id"] == "a"
    [fund] = state["funds"]
    assert fund["name"] == "alpha" and fund["sessions"] == 3 and fund["halted"] is None
    text = json.dumps(state)
    assert "fd-secret-value" not in text and "anthropic-secret-value" not in text


def test_fund_detail_carries_series_holdings_views_and_the_log(dash, clock):
    _ticked_fund(dash, clock)
    detail = dash.fund("alpha")
    assert [p["session"] for p in detail["series"]] == ["2024-06-03", "2024-06-04", "2024-06-05"]
    assert detail["holdings"] and detail["holdings"][0]["ticker"] == "AAPL"
    assert detail["holdings"][0]["shares"] > 0
    view = detail["view"]
    assert view and view["signals"][0]["analyst"] == "a"
    assert view["signals"][0]["stance"] == "bullish" and view["signals"][0]["confidence"] == 100
    trades = [e for e in detail["log"] if e["kind"] == "trade"]
    assert trades and trades[0]["side"] == "buy" and trades[0]["ticker"] == "AAPL"
    assert detail["run"] == {"state": "idle"}
    assert detail["max_drawdown_pct"] <= 0


def test_a_fund_that_never_ran_reads_cleanly(dash):
    deploy("fresh", FundSpec(**SPEC), ["AAPL"], root=dash.paper_root)
    detail = dash.fund("fresh")
    assert detail["sessions"] == 0 and detail["nav"] == 100_000
    assert detail["series"] == [] and detail["holdings"] == [] and detail["view"] is None


# ---------------------------------------------------------------------------
# HTTP: page, create, plan, run, halt
# ---------------------------------------------------------------------------

def test_serves_the_page(http):
    status, page = http("GET", "/")
    assert status == 200 and "<title>" in page


def test_create_fund_validates_its_inputs(http, dash):
    assert http("POST", "/api/funds", {"name": "../evil", "mandate": "solo", "universe": "AAPL"})[0] == 400
    assert http("POST", "/api/funds", {"name": "ok", "mandate": "../solo", "universe": "AAPL"})[0] == 400
    assert http("POST", "/api/funds", {"name": "ok", "mandate": "nope", "universe": "AAPL"})[0] == 400
    assert http("POST", "/api/funds", {"name": "ok", "mandate": "solo", "universe": " , "})[0] == 400
    status, fund = http("POST", "/api/funds", {"name": "beta", "mandate": "solo",
                                               "universe": "aapl, msft", "capital": "5000"})
    assert status == 200 and fund["universe"] == ["AAPL", "MSFT"] and fund["capital"] == 5000
    assert http("POST", "/api/funds", {"name": "beta", "mandate": "solo", "universe": "AAPL"})[0] == 409
    assert http("GET", "/api/funds/missing")[0] == 404


def test_plan_then_run_advances_the_ledger_once(http, dash, clock):
    deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=dash.paper_root)
    clock("2024-06-04")
    status, plan = http("GET", "/api/funds/alpha/plan")
    assert status == 200 and plan["due"] == "2024-06-03" and plan["decides"] is True
    status, job = http("POST", "/api/funds/alpha/run", {"mode": "run"})
    assert status == 200 and job["state"] == "running"
    done = dash.runs.wait("alpha")
    assert done["state"] == "done" and done["session"] == "2024-06-03" and done["decided"] is True
    assert Ledger(dash.paper_root / "alpha").sessions() == ["2024-06-03"]
    # Nothing new has closed: a second run says so instead of failing.
    http("POST", "/api/funds/alpha/run", {"mode": "run"})
    assert dash.runs.wait("alpha")["state"] == "nothing_due"
    status, plan = http("GET", "/api/funds/alpha/plan")
    assert plan["due"] is None and plan["can_redo"] is True


def test_halt_blocks_runs_until_resumed(http, dash, clock):
    deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=dash.paper_root)
    clock("2024-06-04")
    status, fund = http("POST", "/api/funds/alpha/halt", {"reason": "checking something"})
    assert status == 200 and fund["halted"] == "checking something"
    status, err = http("POST", "/api/funds/alpha/run", {"mode": "run"})
    assert status == 409 and "halted" in err["error"]
    status, fund = http("POST", "/api/funds/alpha/resume")
    assert status == 200 and fund["halted"] is None
    assert http("POST", "/api/funds/alpha/run", {"mode": "run"})[0] == 200
    assert dash.runs.wait("alpha")["state"] == "done"


def test_run_needs_only_the_keys_the_fund_uses(http, dash, monkeypatch, clock):
    deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=dash.paper_root)
    # Financial Datasets chosen explicitly but no key: refuse by name.
    monkeypatch.setenv("DATA_SOURCE", "financialdatasets")
    monkeypatch.delenv("FINANCIAL_DATASETS_API_KEY")
    status, err = http("POST", "/api/funds/alpha/run", {"mode": "run"})
    assert status == 428 and "Financial Datasets" in err["error"]
    # No key and no choice: free Yahoo prices, nothing to ask for.
    monkeypatch.delenv("DATA_SOURCE")
    assert dash.state()["data_source"] == "yahoo" and dash.state()["data_ready"] is True
    # A maths-only fund never needs an AI key...
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    clock("2024-06-04")
    assert http("POST", "/api/funds/alpha/run", {"mode": "run"})[0] == 200
    assert dash.runs.wait("alpha")["state"] == "done"
    # ...but a fund with an AI analyst does.
    ai = {**SPEC, "strategies": [{"name": "s", "models": [{"name": "buffett"}], "blend": {"mode": "long_only"}}]}
    deploy("brains", FundSpec(**ai), ["AAPL"], root=dash.paper_root)
    status, err = http("POST", "/api/funds/brains/run", {"mode": "run"})
    assert status == 428 and "Anthropic" in err["error"]


# ---------------------------------------------------------------------------
# keys and model
# ---------------------------------------------------------------------------

def test_keys_and_model_accept_only_known_names(http, dash):
    assert http("POST", "/api/keys", {"env_var": "PATH", "value": "x"})[0] == 400
    assert http("POST", "/api/keys", {"env_var": "OPENAI_API_KEY", "value": "a\nb"})[0] == 400
    assert http("POST", "/api/keys", {"env_var": "OPENAI_API_KEY", "value": " sk-123 "})[0] == 200
    assert dash.saved == {"OPENAI_API_KEY": "sk-123"}
    assert http("POST", "/api/model", {"id": "not-a-model"})[0] == 400
    assert http("POST", "/api/model", {"id": "deepseek-flash"})[0] == 200
    assert dash.saved["HEDGE_FUND_LLM_MODEL"] == "deepseek-flash"


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def test_refuses_foreign_hosts_and_unmarked_or_cross_site_writes(http, dash):
    deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=dash.paper_root)
    assert http("GET", "/api/state", headers={"Host": "evil.example:80"})[0] == 403
    assert http("POST", "/api/funds/alpha/halt", {"reason": "x"}, headers={"X-AIHF": None})[0] == 403
    assert http("POST", "/api/funds/alpha/halt", {"reason": "x"},
                headers={"Origin": "http://evil.example"})[0] == 403
    assert Ledger(dash.paper_root / "alpha").halted() is None


def test_trackers_ask_for_sec_contact_details_and_validate_them(http, dash, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    trackers = {**SPEC, "strategies": [{"name": "s", "models": [{"name": "insiders"}, {"name": "social_watch"}],
                                        "blend": {"mode": "long_only", "method": "slot_weighted"}}]}
    deploy("copycat", FundSpec(**trackers), ["AAPL"], root=dash.paper_root)
    status, err = http("POST", "/api/funds/copycat/run", {"mode": "run"})
    assert status == 428 and "name and email" in err["error"]
    # Hype Watch is optional: no xAI key is not a blocker.
    monkeypatch.setenv("SEC_USER_AGENT", "Jane Smith jane@example.com")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    dash._require_keys(FundSpec(**trackers))
    # The SEC value must look like "Name email"; it is saved as given.
    assert http("POST", "/api/keys", {"env_var": "SEC_USER_AGENT", "value": "jane"})[0] == 400
    assert http("POST", "/api/keys", {"env_var": "SEC_USER_AGENT", "value": "Jane Smith jane@example.com"})[0] == 200
    assert http("POST", "/api/keys", {"env_var": "BARGO_API_KEY", "value": "bg-123"})[0] == 200
    assert dash.saved == {"SEC_USER_AGENT": "Jane Smith jane@example.com", "BARGO_API_KEY": "bg-123"}
    state = dash.state()
    assert {"sec_set", "bargo_set", "xai_set"} <= set(state)
    assert json.dumps(state).count("jane@example.com") == 0  # the dashboard never echoes it back
