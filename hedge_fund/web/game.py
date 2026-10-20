"""The game layer: XP, levels, achievements and the leaderboard.

Everything is derived from what a fund's ledger already records (sessions,
fills, risk events, the kill switch), so it costs nothing to store, can't
drift out of sync, and replays the same way every time. XP rewards playing
and beating the S&P 500, never trading more: churning a fund earns nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from hedge_fund.pipeline.session import SessionRecord

XP_PER_DAY = 10
XP_PER_DAY_AHEAD = 5
XP_PER_BADGE = 25

RANKS = [(1, "Intern"), (3, "Analyst"), (5, "Trader"), (8, "Portfolio Manager"), (12, "Partner"), (17, "Legend")]

TRACKERS = ("insiders", "superinvestors", "congress")


def xp_to_reach(level: int) -> int:
    """Total XP for a level: each level costs 50 XP more than the last."""
    return 25 * level * (level - 1)


def level_for(xp: int) -> int:
    level = 1
    while xp >= xp_to_reach(level + 1):
        level += 1
    return level


def rank_for(level: int) -> str:
    return [title for at, title in RANKS if level >= at][-1]


@dataclass(frozen=True)
class Badge:
    id: str
    name: str
    icon: str          # a glyph key the page draws as pixel art
    desc: str
    target: int = 0    # shown as progress while locked; 0 = no progress bar


BADGES = [
    Badge("opening_bell", "Opening Bell", "bell", "Play your first trading day."),
    Badge("first_trade", "First Trade", "coin", "Your fund makes its first buy or sell."),
    Badge("week_one", "Week One", "calendar", "Play 5 trading days.", 5),
    Badge("month", "A Month In", "calendar", "Play 21 trading days (about a month).", 21),
    Badge("season", "Season Veteran", "star", "Play 63 trading days (about 3 months).", 63),
    Badge("boss_fight", "Boss Fight", "sword", "Be ahead of the S&P 500 after at least 5 days."),
    Badge("boss_slayer", "Boss Slayer", "crown", "Be ahead of the S&P 500 after 63 days or more. The one that counts."),
    Badge("iron_shield", "Iron Shield", "shield", "After 21+ days, your worst drop is smaller than the S&P 500's."),
    Badge("hot_streak", "Hot Streak", "flame", "Beat the S&P 500 five days in a row.", 5),
    Badge("high_score", "High Score", "gem", "Fund value 5% above where it started.", 5),
    Badge("double_digits", "Double Digits", "trophy", "Fund value 10% above where it started.", 10),
    Badge("comeback", "Comeback Kid", "heart", "Fall 5% from a high, then climb back to a new high."),
    Badge("pump_dodger", "Pump Dodger", "alarm", "The pump guard keeps you out of a suspected pump."),
    Badge("shields_up", "Shields Up", "shield", "The market filter cuts risk when the S&P 500 turns down."),
    Badge("copycat", "Copycat", "eye", "A tracker spots insiders, a famous fund or Congress buying one of your stocks."),
    Badge("cool_head", "Cool Head", "snow", "Freeze the fund, then resume it."),
]


def progress(records: list[SessionRecord], events: list[dict], capital: float) -> dict:
    """XP, level, rank and every badge (unlocked with the day it happened,
    or locked with progress) for one fund."""
    unlocked: dict[str, str] = {}

    def unlock(badge: str, when: str) -> None:
        unlocked.setdefault(badge, when)

    first_bench = records[0].benchmark_close if records else None
    peak, bench_peak = capital, first_bench or 0.0
    worst, bench_worst = 0.0, 0.0
    streak = best_streak = days_ahead = 0
    fell_from_high = False
    prev_nav, prev_bench = capital, first_bench
    for i, r in enumerate(records):
        played = i + 1
        unlock("opening_bell", r.session)
        if played >= 5:
            unlock("week_one", r.session)
        if played >= 21:
            unlock("month", r.session)
        if played >= 63:
            unlock("season", r.session)
        if r.executed is not None and r.executed.fills:
            unlock("first_trade", r.session)

        ret = r.nav / capital - 1 if capital else 0.0
        bench_ret = r.benchmark_close / first_bench - 1 if first_bench else 0.0
        ahead = ret > bench_ret
        if ahead:
            days_ahead += 1
        if ahead and played >= 5:
            unlock("boss_fight", r.session)
        if ahead and played >= 63:
            unlock("boss_slayer", r.session)

        day_ret = r.nav / prev_nav - 1 if prev_nav else 0.0
        bench_day = r.benchmark_close / prev_bench - 1 if prev_bench else 0.0
        streak = streak + 1 if day_ret > bench_day else 0
        best_streak = max(best_streak, streak)
        if streak >= 5:
            unlock("hot_streak", r.session)
        prev_nav, prev_bench = r.nav, r.benchmark_close

        if r.nav > peak:
            if fell_from_high:
                unlock("comeback", r.session)
            peak, fell_from_high = r.nav, False
        elif peak and r.nav / peak - 1 <= -0.05:
            fell_from_high = True
        worst = min(worst, r.nav / peak - 1 if peak else 0.0)
        bench_peak = max(bench_peak, r.benchmark_close)
        bench_worst = min(bench_worst, r.benchmark_close / bench_peak - 1 if bench_peak else 0.0)
        if played >= 21 and worst > bench_worst:
            unlock("iron_shield", r.session)

        if ret >= 0.05:
            unlock("high_score", r.session)
        if ret >= 0.10:
            unlock("double_digits", r.session)

        for clamp in _clamps(r):
            if clamp.limit == "pump_guard":
                unlock("pump_dodger", r.session)
            if clamp.limit == "market_filter":
                unlock("shields_up", r.session)
        if r.decision is not None and _copied(r.decision):
            unlock("copycat", r.session)

    halted_at = None
    for event in events:
        when = str(event.get("time", ""))[:10]
        if event.get("kind") == "halt":
            halted_at = when
        elif event.get("kind") == "resume" and halted_at is not None:
            unlock("cool_head", when)

    best_ret = max((r.nav / capital - 1 for r in records), default=0.0) if capital else 0.0
    counts = {"week_one": len(records), "month": len(records), "season": len(records),
              "hot_streak": best_streak, "high_score": round(max(0.0, best_ret) * 100),
              "double_digits": round(max(0.0, best_ret) * 100)}

    xp = XP_PER_DAY * len(records) + XP_PER_DAY_AHEAD * days_ahead + XP_PER_BADGE * len(unlocked)
    level = level_for(xp)
    badges = []
    for b in BADGES:
        row = {"id": b.id, "name": b.name, "icon": b.icon, "desc": b.desc,
               "unlocked": b.id in unlocked, "at": unlocked.get(b.id)}
        if b.target and b.id not in unlocked:
            row["progress"] = [min(counts.get(b.id, 0), b.target), b.target]
        badges.append(row)
    return {
        "xp": xp,
        "level": level,
        "rank": rank_for(level),
        "level_floor": xp_to_reach(level),
        "level_next": xp_to_reach(level + 1),
        "days_ahead": days_ahead,
        "badges": badges,
        "unlocked": len(unlocked),
        "worst_drop": worst,
        "bench_worst_drop": bench_worst,
    }


def _clamps(r: SessionRecord):
    if r.decision is not None:
        yield from r.decision.clamps
    if r.executed is not None:
        yield from r.executed.clamps


def _copied(decision) -> bool:
    return any(
        s.model_name in TRACKERS and s.value > 0 and not (s.metadata or {}).get("abstained")
        for sr in decision.strategies for s in sr.signals
    )


def leaderboard(rows: list[dict]) -> list[dict]:
    """Rank funds by how far ahead of the S&P 500 they are over their own
    days. Unplayed and broken funds go last."""
    key: Callable[[dict], tuple] = lambda r: (
        r.get("error") is not None,
        r.get("lead") is None,
        -(r.get("lead") or 0.0),
        r["name"],
    )
    ranked = sorted(rows, key=key)
    place = 0
    for row in ranked:
        if row.get("lead") is not None and not row.get("error"):
            place += 1
            row["place"] = place
        else:
            row["place"] = None
    return ranked
