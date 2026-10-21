"""Auto-play, alerts, the go-live scorecard, settings and dashboard backtests."""

import json
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from hedge_fund import alerts
from hedge_fund.autoplay import catch_up, describe, play_all
from hedge_fund.backtesting.test_fund import FakeAnalyst, FakeDataClient, SERIES, WEEKDAYS
from hedge_fund.data import sessions
from hedge_fund.fund import Fund, FundSpec
from hedge_fund.paper import deploy, Ledger, tick
from hedge_fund.settings import ALERT_KINDS, load_settings, save_settings
from hedge_fund.web import backtests, scorecard


@pytest.fixture(autouse=True)
def fake_analyst(monkeypatch):
    from hedge_fund.signals import ALPHA_MODEL_REGISTRY
    monkeypatch.setitem(ALPHA_MODEL_REGISTRY, "a", FakeAnalyst)


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


SPEC = dict(schema_version=2, name="paper-fund", capital=100_000, rebalance="weekly",
            strategies=[{"name": "solo", "models": [{"name": "a"}], "blend": {"mode": "long_short"}}],
            risk={"max_position_pct": 1, "max_gross_exposure": 1})


def build_fund(spec):
    return Fund(spec, models={"solo": [FakeAnalyst("a", {"AAPL": 1})]})


@contextmanager
def fake_data():
    yield FakeDataClient(SERIES)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def test_settings_default_off_merge_and_validate(tmp_path):
    path = tmp_path / "settings.json"
    assert load_settings(path)["autoplay"] is False and load_settings(path)["alerts"]["kinds"] == list(ALERT_KINDS)
    save_settings({"autoplay": True}, path)
    saved = save_settings({"alerts": {"phone": True, "kinds": ["pump", "insider"]}}, path)
    assert saved["autoplay"] is True and saved["alerts"] == {"phone": True, "kinds": ["insider", "pump"]}
    for bad in ({"autoplay": "yes"}, {"alerts": {"kinds": ["nonsense"]}}, {"alerts": {"phone": 1}}):
        with pytest.raises(ValueError):
            save_settings(bad, path)
    path.write_text("not json")
    assert load_settings(path)["autoplay"] is False


# ---------------------------------------------------------------------------
# auto-play
# ---------------------------------------------------------------------------

def test_catch_up_plays_every_missed_day_in_order(tmp_path, clock):
    directory = deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=tmp_path)
    clock("2024-06-04")
    tick(directory, FakeDataClient(SERIES), build_fund=build_fund)  # records 2024-06-03
    clock("2024-06-08")  # a long weekend away: Tue-Fri still to play
    out = catch_up(directory, FakeDataClient(SERIES), build_fund=build_fund)
    assert out["sessions"] == ["2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07"]
    assert out["error"] is None and out["traded"] is True
    assert catch_up(directory, FakeDataClient(SERIES), build_fund=build_fund)["played"] == 0  # idempotent


def test_catch_up_stops_at_a_failure_and_keeps_what_it_played(tmp_path, clock):
    directory = deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=tmp_path)
    clock("2024-06-04")
    tick(directory, FakeDataClient(SERIES), build_fund=build_fund)
    clock("2024-06-08")

    class Flaky(FakeDataClient):
        def get_prices(self, ticker, start_date, end_date, **kwargs):
            if end_date >= "2024-06-06" and ticker == "AAPL":
                raise RuntimeError("data feed fell over")
            return super().get_prices(ticker, start_date, end_date, **kwargs)
    out = catch_up(directory, Flaky(SERIES), build_fund=build_fund)
    assert out["sessions"] == ["2024-06-04", "2024-06-05"] and "data feed fell over" in out["error"]
    assert Ledger(directory).halted()  # tick froze it, as for a manual play


def test_play_all_skips_frozen_and_keyless_funds(tmp_path, clock):
    for name in ("alpha", "beta", "gamma"):
        deploy(name, FundSpec(**SPEC), ["AAPL"], root=tmp_path)
    Ledger(tmp_path / "beta").halt("on holiday")
    clock("2024-06-04")
    seen = []
    rows = play_all(tmp_path, fake_data, build_fund=build_fund, after_fund=lambda d: seen.append(d.name),
                    check_keys=lambda spec: None)
    assert [r["status"] for r in rows] == ["played", "frozen", "played"] and seen == ["alpha", "beta", "gamma"]
    rows = play_all(tmp_path, fake_data, build_fund=build_fund, check_keys=lambda spec: "Add your key")
    assert {r["status"] for r in rows} == {"skipped", "frozen"}
    assert "already up to date" in describe({"fund": "x", "status": "up_to_date"})


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------

def sig(model, ticker, value, comps=None, abstained=False, reasoning="why"):
    return NS(model_name=model, ticker=ticker, value=value, components=comps or {},
              metadata={"abstained": True} if abstained else {}, reasoning=reasoning)


def rec(day, nav, signals=(), clamps=()):
    decision = NS(strategies=[NS(signals=list(signals))], clamps=[NS(limit=l, ticker=t) for l, t in clamps]) \
        if signals or clamps else None
    return NS(session=day, nav=nav, decision=decision)


def test_alerts_fire_on_changes_not_on_steady_states():
    records = [
        rec("2026-09-01", 100_000, [sig("insiders", "AAPL", 0.5, {"buyers": 1})]),
        rec("2026-09-02", 100_500, [sig("insiders", "AAPL", 0.8, {"buyers": 3})], [("pump_guard", "GME")]),
        rec("2026-09-03", 104_000, [sig("insiders", "AAPL", 0.8, {"buyers": 3})], [("pump_guard", "GME"), ("market_filter", None)]),
        rec("2026-09-04", 99_000, [sig("social_watch", "TSLA", -0.8), sig("superinvestors", "OXY", 1.0, {"holders": 1, "new_positions": 1}),
                                   sig("congress", "NVDA", 0.3, {"members": 1})]),
        rec("2026-09-05", 88_000, [sig("social_watch", "TSLA", -0.9), sig("insiders", "MSFT", 0.5, {"buyers": 1, "c_suite": 1})]),
    ]
    events = [{"kind": "tick_failed", "time": "2026-09-08T10:00:00", "session": "2026-09-08", "error": "boom"}]
    got = [(a["session"], a["kind"], a["ticker"]) for a in alerts.fund_alerts("alpha", records, events, 100_000)]
    assert ("2026-09-02", "insider", "AAPL") in got and ("2026-09-03", "insider", "AAPL") not in got
    assert got.count(("2026-09-02", "pump", "GME")) == 1 and ("2026-09-03", "pump", "GME") not in got
    assert ("2026-09-03", "shield", None) in got and ("2026-09-04", "shield", None) in got  # up, then down
    assert ("2026-09-03", "big_day", None) in got and ("2026-09-04", "big_day", None) in got
    assert ("2026-09-04", "hype", "TSLA") in got and ("2026-09-05", "hype", "TSLA") not in got
    assert ("2026-09-04", "fund_buy", "OXY") in got and ("2026-09-04", "congress", "NVDA") in got
    assert ("2026-09-05", "insider", "MSFT") in got  # the CEO/CFO buying counts even alone
    assert [k for _, k, _ in got].count("drawdown") == 1  # crossed 10% once (15.4% from the 104k high)
    assert ("2026-09-08", "frozen", None) in got
    assert all(a["body"] for a in alerts.fund_alerts("alpha", records, events, 100_000))


def test_dispatch_skips_history_then_sends_only_new_wanted_alerts(tmp_path, clock):
    directory = deploy("alpha", FundSpec(**SPEC), ["AAPL"], root=tmp_path / "paper")
    sent_path = tmp_path / "sent.json"
    settings = {"alerts": {"phone": True, "kinds": list(ALERT_KINDS)}}
    sends = []
    Ledger(directory).log_event("tick_failed", session="2024-06-03", error="old news")
    assert alerts.dispatch(directory, send=sends.append, sent_path=sent_path, settings=settings, research_dir=tmp_path) == []
    Ledger(directory).log_event("tick_failed", session="2024-06-04", error="new problem")
    out = alerts.dispatch(directory, send=sends.append, sent_path=sent_path, settings=settings, research_dir=tmp_path)
    assert [a["kind"] for a in out] == ["frozen"] and "new problem" in sends[0]["body"]
    assert alerts.dispatch(directory, send=sends.append, sent_path=sent_path, settings=settings, research_dir=tmp_path) == []
    Ledger(directory).log_event("tick_failed", session="2024-06-05", error="unwanted")
    muted = {"alerts": {"phone": True, "kinds": ["pump"]}}
    assert alerts.dispatch(directory, send=sends.append, sent_path=sent_path, settings=muted, research_dir=tmp_path) == []
    assert len(sends) == 1


def test_phone_push_goes_to_ntfy_with_a_safe_title(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "aihf-test-topic-123")
    posted = {}

    class Http:
        def post(self, url, data, timeout, headers):
            posted.update(url=url, data=data, headers=headers)
            return NS(status_code=200)
    alerts.push_phone({"title": "AAPL: insiders buying — wow", "body": "3 insiders bought", "level": "good"}, Http())
    assert posted["url"] == "https://ntfy.sh/aihf-test-topic-123"
    assert posted["headers"]["Title"].isascii() and posted["data"] == b"3 insiders bought"
    monkeypatch.delenv("NTFY_TOPIC")
    with pytest.raises(RuntimeError):
        alerts.push_phone({"title": "x", "body": "y", "level": "info"}, Http())


# ---------------------------------------------------------------------------
# go-live scorecard
# ---------------------------------------------------------------------------

def _days(n):
    from datetime import date, timedelta
    out, d = [], date(2026, 1, 5)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _records(n, fund_daily, bench_daily, wobble=0.004):
    rows, nav, close = [], 100_000.0, 500.0
    for i, day in enumerate(_days(n)):
        swing = wobble if i % 2 else -wobble
        if i:  # the first session is the starting point
            nav *= 1 + fund_daily + swing * 0.6
            close *= 1 + bench_daily + swing
        rows.append(NS(session=day, nav=nav, benchmark_close=close,
                       executed=NS(fills=[NS(quantity=10, price=100.0)] if i % 5 == 0 else [])))
    return rows


def test_scorecard_passes_a_long_calm_winner_with_a_matching_backtest():
    records = _records(140, 0.0012, 0.0004)
    bt = [{"mandate": "steady", "years": 3, "excess_return_pct": 0.2, "max_drawdown_pct": 0.12, "bench_max_drawdown_pct": 0.25}]
    card = scorecard.scorecard(records, [], 100_000, "steady", bt)
    assert card["all_passed"], [c for c in card["checks"] if not c["ok"]]
    without_bt = scorecard.scorecard(records, [], 100_000, "steady", [])
    assert not without_bt["all_passed"] and [c["id"] for c in without_bt["checks"] if not c["ok"]] == ["backtest"]


def test_scorecard_is_honest_about_short_or_costly_records():
    short = scorecard.scorecard(_records(30, 0.002, 0.0), [], 100_000, "x", [])
    time = next(c for c in short["checks"] if c["id"] == "time")
    assert not time["ok"] and time["progress"] == [30, 126]
    # Barely ahead before costs, behind after them.
    churn = _records(140, 0.00041, 0.0004)
    for r in churn:
        r.executed.fills = [NS(quantity=200, price=100.0)]
    card = scorecard.scorecard(churn, [], 100_000, "x", [])
    ok = {c["id"]: c["ok"] for c in card["checks"]}
    assert ok["beat"] and not ok["costs"]
    failed = scorecard.scorecard(_records(140, 0.0012, 0.0004), [{"kind": "tick_failed", "session": _days(140)[-3]}], 100_000, "x", [])
    assert not {c["id"]: c["ok"] for c in failed["checks"]}["clean"]
    assert scorecard.scorecard([], [], 100_000, "x", [])["passed"] == 0


# ---------------------------------------------------------------------------
# dashboard backtests
# ---------------------------------------------------------------------------

def test_backtest_runner_streams_progress_and_saves_a_summary(tmp_path):
    runner = backtests.BacktestRunner(tmp_path, fake_data, build_fund)
    status = runner.start(FundSpec(**SPEC), ["AAPL"], WEEKDAYS[0], WEEKDAYS[-1], "solo")
    assert status["state"] == "running"
    done = runner.wait()
    assert done["state"] == "done" and done["done"] == done["total"] == len(WEEKDAYS)
    assert len(done["curve"]["nav"]) == len(WEEKDAYS)
    [listed] = backtests.list_summaries(tmp_path)
    assert "curve" not in listed and listed["mandate"] == "paper-fund" and listed["mandate_file"] == "solo"
    full = backtests.load_summary(tmp_path, listed["id"])
    assert len(full["curve"]["dates"]) == len(WEEKDAYS) and full["bench_max_drawdown_pct"] >= 0
    assert list(tmp_path.glob("*.json")) and len(list(tmp_path.glob("*.summary.json"))) == 1
    with pytest.raises(FileNotFoundError):
        backtests.load_summary(tmp_path, "../etc/passwd")


def test_backtest_runner_can_be_cancelled(tmp_path):
    import threading
    gate = threading.Event()

    class Slow(FakeDataClient):
        def get_prices(self, *args, **kwargs):
            gate.wait(5)
            return super().get_prices(*args, **kwargs)

    @contextmanager
    def slow():
        yield Slow(SERIES)
    runner = backtests.BacktestRunner(tmp_path, slow, build_fund)
    runner.start(FundSpec(**SPEC), ["AAPL"], WEEKDAYS[0], WEEKDAYS[-1])
    runner.cancel()
    gate.set()
    assert runner.wait()["state"] == "cancelled" and backtests.list_summaries(tmp_path) == []


def test_backtest_windows(clock):
    clock("2026-10-22")
    assert backtests.window(1) == ("2025-10-21", "2026-10-21")
    with pytest.raises(ValueError):
        backtests.window(2)
