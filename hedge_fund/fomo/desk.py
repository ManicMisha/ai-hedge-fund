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
    BENCH_MIN_CLOSED, BENCH_WIN_RATE, buy_council, EUPHORIA, exit_checks, next_checkpoint, Situation,
    SOL_RISK_OFF_H24, votes_json,
)
from hedge_fund.fomo.scout import describe as describe_report
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
RECHECK_UP = 0.05        # cancel if the price jumps 5% while the team checks
RECHECK_LIQ = 0.7        # ...or the pool loses 30% of its money
KEEP_CASES = 150
KEEP_WIRE = 300
GATE_AGENTS = {"Pool Guard", "Too-Late Check", "Market Shield", "FOMO Meter", "Track Record"}
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
                 scout=None, reviewers=None,
                 market_flags: Callable[[], list[str]] | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = {**DEFAULTS, **(config or {})}
        self.rpc = rpc
        self.dex = dex or DexScreener()
        self.bitquery = bitquery or (lambda: Bitquery())
        self.clock = clock
        self.scout = scout          # SocialScout, or None: the team trades without social checks
        self.reviewers = reviewers  # Reviewers (Second Opinion + Exit Desk), or None
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
                "errors": {}, "records": {}, "risk_off": [], "heat": None, "cases": [], "wire": [], "ai_calls": {},
                "case_seq": 0}

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
        keep = {k: self.state.get(k) for k in ("followed", "cursors", "records", "case_seq")}
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

    def _ai_ok(self) -> bool:
        today = _iso(self.clock())[:10]
        calls = self.state["ai_calls"]
        if calls.get("date") != today:
            self.state["ai_calls"] = {"date": today, "count": 0}
        return self.state["ai_calls"]["count"] < int(self.config.get("ai_checks_per_day", 25))

    def _ai_used(self) -> None:
        self.state["ai_calls"]["count"] = self.state["ai_calls"].get("count", 0) + 1

    def _open_case(self, swap: Swap, symbol: str) -> dict:
        self.state["case_seq"] = self.state.get("case_seq", 0) + 1
        case = {"id": f"C{self.state['case_seq']}", "symbol": symbol, "mint": swap.mint, "wallet": short(swap.wallet),
                "opened": _iso(self.clock()), "status": "checking", "messages": []}
        self.state["cases"] = ([case] + self.state["cases"])[:KEEP_CASES]
        return case

    def _case(self, case_id: str | None) -> dict | None:
        return next((c for c in self.state["cases"] if c["id"] == case_id), None) if case_id else None

    def _say(self, case: dict | None, agent: str, text: str, verdict: str = "info", to: str = "Front Desk") -> None:
        if case is not None:
            case["messages"].append({"t": _iso(self.clock()), "agent": agent, "to": to, "verdict": verdict, "text": text})

    def _scout(self, symbol: str, mint: str, purpose: str, max_age_s: float):
        """A Social Scout report, if there's a key and budget left. Errors become None."""
        if self.scout is None or not getattr(self.scout, "ready", True):
            return None, "no xAI key, so nobody is scrolling socials"
        if not self._ai_ok():
            return None, "today's AI budget is used up"
        try:
            report = self.scout.report(symbol, mint, purpose, max_age_s)
        except Exception as exc:
            return None, f"search failed ({type(exc).__name__})"
        if report and not report.get("cached"):
            self._ai_used()
        return report, None

    def _review(self, kind: str, case_file: dict):
        if self.reviewers is None:
            return None, "no AI reviewer set up"
        if not self._ai_ok():
            return None, "today's AI budget is used up"
        try:
            verdict = self.reviewers.entry(case_file) if kind == "entry" else self.reviewers.exit(case_file)
        except Exception as exc:
            return None, f"couldn't reach the AI model ({type(exc).__name__}: {str(exc)[:80]})"
        self._ai_used()
        return verdict, None

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
        self.state["wire"] = self.state["wire"][:KEEP_WIRE]
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
        wire = {"t": _iso(now), "wallet": short(swap.wallet), "side": "buy", "symbol": symbol, "usd": round(usd, 2),
                "delay": max(0, int(now - swap.time)) if swap.time else None, "case": None, "outcome": "watching"}
        self.state["wire"].insert(0, wire)
        case = self._open_case(swap, symbol)
        wire["case"] = case["id"]
        delay = wire["delay"]
        self._say(case, "FOMO Link", f"{short(swap.wallet)} just bought ${usd:,.0f} of {symbol}"
                  f"{f' at ${their_price:.8g}' if their_price else ''}{f' ({delay}s ago)' if delay is not None else ''}.")
        self._say(case, "Front Desk", f"Case {case['id']} opened. Council: check it.", to="Team")

        def close(status: str, why: str, votes=None) -> None:
            case["status"] = status
            wire["outcome"] = {"blocked": "blocked", "rejected": "rejected", "cancelled": "cancelled", "skipped": "skipped"}.get(status, status)
            self._event("skip", **base, case=case["id"], votes=votes_json(votes or []), text=f"Skipped {symbol} that {short(swap.wallet)} bought. {why}")

        if len(self.state["positions"]) >= self.config["max_positions"]:
            self._say(case, "Manager", f"Already holding {self.config['max_positions']} tokens. Passing.", "veto", "Team")
            close("skipped", f"Already holding {self.config['max_positions']} tokens.")
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
        scout_note: dict = {}

        def scout():
            report, why = self._scout(symbol, swap.mint, "entry", 2 * 3600)
            scout_note.update(report=report, why=why)
            return report
        approved, mult, votes = buy_council(situation, hype=scout)
        for v in votes:
            if v.agent == "Social Scout":
                if scout_note.get("report"):
                    self._say(case, "Social Scout", describe_report(scout_note["report"]), v.verdict)
                self._say(case, "Social Scout", v.text if not scout_note.get("why") else f"Couldn't scroll socials: {scout_note['why']}.",
                          v.verdict)
            else:
                self._say(case, v.agent, v.text, v.verdict)
        if not approved:
            veto = next(v for v in votes if v.verdict == "veto")
            self._say(case, "Front Desk", f"Blocked by {veto.agent}. Case closed.", "veto", "Team")
            close("blocked", f"{veto.agent}: {veto.text}", votes)
            return None

        case_file = {"trade": {"trader": short(swap.wallet), "rank": situation.rank, "token": symbol, "their_usd": round(usd, 2),
                               "their_price": their_price, "price_now": q.price, "seconds_behind": delay,
                               "pool_liquidity_usd": q.liquidity, "price_change_1h": q.change_h1, "price_change_24h": q.change_h24},
                     "council": votes_json(votes), "social_scout": scout_note.get("report"),
                     "trader_copy_record": situation.record, "risk_off": situation.risk_off, "fomo_heat": situation.heat,
                     "proposed_size_pct_of_fund": round(self.config["slot_pct"] * mult, 4)}
        second, why = self._review("entry", case_file)
        if second is None:
            self._say(case, "Second Opinion", f"No second opinion: {why}. The council's checks stand.", "info")
        else:
            verdict = "go" if second["decision"] == "approve" else "veto"
            self._say(case, "Second Opinion", f"{'Approve' if verdict == 'go' else 'Reject'} ({second['confidence']:.0%} sure): {second['reason']}", verdict)
            if verdict == "veto":
                self._say(case, "Front Desk", "Second Opinion rejected it. Case closed.", "veto", "Team")
                close("rejected", f"Second Opinion: {second['reason']}", votes)
                return None

        # The double-check: a fresh price, right before buying.
        try:
            fresh = self.dex.quotes([swap.mint]).get(swap.mint) or q
        except SourceError:
            fresh = q
        jump = fresh.price / q.price - 1 if q.price else 0.0
        if jump > RECHECK_UP or fresh.liquidity < q.liquidity * RECHECK_LIQ:
            why = (f"price jumped {jump:.0%} while we checked" if jump > RECHECK_UP
                   else f"the pool lost {1 - fresh.liquidity / q.liquidity:.0%} of its money while we checked")
            self._say(case, "Front Desk", f"Double-check failed: {why}. Not buying.", "veto", "Team")
            close("cancelled", f"Double-check: {why}.", votes)
            return None
        self._say(case, "Front Desk", f"Double-checked: price {jump:+.1%} since the council looked, pool steady. Over to the Manager.", "go", "Manager")
        q = fresh

        nav = self._nav()
        size = nav * self.config["slot_pct"] * mult
        size = min(size, self.state["cash"], q.liquidity * MAX_POOL_SHARE)
        if size < 5:
            self._say(case, "Manager", "Not enough cash left. Passing.", "veto", "Team")
            close("skipped", "Not enough cash left.", votes)
            return None
        cluster = situation.holders_buying >= 2
        slip = BASE_SLIPPAGE + size / q.liquidity
        fill = q.price * (1 + slip)
        fee = size * FEE
        qty = (size - fee) / fill
        lag = fill / their_price - 1 if their_price else None
        self.state["cash"] -= size
        self.state["fees"] += fee
        self.state["copies"] += 1
        self.state["positions"][swap.mint] = {
            "symbol": symbol, "qty": qty, "cost": size, "entry": fill, "last": q.price, "peak": q.price,
            "opened": now, "from": [swap.wallet], "their_price": their_price, "lag": lag, "no_price_since": None,
            "realized": 0.0, "checkpoints": [], "case": case["id"],
        }
        case["status"] = "holding"
        wire["outcome"] = "copied"
        self._say(case, "Manager", f"Bought ${size:,.0f} of {symbol} at ${fill:.8g} (team size x{mult:.2f}"
                  f"{f', {lag:+.1%} vs their price' if lag is not None else ''}). Exit Desk reviews at +50% and +100%.", "go", "Team")
        return self._event("buy", **base, usd=round(size, 2), price=fill, lag=lag, delay=delay, cluster=cluster,
                           mult=round(mult, 2), votes=votes_json(votes), case=case["id"],
                           text=f"Copied {short(swap.wallet)}: bought ${size:,.0f} of {symbol}"
                                f"{' (2+ top traders buying)' if cluster else ''}"
                                f"{f', {lag:+.1%} vs their price' if lag is not None else ''}"
                                f"{f', {delay}s behind' if delay is not None else ''}. Team size x{mult:.2f}.")

    def _on_sell(self, swap: Swap, quotes: dict) -> dict | None:
        held = self.state["holdings"].setdefault(swap.wallet, {})
        before = held.get(swap.mint, 0.0)
        held[swap.mint] = max(0.0, before - swap.amount)
        pos = self.state["positions"].get(swap.mint)
        q = quotes.get(swap.mint)
        if swap.wallet in self.state["followed"] or pos:
            usd = self._usd(swap)
            self.state["wire"].insert(0, {"t": _iso(self.clock()), "wallet": short(swap.wallet), "side": "sell",
                                          "symbol": pos["symbol"] if pos else (q.symbol if q else swap.mint[:6]),
                                          "usd": round(usd, 2) if usd else None, "delay": None,
                                          "case": pos.get("case") if pos else None,
                                          "outcome": "we sell too" if pos and swap.wallet in pos["from"] else "we don't hold it"})
        if not pos or swap.wallet not in pos["from"]:
            return None
        self._say(self._case(pos.get("case")), "FOMO Link", f"{short(swap.wallet)} is selling {pos['symbol']}.")
        share = min(1.0, swap.amount / before) if before > 0 else 1.0
        if share < 0.2:
            self._say(self._case(pos.get("case")), "Manager", f"They only trimmed ({share:.0%}). Holding.", "info", "Team")
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
        case = self._case(pos.get("case"))
        self._say(case, "Manager", f"Sold {'all' if share >= 0.999 else f'{share:.0%}'} ({why}): {'+' if profit >= 0 else '-'}${abs(profit):,.0f}.",
                  "info", "Team")
        if share >= 0.999:
            rec["closed"] += 1
            rec["wins"] += 1 if pos["realized"] > 0 else 0
            if case is not None:
                case["status"] = "closed"
                case["result"] = {"profit": round(pos["realized"], 2)}
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
                out.append(self._sell(mint, share, quotes, why))
                continue
            gain = q.price / pos["entry"] - 1 if pos["entry"] else 0.0
            level = next_checkpoint(pos, gain)
            if level is not None:
                event = self._checkpoint(mint, pos, q, gain, level, quotes)
                if event:
                    out.append(event)
                    continue
            if q.price <= pos["entry"] * (1 - self.config["stop_loss"]):
                out.append(self._sell(mint, 1.0, quotes, f"stop-loss at {-self.config['stop_loss']:.0%}"))
            elif now - pos["opened"] > TIME_LIMIT_S:
                out.append(self._sell(mint, 1.0, quotes, "held 14 days"))
        return out

    def _checkpoint(self, mint: str, pos: dict, q, gain: float, level: float, quotes: dict) -> dict | None:
        """A held copy hit a profit checkpoint: the Exit Desk decides, with a
        fresh look at socials and whether the traders we copied still hold."""
        pos["checkpoints"] = sorted(set(pos.get("checkpoints") or []) | {c for c in (0.5, 1.0, 2.0, 4.0, 8.0) if c <= level})
        case = self._case(pos.get("case"))
        self._say(case, "Front Desk", f"{pos['symbol']} is up {gain:+.0%}. Exit Desk: hold or withdraw?", to="Exit Desk")
        still = {short(w): (self.state["holdings"].get(w, {}).get(mint, 0.0) > 0) for w in pos["from"]}
        self._say(case, "FOMO Link", "Traders we copied: " + ", ".join(f"{w} {'still holding' if h else 'sold out'}" for w, h in still.items()) + ".")
        report, why = self._scout(pos["symbol"], mint, "review", 1800)
        self._say(case, "Social Scout", describe_report(report) if report else f"Couldn't scroll socials: {why}.")
        case_file = {"position": {"token": pos["symbol"], "gain_pct": round(gain, 4), "peak_gain_pct": round(pos["peak"] / pos["entry"] - 1, 4),
                                  "price_change_1h": q.change_h1, "price_change_24h": q.change_h24, "pool_liquidity_usd": q.liquidity,
                                  "held_hours": round((self.clock() - pos["opened"]) / 3600, 1), "already_took_half": bool(pos.get("took_profit"))},
                     "copied_traders_still_holding": still, "social_scout": report,
                     "risk_off": self.state["risk_off"], "fomo_heat": self.state["heat"]}
        verdict, why = self._review("exit", case_file)
        if verdict is None:
            # No reviewer: a plain rule. Take half at +100% (once); otherwise ride with the trailing stop.
            decision = "take_half" if level >= 1.0 and not pos.get("took_profit") else "hold"
            if report and (report["pump_risk"] >= 0.6 or report["sentiment"] <= -0.3) and decision == "hold":
                decision = "take_half"
            reason = f"no AI reviewer ({why}); rule: {'take half at +100%' if level >= 1.0 else 'hold, trailing stop protects the gain'}"
        else:
            decision, reason = verdict["decision"], verdict["reason"]
        if not any(still.values()) and decision != "exit":
            decision, reason = "exit", reason + " Overruled: every trader we copied has already sold out."
        self._say(case, "Exit Desk", f"{decision.replace('_', ' ').capitalize()}: {reason}",
                  {"hold": "go", "take_half": "caution", "exit": "veto"}[decision], "Manager")
        if decision == "hold":
            return None
        if decision == "take_half":
            if pos.get("took_profit"):
                return None
            pos["took_profit"] = True
            return self._sell(mint, 0.5, quotes, f"Exit Desk at {gain:+.0%}: taking half")
        return self._sell(mint, 1.0, quotes, f"Exit Desk at {gain:+.0%}: withdrawing")

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
            "cases": self.state["cases"][:60], "wire": self.state["wire"][:80],
            "ai_calls": self.state["ai_calls"], "scout_ready": bool(self.scout is not None and getattr(self.scout, "ready", True)),
            "reviewer_ready": self.reviewers is not None,
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
