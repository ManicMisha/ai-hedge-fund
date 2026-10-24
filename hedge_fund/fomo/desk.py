"""The FOMO desk: follow FOMO's top traders and paper-copy their trades.

Paper money only. Nothing here can sign a transaction or touch a wallet:
the desk reads public data and keeps a pretend book.

Every minute or so it reads the followed wallets' new transactions. When one
buys a token, the desk buys it too at the price *now* (so the delay is real),
pays FOMO's ~0.5% fee and an estimate of slippage, and records how much worse
its price was than the trader's: the "lag cost", the number that decides
whether riding behind them can work. When a followed trader sells, the desk
sells the same share. A stop-loss and a time limit catch the ones they
forget, and a token whose pool vanishes is written off.

Rules (all in settings): 4% of the fund per copy (6% when two followed
traders buy the same token within a day), at most 20 positions, skip pools
under $30k liquidity or younger than 10 minutes, never more than 2% of a
pool, stop-loss at -50%, time limit 14 days.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from hedge_fund.fomo import leaderboard as lb
from hedge_fund.fomo.council import (
    BENCH_MIN_CLOSED, BENCH_WIN_RATE, buy_council, crypto_hype, EUPHORIA, exit_checks, HYPE_VETO, Situation,
    SOL_RISK_OFF_H24, votes_json,
)
from hedge_fund.fomo.sources import (
    Bitquery, DexScreener, SetupError, SOL_MINT, SolanaRPC, SourceError, STABLES, Swap,
)
from hedge_fund.paths import write_atomic

FEE = 0.005               # FOMO's spot fee, each side
BASE_SLIPPAGE = 0.003
MIN_COPY_USD = 50.0       # ignore a trader's dust buys
MAX_POOL_SHARE = 0.02
MIN_POOL_AGE_S = 600
CLUSTER_WINDOW_S = 86_400
TIME_LIMIT_S = 14 * 86_400
NO_PRICE_WRITE_OFF_S = 86_400
HISTORY_EVERY_S = 900
HYPE_RECHECK_S = 6 * 3600
MAX_SIGS_PER_POLL = 15
KEEP_EVENTS = 400

DEFAULTS = {"follow": 10, "slot_pct": 0.04, "min_liquidity": 30_000.0, "capital": 1_000.0,
            "stop_loss": 0.5, "max_positions": 20}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def short(wallet: str) -> str:
    return f"{wallet[:4]}…{wallet[-4:]}" if len(wallet) > 10 else wallet


class Desk:
    def __init__(self, root: Path, config: dict | None = None, *, rpc: SolanaRPC | None = None,
                 dex: DexScreener | None = None, bitquery: Callable[[], Bitquery] | None = None,
                 clock: Callable[[], float] = time.time,
                 hype: Callable[[str, str], dict | None] | None = None,
                 market_flags: Callable[[], list[str]] | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = {**DEFAULTS, **(config or {})}
        self.rpc = rpc
        self.dex = dex or DexScreener()
        self.bitquery = bitquery or (lambda: Bitquery())
        self.clock = clock
        self.hype = hype if hype is not None else (lambda symbol, mint: crypto_hype(symbol, mint, self.root / "hype"))
        self.market_flags = market_flags or (lambda: [])
        self.store = lb.Store(self.root / "fomo.sqlite")
        self.state_path = self.root / "desk.json"
        self.state = self._load()

    # ---- state --------------------------------------------------------------

    def _fresh(self) -> dict:
        capital = float(self.config["capital"])
        return {"capital": capital, "cash": capital, "started": _iso(self.clock()), "positions": {}, "followed": [],
                "cursors": {}, "holdings": {}, "recent_buys": {}, "history": [], "sol_start": None, "sol_last": None,
                "events": [], "realized": 0.0, "fees": 0.0, "copies": 0, "last_poll": None, "last_collect": None,
                "errors": {}, "records": {}, "risk_off": [], "heat": None}

    def _load(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text())
            return {**self._fresh(), **state}
        except (OSError, ValueError):
            return self._fresh()

    def save(self) -> None:
        write_atomic(self.state_path, json.dumps(self.state))

    def reset(self, capital: float | None = None) -> None:
        if capital is not None:
            self.config["capital"] = float(capital)
        keep = {k: self.state.get(k) for k in ("followed", "cursors", "records")}
        self.state = self._fresh()
        self.state.update({k: v for k, v in keep.items() if v is not None})  # keep who we watch and what we learned
        self.save()

    def _event(self, kind: str, **fields) -> dict:
        event = {"t": _iso(self.clock()), "kind": kind, **fields}
        self.state["events"] = ([event] + self.state["events"])[:KEEP_EVENTS]
        return event

    def _error(self, source: str, exc: Exception | None) -> None:
        if exc is None:
            self.state["errors"].pop(source, None)
        else:
            self.state["errors"][source] = {"t": _iso(self.clock()), "message": str(exc)[:200]}

    # ---- finding traders --------------------------------------------------------

    def collect(self, now: datetime | None = None) -> int:
        """Save due windows of FOMO trades and re-pick who to follow."""
        try:
            saved = lb.collect(self.store, self.bitquery(), now)
        except SetupError as exc:
            self._error("bitquery", exc)
            return 0
        except SourceError as exc:
            self._error("bitquery", exc)
            saved = 0
        else:
            self._error("bitquery", None)
        board = lb.leaderboard(self.store, now)
        picks = [w["wallet"] for w in board if w["eligible"] and not self.benched(w["wallet"])][: int(self.config["follow"])]
        if picks and picks != self.state["followed"]:  # an empty board (warming up, Bitquery down) changes nothing
            added = [w for w in picks if w not in self.state["followed"]]
            if added:
                self._event("follow", wallets=added, text=f"Now following {len(picks)} top FOMO traders "
                            f"({len(added)} new: {', '.join(short(w) for w in added[:3])}).")
            self.state["followed"] = picks
        self.state["last_collect"] = _iso(self.clock())
        self.save()
        return saved

    def benched(self, wallet: str) -> bool:
        """Copying this trader has been losing us money: stop following them."""
        r = self.state["records"].get(wallet) or {}
        closed = r.get("closed", 0)
        return closed >= BENCH_MIN_CLOSED and (r.get("wins", 0) / closed < BENCH_WIN_RATE or r.get("pnl", 0.0) < 0)

    def watching(self) -> list[str]:
        """Followed traders, plus anyone whose trade we still hold a copy of."""
        held_from = [w for p in self.state["positions"].values() for w in p["from"]]
        return list(dict.fromkeys(self.state["followed"] + held_from))

    # ---- following --------------------------------------------------------------

    def poll(self) -> list[dict]:
        """Read new trades, copy them, run the exits, mark the book.
        Returns the events worth an alert."""
        out: list[dict] = []
        rpc = self.rpc or SolanaRPC()
        swaps: list[Swap] = []
        try:
            for wallet in self.watching():
                cursor = self.state["cursors"].get(wallet)
                if cursor is None:  # first look: start from now, don't replay their history
                    latest = rpc.new_signatures(wallet, None, limit=1)
                    self.state["cursors"][wallet] = latest[-1]["signature"] if latest else ""
                    continue
                sigs = rpc.new_signatures(wallet, cursor or None)[-MAX_SIGS_PER_POLL:]
                for s in sigs:
                    swaps += rpc.swaps(wallet, s["signature"])
                    self.state["cursors"][wallet] = s["signature"]
            self._error("solana", None)
        except SourceError as exc:
            self._error("solana", exc)
        swaps.sort(key=lambda s: s.time)
        mints = {s.mint for s in swaps} | set(self.state["positions"]) | {SOL_MINT}
        try:
            quotes = self.dex.quotes(sorted(mints))
            self._error("prices", None)
        except SourceError as exc:
            self._error("prices", exc)
            quotes = {}
        sol = quotes.get(SOL_MINT)
        if sol:
            self.state["sol_last"] = sol.price
            self.state["sol_start"] = self.state["sol_start"] or sol.price
        risk_off = list(self.market_flags())
        if sol and sol.change_h24 is not None and sol.change_h24 <= SOL_RISK_OFF_H24:
            risk_off.insert(0, f"SOL down {-sol.change_h24:.0%} in a day")
        self.state["risk_off"] = risk_off
        self.state["heat"] = lb.heat(self.store)
        for swap in swaps:
            event = self._on_buy(swap, quotes) if swap.side == "buy" else self._on_sell(swap, quotes)
            if event:
                out.append(event)
        out += self._exits(quotes)
        self._mark(quotes)
        self.state["last_poll"] = _iso(self.clock())
        self.save()
        return out

    def _usd(self, swap: Swap) -> float | None:
        if swap.cash_mint in STABLES:
            return swap.cash
        sol = self.state.get("sol_last")
        return swap.cash * sol if sol else None

    def _nav(self) -> float:
        return self.state["cash"] + sum(p["qty"] * p["last"] for p in self.state["positions"].values())

    def _on_buy(self, swap: Swap, quotes: dict) -> dict | None:
        held = self.state["holdings"].setdefault(swap.wallet, {})
        held[swap.mint] = held.get(swap.mint, 0.0) + swap.amount
        usd = self._usd(swap)
        if usd is None or usd < MIN_COPY_USD or swap.wallet not in self.state["followed"]:
            return None
        now = self.clock()
        recent = [b for b in self.state["recent_buys"].get(swap.mint, []) if now - b["t"] < CLUSTER_WINDOW_S]
        cluster = any(b["wallet"] != swap.wallet for b in recent)
        recent.append({"wallet": swap.wallet, "t": now})
        self.state["recent_buys"][swap.mint] = recent
        their_price = usd / swap.amount if swap.amount else None
        pos = self.state["positions"].get(swap.mint)
        q = quotes.get(swap.mint)
        symbol = q.symbol if q else swap.mint[:6]
        base = dict(wallet=swap.wallet, mint=swap.mint, symbol=symbol, their_usd=round(usd, 2))
        if pos:
            if swap.wallet not in pos["from"]:
                pos["from"].append(swap.wallet)
            return None
        if len(self.state["positions"]) >= self.config["max_positions"]:
            self._event("skip", **base, text=f"Skipped {symbol} that {short(swap.wallet)} bought: already holding "
                        f"{self.config['max_positions']} tokens.", votes=[])
            return None
        followed = self.state["followed"]
        situation = Situation(
            symbol=symbol, wallet_label=short(swap.wallet), usd=usd,
            rank=followed.index(swap.wallet) if swap.wallet in followed else None,
            their_price=their_price, price=q.price if q else None, liquidity=q.liquidity if q else 0.0,
            pool_age_s=now - q.pair_created / 1000 if q and q.pair_created else None,
            change_h1=q.change_h1 if q else None,
            holders_buying=len({b["wallet"] for b in recent}),
            record=self.state["records"].get(swap.wallet, {}), risk_off=self.state["risk_off"], heat=self.state["heat"],
            min_liquidity=self.config["min_liquidity"], min_pool_age_s=MIN_POOL_AGE_S)
        approved, mult, votes = buy_council(situation, hype=lambda: self.hype(symbol, swap.mint))
        if not approved:
            veto = next(v for v in votes if v.verdict == "veto")
            self._event("skip", **base, votes=votes_json(votes),
                        text=f"Skipped {symbol} that {short(swap.wallet)} bought. {veto.agent}: {veto.text}")
            return None
        nav = self._nav()
        size = nav * self.config["slot_pct"] * mult
        size = min(size, self.state["cash"], q.liquidity * MAX_POOL_SHARE)
        if size < 5:
            self._event("skip", **base, text=f"Skipped {symbol}: not enough cash left.", votes=votes_json(votes))
            return None
        cluster = situation.holders_buying >= 2
        slip = BASE_SLIPPAGE + size / q.liquidity
        fill = q.price * (1 + slip)
        fee = size * FEE
        qty = (size - fee) / fill
        lag = fill / their_price - 1 if their_price else None
        delay = max(0, int(now - swap.time)) if swap.time else None
        self.state["cash"] -= size
        self.state["fees"] += fee
        self.state["copies"] += 1
        self.state["positions"][swap.mint] = {
            "symbol": symbol, "qty": qty, "cost": size, "entry": fill, "last": q.price, "peak": q.price,
            "opened": now, "from": [swap.wallet], "their_price": their_price, "lag": lag, "no_price_since": None,
            "realized": 0.0, "took_profit": False, "hype_checked": now, "hype_cut": False,
        }
        return self._event("buy", **base, usd=round(size, 2), price=fill, lag=lag, delay=delay, cluster=cluster,
                           mult=round(mult, 2), votes=votes_json(votes),
                           text=f"Copied {short(swap.wallet)}: bought ${size:,.0f} of {symbol}"
                                f"{' (2+ top traders buying)' if cluster else ''}"
                                f"{f', {lag:+.1%} vs their price' if lag is not None else ''}"
                                f"{f', {delay}s behind' if delay is not None else ''}. Team size x{mult:.2f}.")

    def _on_sell(self, swap: Swap, quotes: dict) -> dict | None:
        held = self.state["holdings"].setdefault(swap.wallet, {})
        before = held.get(swap.mint, 0.0)
        held[swap.mint] = max(0.0, before - swap.amount)
        pos = self.state["positions"].get(swap.mint)
        if not pos or swap.wallet not in pos["from"]:
            return None
        share = min(1.0, swap.amount / before) if before > 0 else 1.0
        if share < 0.2:
            return None  # a trim, not an exit
        return self._sell(swap.mint, share if share < 0.9 else 1.0, quotes,
                          f"{short(swap.wallet)} sold {share:.0%} of theirs")

    def _sell(self, mint: str, share: float, quotes: dict, why: str, write_off: bool = False) -> dict:
        pos = self.state["positions"][mint]
        q = quotes.get(mint)
        price = 0.0 if write_off or q is None else q.price
        qty = pos["qty"] * share
        liq = q.liquidity if q else 0.0
        slip = BASE_SLIPPAGE + (qty * price / liq if liq else 0.0)
        gross = qty * price * (1 - min(slip, 0.5))
        fee = gross * FEE
        proceeds = gross - fee
        cost = pos["cost"] * share
        profit = proceeds - cost
        self.state["cash"] += proceeds
        self.state["fees"] += fee
        self.state["realized"] += profit
        pos["realized"] = pos.get("realized", 0.0) + profit
        trader = pos["from"][0]  # the trader we copied: their record learns from every outcome
        rec = self.state["records"].setdefault(trader, {"closed": 0, "wins": 0, "pnl": 0.0})
        rec["pnl"] = round(rec["pnl"] + profit, 2)
        if share >= 0.999:
            rec["closed"] += 1
            rec["wins"] += 1 if pos["realized"] > 0 else 0
            del self.state["positions"][mint]
        else:
            pos["qty"] -= qty
            pos["cost"] -= cost
        return self._event("sell", mint=mint, symbol=pos["symbol"], usd=round(proceeds, 2), profit=round(profit, 2),
                           pct=profit / cost if cost else 0.0, share=share,
                           text=f"Sold {'all' if share >= 0.999 else f'{share:.0%}'} of {pos['symbol']} ({why}): "
                                f"{'+' if profit >= 0 else '-'}${abs(profit):,.0f} ({profit / cost if cost else 0:+.0%}).")

    def _exits(self, quotes: dict) -> list[dict]:
        out, now = [], self.clock()
        for mint, pos in list(self.state["positions"].items()):
            q = quotes.get(mint)
            if q is None:
                pos["no_price_since"] = pos["no_price_since"] or now
                if now - pos["no_price_since"] > NO_PRICE_WRITE_OFF_S:
                    out.append(self._sell(mint, 1.0, quotes, "no price for a day, written off", write_off=True))
                continue
            pos["no_price_since"] = None
            pos["peak"] = max(pos["peak"], q.price)
            tight = bool(self.state["risk_off"]) or (self.state["heat"] or 0) >= EUPHORIA
            planned = exit_checks(pos, q.price, tight=tight)
            if planned:
                share, why = planned
                if share < 1.0:
                    pos["took_profit"] = True
                out.append(self._sell(mint, share, quotes, why))
                continue
            if not pos.get("hype_cut") and now - pos.get("hype_checked", pos["opened"]) >= HYPE_RECHECK_S:
                pos["hype_checked"] = now
                try:
                    verdict = self.hype(pos["symbol"], mint)
                except Exception:
                    verdict = None
                if verdict and float(verdict.get("pump_risk", 0)) >= HYPE_VETO:
                    pos["hype_cut"] = True
                    out.append(self._sell(mint, 0.5, quotes, "Hype Watch sees coordinated pumping on X: taking half off"))
                    continue
            if q.price <= pos["entry"] * (1 - self.config["stop_loss"]):
                out.append(self._sell(mint, 1.0, quotes, f"stop-loss at {-self.config['stop_loss']:.0%}"))
            elif now - pos["opened"] > TIME_LIMIT_S:
                out.append(self._sell(mint, 1.0, quotes, "held 14 days"))
        return out

    def _mark(self, quotes: dict) -> None:
        for mint, pos in self.state["positions"].items():
            q = quotes.get(mint)
            if q:
                pos["last"] = q.price
                pos["peak"] = max(pos["peak"], q.price)
        now = self.clock()
        hist = self.state["history"]
        if not hist or now - hist[-1]["ts"] >= HISTORY_EVERY_S:
            hist.append({"ts": now, "t": _iso(now), "nav": round(self._nav(), 2), "sol": self.state.get("sol_last")})
            self.state["history"] = hist[-3000:]

    # ---- the page ---------------------------------------------------------------

    def status(self, now: datetime | None = None) -> dict:
        board = lb.leaderboard(self.store, now)
        followed = set(self.state["followed"])
        nav = self._nav()
        sol_start, sol_last = self.state.get("sol_start"), self.state.get("sol_last")
        bench = self.state["capital"] * sol_last / sol_start if sol_start and sol_last else None
        lags = [e["lag"] for e in self.state["events"] if e["kind"] == "buy" and e.get("lag") is not None]
        sells = [e for e in self.state["events"] if e["kind"] == "sell"]
        return {
            "nav": round(nav, 2), "capital": self.state["capital"], "cash": round(self.state["cash"], 2),
            "return_pct": nav / self.state["capital"] - 1 if self.state["capital"] else 0.0,
            "sol_hold": bench, "sol_return_pct": bench / self.state["capital"] - 1 if bench else None,
            "realized": round(self.state["realized"], 2), "fees": round(self.state["fees"], 2),
            "copies": self.state["copies"], "started": self.state["started"],
            "avg_lag": sum(lags) / len(lags) if lags else None,
            "win_rate": (sum(1 for e in sells if e["profit"] > 0) / len(sells)) if sells else None,
            "positions": sorted(({"mint": m, "symbol": p["symbol"], "value": p["qty"] * p["last"], "cost": p["cost"],
                                  "pnl_pct": p["qty"] * p["last"] / p["cost"] - 1 if p["cost"] else 0.0,
                                  "lag": p.get("lag"), "from": [short(w) for w in p["from"]], "opened": _iso(p["opened"])}
                                 for m, p in self.state["positions"].items()), key=lambda r: -r["value"]),
            "history": [{"t": h["t"], "nav": h["nav"], "sol": h["sol"]} for h in self.state["history"]],
            "events": self.state["events"][:80],
            "leaderboard": [{**w, "short": short(w["wallet"]), "following": w["wallet"] in followed} for w in board[:25]],
            "followed": len(followed), "coverage": lb.coverage(self.store, now),
            "last_poll": self.state["last_poll"], "last_collect": self.state["last_collect"],
            "errors": self.state["errors"], "config": self.config,
            "risk_off": self.state["risk_off"], "heat": self.state["heat"],
            "records": [{"wallet": short(w), "closed": r["closed"], "wins": r["wins"], "pnl": r["pnl"],
                         "benched": self.benched(w)} for w, r in self.state["records"].items()],
            "council": council_stats(self.state["events"]),
        }


def council_stats(events: list[dict]) -> dict:
    """How often each agent stopped a copy, from the recent trade feed."""
    vetoes: dict[str, int] = {}
    approved = 0
    for e in events:
        if e["kind"] == "buy":
            approved += 1
        for v in e.get("votes") or []:
            if v["verdict"] == "veto":
                vetoes[v["agent"]] = vetoes.get(v["agent"], 0) + 1
    return {"approved": approved, "vetoed": sum(vetoes.values()), "vetoes_by_agent": vetoes}


def event_alert(event: dict) -> dict | None:
    """The desk events worth a phone ping, in the alerts format."""
    if event["kind"] == "buy":
        return {"title": f"FOMO copy: bought {event['symbol']}", "body": event["text"], "level": "good", "kind": "fomo"}
    if event["kind"] == "sell":
        return {"title": f"FOMO copy: sold {event['symbol']} ({event['pct']:+.0%})", "body": event["text"],
                "level": "good" if event["profit"] >= 0 else "bad", "kind": "fomo"}
    return None
