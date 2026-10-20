"""XP, levels, badges and the leaderboard, all derived from the ledger."""

from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest

from hedge_fund.web import game


def days(n, start="2026-01-05"):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def records(navs, bench, fills_on=(), clamps=None, decisions=None):
    clamps, decisions = clamps or {}, decisions or {}
    out = []
    for i, (day, nav, close) in enumerate(zip(days(len(navs)), navs, bench)):
        executed = NS(fills=[object()] if i in fills_on else [], clamps=[])
        decision = decisions.get(i)
        if i in clamps:
            decision = decision or NS(clamps=[], strategies=[])
            decision.clamps = [NS(limit=clamps[i])]
        out.append(NS(session=day, nav=nav, benchmark_close=close, executed=executed, decision=decision))
    return out


def badge(progress, badge_id):
    return next(b for b in progress["badges"] if b["id"] == badge_id)


def test_levels_cost_fifty_more_xp_each():
    assert [game.xp_to_reach(n) for n in (1, 2, 3, 4)] == [0, 50, 150, 300]
    assert [game.level_for(x) for x in (0, 49, 50, 149, 150)] == [1, 1, 2, 2, 3]
    assert game.rank_for(1) == "Intern" and game.rank_for(5) == "Trader" and game.rank_for(30) == "Legend"


def test_a_steady_winner_earns_the_milestones_on_the_right_days():
    n = 70
    navs = [100_000 * (1.002 ** (i + 1)) for i in range(n)]  # beats a flat-ish boss every day
    bench = [500 * (1.0005 ** i) for i in range(n)]
    p = game.progress(records(navs, bench, fills_on={1}), [], 100_000)
    d = days(n)
    assert badge(p, "opening_bell")["at"] == d[0]
    assert badge(p, "first_trade")["at"] == d[1]
    assert badge(p, "boss_fight")["at"] == d[4]
    assert badge(p, "boss_slayer")["at"] == d[62]
    assert badge(p, "hot_streak")["at"] == d[4]
    assert badge(p, "season")["at"] == d[62]
    assert badge(p, "high_score")["unlocked"] and badge(p, "double_digits")["unlocked"]
    # XP: 10 per day, 5 per day ahead, 25 per badge.
    assert p["xp"] == 10 * n + 5 * p["days_ahead"] + 25 * p["unlocked"]
    assert p["level"] == game.level_for(p["xp"]) and p["level_floor"] <= p["xp"] < p["level_next"]


def test_locked_badges_show_progress_and_trading_more_earns_nothing():
    flat = game.progress(records([100_000] * 3, [500] * 3), [], 100_000)
    assert badge(flat, "month")["progress"] == [3, 21] and not badge(flat, "month")["unlocked"]
    busy = game.progress(records([100_000] * 3, [500] * 3, fills_on={0, 1, 2}), [], 100_000)
    assert busy["xp"] - flat["xp"] == game.XP_PER_BADGE  # one First Trade badge, not XP per trade


def test_comeback_and_iron_shield():
    navs = [100_000, 104_000, 98_000, 97_000, 105_000] + [105_000] * 20
    bench = [500, 500, 480, 450, 460] + [455] * 20
    p = game.progress(records(navs, bench), [], 100_000)
    d = days(len(navs))
    assert badge(p, "comeback")["at"] == d[4]
    assert badge(p, "iron_shield")["at"] == d[20]  # 6.7% worst drop vs the boss's 10%
    assert p["worst_drop"] == pytest.approx(97 / 104 - 1)


def test_risk_events_trackers_and_the_kill_switch():
    tracker = NS(model_name="insiders", value=0.8, metadata={})
    sleepy = NS(model_name="congress", value=0.0, metadata={"abstained": True})
    decisions = {3: NS(clamps=[], strategies=[NS(signals=[tracker])]),
                 4: NS(clamps=[], strategies=[NS(signals=[sleepy])])}
    recs = records([100_000] * 6, [500] * 6, clamps={1: "pump_guard", 2: "market_filter"}, decisions=decisions)
    events = [{"kind": "halt", "time": "2026-02-01T10:00:00"}, {"kind": "resume", "time": "2026-02-02T10:00:00"}]
    p = game.progress(recs, events, 100_000)
    d = days(6)
    assert badge(p, "pump_dodger")["at"] == d[1] and badge(p, "shields_up")["at"] == d[2]
    assert badge(p, "copycat")["at"] == d[3]
    assert badge(p, "cool_head")["at"] == "2026-02-02"
    only_abstained = game.progress(records([100_000] * 6, [500] * 6, decisions={4: decisions[4]}), [], 100_000)
    assert not badge(only_abstained, "copycat")["unlocked"]


def test_an_empty_fund_is_level_one_with_nothing_unlocked():
    p = game.progress([], [], 100_000)
    assert (p["xp"], p["level"], p["rank"], p["unlocked"]) == (0, 1, "Intern", 0)


def test_leaderboard_ranks_by_lead_and_puts_unplayed_and_broken_last():
    rows = [{"name": "new", "lead": None}, {"name": "b", "lead": -0.01}, {"name": "a", "lead": 0.03},
            {"name": "broken", "error": "boom"}, {"name": "c", "lead": 0.0}]
    ranked = game.leaderboard(rows)
    assert [r["name"] for r in ranked] == ["a", "c", "b", "new", "broken"]
    assert [r["place"] for r in ranked] == [1, 2, 3, None, None]
