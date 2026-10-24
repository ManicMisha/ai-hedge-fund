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


def test_state_ranks_funds_and_carries_their_game_progress(http, dash, clock):
    deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=dash.paper_root)
    deploy("beta", FundSpec(**SPEC), ["AAPL"], root=dash.paper_root)
    clock("2024-06-04")
    assert http("POST", "/api/funds/alpha/run", {"mode": "run"})[0] == 200
    assert dash.runs.wait("alpha")["state"] == "done"
    status, state = http("GET", "/api/state")
    assert status == 200 and state["leaderboard"] == ["alpha", "beta"]  # played before unplayed
    alpha = next(f for f in state["funds"] if f["name"] == "alpha")
    assert alpha["place"] == 1 and alpha["lead"] is not None
    assert alpha["game"]["xp"] > 0 and any(b["id"] == "opening_bell" and b["unlocked"] for b in alpha["game"]["badges"])
    beta = next(f for f in state["funds"] if f["name"] == "beta")
    assert beta["place"] is None and beta["game"]["level"] == 1


# ---------------------------------------------------------------------------
# auto-play, alerts, settings, backtests
# ---------------------------------------------------------------------------

def test_autoplay_catches_every_fund_up_and_reports_why_it_skipped(dash, clock, monkeypatch):
    from hedge_fund.paper import tick
    directory = deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=dash.paper_root)
    deploy("frozen", FundSpec(**SPEC), ["AAPL"], root=dash.paper_root)
    Ledger(dash.paper_root / "frozen").halt("paused")
    clock("2024-06-04")
    tick(directory, FakeDataClient(SERIES), build_fund=build_fund)
    clock("2024-06-08")
    rows = {r["fund"]: r for r in dash.autoplay.check()}
    assert rows["alpha"]["status"] == "played" and rows["alpha"]["played"] == 4
    assert rows["frozen"]["status"] == "frozen"
    assert Ledger(directory).sessions()[-1] == "2024-06-07"
    state = dash.state()["autoplay"]
    assert state["enabled"] is False and state["last_check"] and state["checking"] is False
    assert dash.autoplay.check()[0]["status"] == "up_to_date"


def test_settings_and_alert_endpoints(http, dash, monkeypatch):
    status, out = http("POST", "/api/settings", {"autoplay": True, "alerts": {"phone": True, "kinds": ["pump"]}})
    assert status == 200 and out["settings"]["autoplay"] is True and out["autoplay"]["enabled"] is True
    assert http("POST", "/api/settings", {"autoplay": "please"})[0] == 400
    assert http("POST", "/api/keys", {"env_var": "NTFY_TOPIC", "value": "short"})[0] == 400
    assert http("POST", "/api/keys", {"env_var": "NTFY_TOPIC", "value": "aihf-k3j4h5g6"})[0] == 200
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert http("POST", "/api/alerts/test", {})[0] == 428
    pushed = []
    dash.push = pushed.append
    monkeypatch.setenv("NTFY_TOPIC", "aihf-k3j4h5g6")
    assert http("POST", "/api/alerts/test", {})[0] == 200 and "Test alert" in pushed[0]["title"]
    status, feed = http("GET", "/api/alerts")
    assert status == 200 and feed["alerts"] == [] and feed["phone_ready"] is True and feed["settings"]["kinds"] == ["pump"]


def test_a_failed_manual_play_pings_the_phone(dash, clock, monkeypatch):
    directory = deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=dash.paper_root)
    monkeypatch.setenv("NTFY_TOPIC", "aihf-k3j4h5g6")
    dash.update_settings({"alerts": {"phone": True}})
    pushed = []
    dash.push = pushed.append
    dash.send_alerts(directory)  # first sight: history marked, nothing sent
    clock("2024-06-04")

    def broken(spec):
        raise RuntimeError("bad day")
    dash.build_fund = broken
    dash.start_run("alpha", {"mode": "run"})
    assert dash.runs.wait("alpha")["state"] == "failed"
    Ledger(directory).log_event("tick_failed", session="2024-06-03", error="bad day")
    dash.send_alerts(directory)
    assert [a["kind"] for a in pushed] == ["frozen"]


def test_backtest_routes(http, dash, clock, tmp_path):
    clock("2024-06-22")
    lab = Dashboard(dash.paper_root, dash.mandates_root, data=fake_data, build_fund=build_fund,
                    backtest_fund_builder=build_fund, save_key=lambda *a: None)
    status = lab.start_backtest({"mandate": "solo", "universe": "AAPL", "years": 1})
    assert status["state"] == "running"
    done = lab.lab.wait()
    assert done["state"] == "done" and done["result"]["mandate"] == "paper-fund"
    listing = lab.backtest_list()
    assert listing["runs"][0]["id"] == done["result"]["id"]
    assert len(lab.backtest(done["result"]["id"])["curve"]["nav"]) == done["total"]
    for bad in ({"mandate": "solo", "universe": "AAPL", "years": 2}, {"mandate": "nope", "universe": "AAPL", "years": 1}):
        with pytest.raises(Exception):
            lab.start_backtest(bad)
    ai_plan = {**SPEC, "strategies": [{"name": "s", "models": [{"name": "buffett"}], "blend": {"mode": "long_only"}}]}
    (dash.mandates_root / "brainy.yaml").write_text(yaml.safe_dump(ai_plan))
    from hedge_fund.web.server import ApiError
    with pytest.raises(ApiError) as exc:
        lab.start_backtest({"mandate": "brainy", "universe": "AAPL", "years": 1})
    assert exc.value.status == 409 and "AI" in exc.value.message
    assert http("GET", "/api/backtests")[0] == 200 and http("GET", "/api/backtests/..%2Fx")[0] == 404


# ---------------------------------------------------------------------------
# FOMO desk
# ---------------------------------------------------------------------------

def test_fomo_desk_routes_settings_and_alert_feed(http, dash, monkeypatch, tmp_path):
    from hedge_fund.fomo.sources import Quote, SOL_MINT, Swap
    from hedge_fund.fomo.test_fomo import BONK, FakeDex, FakeRPC, W
    rpc, dex = FakeRPC(), FakeDex()
    app = Dashboard(dash.paper_root, dash.mandates_root, data=fake_data, build_fund=build_fund, save_key=lambda *a: None,
                    fomo_sources={"rpc": rpc, "dex": dex, "scout": None, "reviewers": None,
                                  "bitquery": lambda: type("Q", (), {"window": lambda self, s, t: []})()})
    status = app.fomo_status()
    assert status["settings"]["enabled"] is False and status["nav"] == 1000 and status["bitquery_set"] is False
    with pytest.raises(Exception):
        app.fomo_settings({"slot_pct": 0.9})
    assert app.fomo_settings({"slot_pct": 0.05, "capital": 2000})["config"]["slot_pct"] == 0.05
    assert app.fomo_reset({})["capital"] == 2000
    desk = app.fomo.desk()
    desk.state["followed"] = [W]
    app.fomo.cycle()  # first look sets the cursor
    rpc.sigs[W] = ["s1"]
    rpc.swaps_by_sig["s1"] = [Swap(W, "s1", 0, BONK, "buy", 1_000_000, SOL_MINT, 0.5)]
    dex.book[BONK] = Quote(BONK, "BONK", 0.0001, 300_000.0, 0)
    pushed = []
    app.push = pushed.append
    monkeypatch.setenv("NTFY_TOPIC", "aihf-k3j4h5g6")
    app.update_settings({"alerts": {"phone": True}})
    [event] = app.fomo.cycle()
    assert event["kind"] == "buy" and pushed and pushed[0]["title"] == "FOMO copy: bought BONK"
    feed = app.alerts()["alerts"]
    assert feed[0]["fund"] == "FOMO desk" and feed[0]["kind"] == "fomo"
    assert http("GET", "/api/fomo")[0] == 200
    assert http("POST", "/api/keys", {"env_var": "SOLANA_RPC_URL", "value": "http://insecure"})[0] == 400
    assert http("POST", "/api/keys", {"env_var": "BITQUERY_TOKEN", "value": "ory_at_abc"})[0] == 200
