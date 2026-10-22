"""FOMO's leaderboard, rebuilt from its public trades.

Every few hours the desk saves one window of FOMO trading: for each trader
and token, how much they bought and sold, in tokens and dollars. Stacking
those windows gives each trader's running position per token, so profit can
be measured the way a broker would: average cost in, realized profit on
what they sold. Sells of tokens bought before the desk started watching have
no known cost and are left out rather than guessed.

Ranking rules, chosen to resist luck and gaming:
- realized profit over the last 7 days, not open positions that may never sell
- at least 5 closed positions (one lucky memecoin doesn't make a top trader)
- at least half of closed positions profitable
- at least $500 of buying (dust wallets and bots spraying tiny trades are out)
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hedge_fund.fomo.sources import WindowRow

WINDOW_HOURS = 3
LOOKBACK_DAYS = 7
MIN_CLOSED = 5
MIN_WIN_RATE = 0.5
MIN_BOUGHT_USD = 500.0
CLOSED_SHARE = 0.9   # sold at least 90% of what they bought = a closed position


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS windows (
                since TEXT PRIMARY KEY, till TEXT NOT NULL, collected TEXT NOT NULL, rows INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS flows (
                since TEXT NOT NULL, owner TEXT NOT NULL, mint TEXT NOT NULL, symbol TEXT,
                bought_usd REAL, sold_usd REAL, bought REAL, sold REAL, trades INTEGER,
                PRIMARY KEY (since, owner, mint))""")

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def windows(self) -> list[tuple[str, str]]:
        with closing(self._db()) as db:
            return db.execute("SELECT since, till FROM windows ORDER BY since").fetchall()

    def save_window(self, since: str, till: str, rows: list[WindowRow]) -> None:
        with closing(self._db()) as db, db:
            db.execute("DELETE FROM flows WHERE since = ?", (since,))
            db.executemany("INSERT INTO flows VALUES (?,?,?,?,?,?,?,?,?)",
                           [(since, r.owner, r.mint, r.symbol, r.bought_usd, r.sold_usd, r.bought, r.sold, r.trades) for r in rows])
            db.execute("INSERT OR REPLACE INTO windows VALUES (?,?,?,?)",
                       (since, till, _iso(datetime.now(timezone.utc)), len(rows)))

    def prune(self, keep_days: int = LOOKBACK_DAYS + 7) -> None:
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=keep_days))
        with closing(self._db()) as db, db:
            db.execute("DELETE FROM flows WHERE since < ?", (cutoff,))
            db.execute("DELETE FROM windows WHERE since < ?", (cutoff,))

    def flows(self, since: str) -> list[tuple]:
        with closing(self._db()) as db:
            return db.execute("""SELECT since, owner, mint, symbol, bought_usd, sold_usd, bought, sold, trades
                                 FROM flows WHERE since >= ? ORDER BY since""", (since,)).fetchall()


def due_windows(store: Store, now: datetime | None = None, hours: int = WINDOW_HOURS, max_back_hours: int = 9) -> list[tuple[str, str]]:
    """The finished windows not saved yet, oldest first. Bitquery's realtime
    data reaches back ~12 hours, so anything older than that is gone."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)
    end -= timedelta(hours=end.hour % hours)
    have = {s for s, _ in store.windows()}
    out = []
    start = end - timedelta(hours=max_back_hours)
    t = start
    while t < end:
        since, till = _iso(t), _iso(t + timedelta(hours=hours))
        if since not in have:
            out.append((since, till))
        t += timedelta(hours=hours)
    return out


def collect(store: Store, bitquery, now: datetime | None = None) -> int:
    """Save every due window. Returns how many were saved."""
    saved = 0
    for since, till in due_windows(store, now):
        store.save_window(since, till, bitquery.window(since, till))
        saved += 1
    store.prune()
    return saved


def leaderboard(store: Store, now: datetime | None = None, days: int = LOOKBACK_DAYS) -> list[dict]:
    """Every trader with realized profit or loss, best first,
    marked eligible when they pass the ranking rules."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = _iso(now - timedelta(days=days))
    pos: dict[tuple[str, str], dict] = {}
    for _, owner, mint, symbol, b_usd, s_usd, bought, sold, trades in store.flows(since):
        p = pos.setdefault((owner, mint), {"symbol": symbol, "held": 0.0, "cost": 0.0, "bought": 0.0, "bought_usd": 0.0,
                                           "sold": 0.0, "realized": 0.0, "trades": 0})
        p["trades"] += trades or 0
        if bought and b_usd:
            p["held"] += bought
            p["cost"] += b_usd
            p["bought"] += bought
            p["bought_usd"] += b_usd
        if sold and s_usd and p["held"] > 0:
            matched = min(sold, p["held"])  # sells beyond what we saw them buy have no known cost
            avg = p["cost"] / p["held"]
            p["realized"] += s_usd * (matched / sold) - avg * matched
            p["cost"] -= avg * matched
            p["held"] -= matched
            p["sold"] += matched
    people: dict[str, dict] = {}
    for (owner, mint), p in pos.items():
        w = people.setdefault(owner, {"wallet": owner, "realized": 0.0, "closed": 0, "wins": 0, "trades": 0,
                                      "bought_usd": 0.0, "open": 0, "best": None})
        w["trades"] += p["trades"]
        w["bought_usd"] += p["bought_usd"]
        w["realized"] += p["realized"]
        if p["bought"] and p["sold"] >= CLOSED_SHARE * p["bought"]:
            w["closed"] += 1
            if p["realized"] > 0:
                w["wins"] += 1
            if w["best"] is None or p["realized"] > w["best"]["profit"]:
                w["best"] = {"symbol": p["symbol"], "mint": mint, "profit": round(p["realized"], 2)}
        elif p["held"] > 0:
            w["open"] += 1
    rows = []
    for w in people.values():
        if not w["closed"] and abs(w["realized"]) < 0.01:
            continue  # never sold anything we can price: nothing to rank yet
        w["win_rate"] = w["wins"] / w["closed"] if w["closed"] else None
        w["eligible"] = (w["closed"] >= MIN_CLOSED and (w["win_rate"] or 0) >= MIN_WIN_RATE
                         and w["bought_usd"] >= MIN_BOUGHT_USD and w["realized"] > 0)
        w["realized"] = round(w["realized"], 2)
        rows.append(w)
    rows.sort(key=lambda w: (not w["eligible"], -w["realized"]))
    return rows


def coverage(store: Store, now: datetime | None = None) -> dict:
    """How much history the leaderboard stands on."""
    windows = store.windows()
    hours = len(windows) * WINDOW_HOURS
    return {"windows": len(windows), "hours": hours, "since": windows[0][0] if windows else None,
            "last": windows[-1][1] if windows else None, "full": hours >= LOOKBACK_DAYS * 24}
