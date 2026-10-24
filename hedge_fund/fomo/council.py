"""The council: every agent weighs in before the desk copies a trade.

A followed trader buying is only the start. Before the desk follows, each
agent looks at the trade from its own angle and votes:

  Copy Scout      who bought, how much, and where they rank
  Pool Guard      is there enough money in the pool to get in and out?
  Too-Late Check  has the price already run away from what they paid?
  Crowd Count     are other top traders buying the same token?
  Track Record    have this trader's past copies made us money?
  Market Shield   is the wider market risk-off (SOL falling, or the stock
                  side's S&P 500 shield up)?
  FOMO Meter      is the FOMO crowd euphoric right now?
  Hype Watch      does X look like a coordinated pump? (asked last, and
                  only if nobody vetoed, because it costs a few cents)

Any veto stops the copy. Otherwise the manager multiplies the agents' size
adjustments together and keeps the result between a quarter and double the
normal slot. Every vote is recorded with the trade, so you can read exactly
why the team did what it did.

The same agents watch the exits: take half off when a copy doubles, sell
the rest if it falls 35% from its high (25% when the market is risk-off or
the crowd is euphoric), and take half off if Hype Watch flags a held token.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import requests

LAG_VETO = 0.15
LAG_CAUTION = 0.07
CHASE_H1 = 0.60
SOL_RISK_OFF_H24 = -0.08
EUPHORIA = 2.5
QUIET = 0.5
BENCH_MIN_CLOSED = 3
BENCH_WIN_RATE = 0.35
TRUSTED_WIN_RATE = 0.6
MIN_MULT, MAX_MULT = 0.25, 2.0

TAKE_PROFIT_AT = 1.0       # +100%: take half off
TRAIL_ARMED_AT = 0.5       # once up 50%, a trailing stop protects the gain
TRAIL = 0.35
TRAIL_TIGHT = 0.25
HYPE_VETO = 0.6
HYPE_CAUTION = 0.4


@dataclass
class Vote:
    agent: str
    verdict: str            # "go", "boost", "caution", "veto" or "info"
    text: str
    multiplier: float = 1.0


@dataclass
class Situation:
    """What the council knows about one candidate copy."""
    symbol: str
    wallet_label: str
    usd: float
    rank: int | None                      # 0 = top followed trader
    their_price: float | None
    price: float | None                   # now, before our slippage
    liquidity: float
    pool_age_s: float | None
    change_h1: float | None
    holders_buying: int                   # followed traders who bought it in the last day
    record: dict                          # this trader's copy record: closed, wins, pnl
    risk_off: list[str] = field(default_factory=list)
    heat: float | None = None             # FOMO volume now vs normal
    min_liquidity: float = 30_000.0
    min_pool_age_s: float = 600.0
    slot_usd: float = 0.0


def buy_council(s: Situation, hype: Callable[[], dict | None] | None = None) -> tuple[bool, float, list[Vote]]:
    votes: list[Vote] = []
    rank = f"#{s.rank + 1} on the leaderboard" if s.rank is not None else "a trader we still hold copies from"
    votes.append(Vote("Copy Scout", "boost" if s.rank is not None and s.rank < 3 else "go",
                      f"{s.wallet_label} ({rank}) bought ${s.usd:,.0f} of {s.symbol}.",
                      1.25 if s.rank is not None and s.rank < 3 else 1.0))

    if s.price is None:
        votes.append(Vote("Pool Guard", "veto", "No price anywhere: can't value it, can't get out."))
    elif s.liquidity < s.min_liquidity:
        votes.append(Vote("Pool Guard", "veto", f"Pool holds only ${s.liquidity:,.0f}; selling would crash the price."))
    elif s.pool_age_s is not None and s.pool_age_s < s.min_pool_age_s:
        votes.append(Vote("Pool Guard", "veto", "Launched minutes ago: the riskiest moment for a rug pull."))
    else:
        votes.append(Vote("Pool Guard", "go", f"${s.liquidity:,.0f} in the pool, enough to trade."))

    lag = s.price / s.their_price - 1 if s.price and s.their_price else None
    if lag is not None and lag > LAG_VETO:
        votes.append(Vote("Too-Late Check", "veto", f"Already {lag:.0%} above what they paid. We'd be their exit."))
    elif s.change_h1 is not None and s.change_h1 > CHASE_H1:
        votes.append(Vote("Too-Late Check", "veto", f"Up {s.change_h1:.0%} in the last hour: chasing, not following."))
    elif lag is not None and lag > LAG_CAUTION:
        votes.append(Vote("Too-Late Check", "caution", f"{lag:.0%} above their price: smaller bet.", 0.75))
    else:
        votes.append(Vote("Too-Late Check", "go", f"Close to their price ({lag:+.1%})." if lag is not None else "Their price unknown."))

    if s.holders_buying >= 2:
        votes.append(Vote("Crowd Count", "boost", f"{s.holders_buying} top traders bought it in the last day.", 1.5))
    else:
        votes.append(Vote("Crowd Count", "info", "Only this trader so far."))

    closed, wins, pnl = s.record.get("closed", 0), s.record.get("wins", 0), s.record.get("pnl", 0.0)
    if closed >= BENCH_MIN_CLOSED and (wins / closed < BENCH_WIN_RATE or pnl < 0):
        votes.append(Vote("Track Record", "veto", f"Copying this trader has lost us ${-pnl:,.0f} over {closed} trades ({wins} wins). Benched."
                          if pnl < 0 else f"Only {wins} of {closed} copies of this trader won. Benched."))
    elif closed >= BENCH_MIN_CLOSED and wins / closed >= TRUSTED_WIN_RATE:
        votes.append(Vote("Track Record", "boost", f"{wins} of {closed} copies of this trader won (+${pnl:,.0f}).", 1.25))
    else:
        votes.append(Vote("Track Record", "info", f"{closed} closed copies of this trader so far."))

    if s.risk_off:
        votes.append(Vote("Market Shield", "caution", "Risk-off: " + "; ".join(s.risk_off) + ". Half size.", 0.5))
    else:
        votes.append(Vote("Market Shield", "go", "Market calm."))

    if s.heat is not None and s.heat >= EUPHORIA:
        votes.append(Vote("FOMO Meter", "caution", f"FOMO buying is {s.heat:.1f}x normal: the crowd is euphoric, tops form here. Half size.", 0.5))
    elif s.heat is not None and s.heat <= QUIET:
        votes.append(Vote("FOMO Meter", "info", f"FOMO is quiet ({s.heat:.1f}x normal)."))
    else:
        votes.append(Vote("FOMO Meter", "info", "Crowd mood normal." if s.heat is not None else "Still learning what normal looks like."))

    if not any(v.verdict == "veto" for v in votes) and hype is not None:
        verdict = None
        try:
            verdict = hype()
        except Exception:
            verdict = None
        if verdict is None:
            votes.append(Vote("Hype Watch", "info", "Couldn't check X this time."))
        else:
            risk = float(verdict.get("pump_risk", 0))
            note = str(verdict.get("summary") or "")[:160]
            if risk >= HYPE_VETO:
                votes.append(Vote("Hype Watch", "veto", f"Pump chatter on X ({risk:.0%}): {note}"))
            elif risk >= HYPE_CAUTION:
                votes.append(Vote("Hype Watch", "caution", f"Some pump chatter ({risk:.0%}). Half size.", 0.5))
            else:
                votes.append(Vote("Hype Watch", "go", f"Nothing suspicious on X ({risk:.0%})."))

    approved = not any(v.verdict == "veto" for v in votes)
    mult = 1.0
    for v in votes:
        mult *= v.multiplier
    return approved, max(MIN_MULT, min(MAX_MULT, mult)), votes


def exit_checks(pos: dict, price: float, *, tight: bool) -> tuple[float, str] | None:
    """(share to sell, why) when the exit agents want out, else None."""
    gain = price / pos["entry"] - 1 if pos["entry"] else 0.0
    peak_gain = pos["peak"] / pos["entry"] - 1 if pos["entry"] else 0.0
    if gain >= TAKE_PROFIT_AT and not pos.get("took_profit"):
        return 0.5, f"doubled: taking half off the table"
    trail = TRAIL_TIGHT if tight else TRAIL
    if peak_gain >= TRAIL_ARMED_AT and price <= pos["peak"] * (1 - trail):
        return 1.0, f"fell {trail:.0%} from its high after a {peak_gain:+.0%} run: locking in"
    return None


def votes_json(votes: list[Vote]) -> list[dict]:
    return [asdict(v) for v in votes]


# ---------------------------------------------------------------------------
# Hype Watch for tokens: Grok searches X for coordinated pumping
# ---------------------------------------------------------------------------

CRYPTO_PROMPT = """You are a crypto market-surveillance analyst. Search X for posts from the last 48 hours about the Solana token {symbol} (contract {mint}).

Decide how strongly the chatter looks like a coordinated pump-and-dump: copy-pasted shilling, paid promoters, "next 100x" spam, bot-like accounts, Telegram/Discord pump groups, or influencers who are known to dump. Normal community excitement is NOT a pump.

Reply with JSON only:
{{"pump_risk": <number from 0 to 1>, "summary": "<one plain sentence on what you saw>"}}"""


def crypto_hype(symbol: str, mint: str, cache_dir: Path, *, key: str | None = None, session=None,
                cache_hours: float = 6.0, now: float | None = None) -> dict | None:
    """Grok's pump verdict for a token, cached for a few hours. None without an xAI key."""
    import time as _time
    from hedge_fund.signals.social import default_grok, parse_verdict, response_text, XAI_URL
    key = (key or os.environ.get("XAI_API_KEY", "")).strip()
    if not key:
        return None
    now = now if now is not None else _time.time()
    path = Path(cache_dir) / f"{re.sub(r'[^A-Za-z0-9]', '_', mint)[:60]}.json"
    try:
        cached = json.loads(path.read_text())
        if now - cached.get("at", 0) < cache_hours * 3600:
            return cached["verdict"]
    except (OSError, ValueError, KeyError):
        pass
    http = session or requests
    resp = http.post(XAI_URL, timeout=120, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                     json={"model": default_grok(), "tools": [{"type": "x_search"}],
                           "input": [{"role": "user", "content": CRYPTO_PROMPT.format(symbol=symbol, mint=mint)}]})
    if resp.status_code != 200:
        raise RuntimeError(f"xAI answered HTTP {resp.status_code}")
    verdict = parse_verdict(response_text(resp.json()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": now, "verdict": verdict}))
    return verdict
