"""The intel board, the agreement map, the manager's briefing and the Team room."""

import json
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from hedge_fund import intel
from hedge_fund.backtesting.test_fund import FakeAnalyst, FakeDataClient, SERIES
from hedge_fund.briefing import digest, latest_briefing, SYSTEM, write_briefing
from hedge_fund.data import sessions
from hedge_fund.fund import Fund, FundSpec
from hedge_fund.paper import deploy, tick


@pytest.fixture(autouse=True)
def fake_analyst(monkeypatch):
    from hedge_fund.signals import ALPHA_MODEL_REGISTRY
    monkeypatch.setitem(ALPHA_MODEL_REGISTRY, "a", FakeAnalyst)
    monkeypatch.setitem(ALPHA_MODEL_REGISTRY, "b", FakeAnalyst)


@pytest.fixture
def clock(monkeypatch):
    def set_today(day):
        y, m, d = map(int, day.split("-"))

        class Clock:
            @staticmethod
            def now(tz=None):
                return datetime(y, m, d, 12, tzinfo=tz)
        monkeypatch.setattr(sessions, "datetime", Clock)
    return set_today


SPEC = dict(schema_version=2, name="team-fund", capital=100_000, rebalance="weekly",
            strategies=[{"name": "solo", "models": [{"name": "a"}, {"name": "b"}], "blend": {"mode": "long_short"}}],
            risk={"max_position_pct": 1, "max_gross_exposure": 1})


def build_fund(spec):
    return Fund(spec, models={"solo": [FakeAnalyst("a", {"AAPL": 0.8}), FakeAnalyst("b", {"AAPL": 0.1})]})


def _fund(tmp_path, clock):
    directory = deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=tmp_path / "paper")
    clock("2024-06-04")
    tick(directory, FakeDataClient(SERIES), build_fund=build_fund)
    return directory


def test_stock_notes_carry_strong_views_and_the_managers_plan(tmp_path, clock):
    notes = intel.stock_notes(_fund(tmp_path, clock))
    views = [n for n in notes if n["agent"] != "Manager"]
    assert [(n["subject"], n["stance"], n["strength"]) for n in views] == [("AAPL", "bull", 0.8)]  # b's 0.1 is noise
    plan = next(n for n in notes if n["agent"] == "Manager")
    assert plan["text"].startswith("Plan: AAPL") and plan["desk"] == "alpha"


def _event(kind, symbol, votes, t="2026-10-24T10:00:00+00:00", text="x"):
    return {"t": t, "kind": kind, "symbol": symbol, "text": text,
            "votes": [{"agent": a, "verdict": v, "text": f"{a} says {v}", "multiplier": 1.0} for a, v in votes]}


def test_agreement_map_shows_consensus_splits_and_vetoes():
    events = [
        _event("buy", "WIF", [("Copy Scout", "boost"), ("Crowd Count", "boost"), ("Pool Guard", "go"), ("Market Shield", "caution")]),
        _event("skip", "PNUT", [("Copy Scout", "go"), ("Too-Late Check", "veto")]),
    ]
    notes = intel.crypto_notes(events, ["SOL down 9% in a day"])
    notes.append({"t": "2026-10-24T11:00:00", "desk": "alpha", "agent": "Momentum Runner", "kind": "stock", "subject": "AAPL",
                  "stance": "bull", "strength": 0.6, "text": "up"})
    notes.append({"t": "2026-10-24T11:00:00", "desk": "alpha", "agent": "Steady Hand", "kind": "stock", "subject": "AAPL",
                  "stance": "bear", "strength": 0.6, "text": "jumpy"})
    rows = {r["subject"]: r for r in intel.agreement(notes)}
    assert rows["WIF"]["verdict"] == "Leaning buy" and rows["WIF"]["caution"] == ["Market Shield"]
    assert rows["WIF"]["bull"] == ["Copy Scout", "Crowd Count"] and rows["WIF"]["clear"] == ["Pool Guard"]
    assert rows["PNUT"]["verdict"] == "Blocked" and rows["PNUT"]["veto"] == ["Too-Late Check"]
    assert rows["AAPL"]["verdict"] == "Split"
    roster = {a["agent"]: a for a in intel.roster(notes)}
    assert roster["Too-Late Check"]["role"].startswith("Stops us buying") and roster["Manager"]["kind"] == "manager"
    assert any(n["text"] == "Risk-off: SOL down 9% in a day." for n in notes)


class FakeLLM:
    model = "fake-1"

    def __init__(self):
        self.seen = None

    def complete(self, system, user):
        self.seen = (system, user)
        return "## Where the team agrees\nWIF.\n## Where it's split\nAAPL.\n## Risks right now\nSOL.\n## What I'd watch next\nLag."


def test_briefing_reads_only_the_board_and_is_saved(tmp_path):
    board = {"notes": intel.crypto_notes([_event("buy", "WIF", [("Copy Scout", "go")])]),
             "agreement": [{"subject": "WIF", "verdict": "Leaning buy", "bull": ["Copy Scout"], "bear": [], "veto": [], "caution": []}]}
    funds = [{"name": "alpha", "mandate": "momentum-leaders", "nav": 101_000, "return_pct": 0.01, "lead": 0.004, "sessions": 9}]
    desk = {"nav": 1040, "return_pct": 0.04, "sol_return_pct": 0.01, "copies": 3, "positions": [1], "avg_lag": 0.03,
            "risk_off": [], "heat": 1.2}
    text = digest(board, funds, desk)
    assert "alpha (momentum-leaders)" in text and "FOMO copy desk" in text and "WIF: Leaning buy" in text
    llm = FakeLLM()
    out = write_briefing(llm, board, funds, desk, tmp_path)
    assert llm.seen[0] == SYSTEM and "Never invent" in SYSTEM and out["model"] == "fake-1"
    assert latest_briefing(tmp_path)["text"].startswith("## Where the team agrees")
    assert latest_briefing(tmp_path / "none") is None


def test_team_room_adds_up_every_desk_and_writes_a_briefing(tmp_path, clock, monkeypatch):
    from hedge_fund.web.server import ApiError, Dashboard
    from hedge_fund.fomo.test_fomo import FakeDex, FakeRPC
    _fund(tmp_path, clock)
    (tmp_path / "mandates").mkdir()

    @contextmanager
    def data():
        yield FakeDataClient(SERIES)
    llm = FakeLLM()
    app = Dashboard(tmp_path / "paper", tmp_path / "mandates", data=data, build_fund=build_fund, save_key=lambda *a: None,
                    fomo_sources={"rpc": FakeRPC(), "dex": FakeDex(), "scout": None, "reviewers": None,
                                  "bitquery": lambda: NS(window=lambda s, t: [])},
                    llm_factory=lambda: llm)
    team = app.team()
    assert team["capital"] == 100_000 + 1_000 and [d["name"] for d in team["desks"]] == ["alpha", "FOMO desk"]
    assert any(n["agent"] == "Manager" for n in team["notes"]) and team["briefing"] is None
    app.start_briefing()
    for _ in range(200):
        if app._briefing_job["state"] != "running":
            break
        import time
        time.sleep(0.01)
    assert app._briefing_job["state"] == "done" and app.team()["briefing"]["model"] == "fake-1"
    keyless = Dashboard(tmp_path / "paper", tmp_path / "mandates", data=data, save_key=lambda *a: None)
    monkeypatch.setenv("HEDGE_FUND_LLM_MODEL", "claude-opus-5-5")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ApiError) as exc:
        keyless.start_briefing()
    assert exc.value.status == 428


def test_stock_market_shield_is_shared_with_the_fomo_desk(tmp_path, clock):
    from hedge_fund.web.server import Dashboard
    from hedge_fund.paper import Ledger
    directory = _fund(tmp_path, clock)
    (tmp_path / "mandates").mkdir()
    app = Dashboard(tmp_path / "paper", tmp_path / "mandates", save_key=lambda *a: None)
    assert app.stock_flags() == []
    # Pretend the latest plan was cut by the market filter.
    ledger = Ledger(directory)
    path = ledger.path(ledger.sessions()[-1])
    data = json.loads(path.read_text())
    data["decision"]["clamps"].append({"limit": "market_filter", "ticker": None, "before": 1.0, "after": 0.3})
    path.write_text(json.dumps(data))
    app._flags_cache = (0.0, [])
    assert app.stock_flags() and "S&P 500" in app.stock_flags()[0]


def test_safety_checks_clear_rather_than_cheer_and_votes_stay_out_of_the_feed():
    events = [_event("buy", "BONK", [("Copy Scout", "boost"), ("Crowd Count", "boost"), ("Pool Guard", "go"),
                                     ("Too-Late Check", "go"), ("Market Shield", "go")])]
    board = intel.board([], events)
    [row] = board["agreement"]
    assert row["verdict"] == "Team agrees: buy" and sorted(row["clear"]) == ["Market Shield", "Pool Guard", "Too-Late Check"]
    assert [n["agent"] for n in board["notes"]] == ["Manager"] and len(board["notes"][0]["votes"]) == 5
    assert {a["agent"] for a in board["agents"]} >= {"Copy Scout", "Pool Guard", "Manager"}
