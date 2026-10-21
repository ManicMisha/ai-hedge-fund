"""Trade alerts: the moments worth a ping, derived from each fund's ledger.

Like the game layer, alerts are computed from what the ledger already holds,
so they can't drift and need no extra bookkeeping beyond "which ones have I
already sent". An alert fires when something *changes*: a tracker sees
more insiders buying than at the last decision, the pump guard trips on a
stock it wasn't guarding, the market shield goes up or comes down. Steady
states don't repeat every day.

Phone alerts go through ntfy (https://ntfy.sh), a free push service: install
the ntfy app, subscribe to a private topic name, and put the same name in
NTFY_TOPIC. Anyone who knows the topic name can read the alerts, so pick
one that's hard to guess (the dashboard suggests one). Alerts say which
fund and stock, never your keys or balances beyond the fund's paper value.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import requests

from hedge_fund.paper import Ledger, load_deployed
from hedge_fund.paths import RESEARCH_DIR, USER_DIR, write_atomic
from hedge_fund.settings import load_settings

SENT_PATH = USER_DIR / "alerts-sent.json"
NTFY_TOPIC_VAR = "NTFY_TOPIC"
NTFY_SERVER_VAR = "NTFY_SERVER"
DEFAULT_NTFY = "https://ntfy.sh"
BIG_DAY = 0.03
DRAWDOWN_LEVELS = (0.10, 0.20, 0.30)
_KEEP_SENT = 400

LABELS = {
    "insider": "Insider buying", "fund_buy": "Famous fund buying", "congress": "Congress buying",
    "hype": "Hype warning", "pump": "Pump alarm", "shield": "Market shield", "big_day": "Big day",
    "drawdown": "Drawdown", "frozen": "Fund frozen", "golive": "Go-live check",
}
_TAGS = {"good": "chart_with_upwards_trend", "warn": "warning", "bad": "rotating_light", "info": "bell"}


def _alert(fund: str, session: str, kind: str, level: str, title: str, body: str, ticker: str | None = None) -> dict:
    return {"id": f"{fund}:{session}:{kind}:{ticker or ''}", "fund": fund, "session": session, "kind": kind,
            "label": LABELS[kind], "level": level, "ticker": ticker, "title": title, "body": body[:300]}


def fund_alerts(fund: str, records: list, events: list[dict], capital: float) -> list[dict]:
    """Every alert in the fund's history, oldest first."""
    out: list[dict] = []
    prev_signals: dict[tuple[str, str], object] = {}
    prev_pumps: set[str] = set()
    prev_shield = False
    prev_nav = capital
    peak = capital
    crossed: set[float] = set()

    for r in records:
        day = r.session
        if prev_nav:
            move = r.nav / prev_nav - 1
            if abs(move) >= BIG_DAY:
                good = move > 0
                out.append(_alert(fund, day, "big_day", "good" if good else "bad",
                                  f"{fund} {'jumped' if good else 'fell'} {abs(move):.1%} today",
                                  f"Paper value ${r.nav:,.0f} at the {day} close."))
        prev_nav = r.nav
        peak = max(peak, r.nav)
        drop = 1 - r.nav / peak if peak else 0.0
        for level in DRAWDOWN_LEVELS:
            if drop >= level and level not in crossed:
                crossed.add(level)
                out.append(_alert(fund, day, "drawdown", "bad", f"{fund} is down {drop:.0%} from its high",
                                  "A good moment to check the plan, not to panic. The S&P 500 itself has fallen "
                                  "more than 30% several times."))

        decision = r.decision
        if decision is None:
            continue
        signals = {(s.model_name, s.ticker): s for sr in decision.strategies for s in sr.signals}
        for (model, ticker), sig in signals.items():
            active = not (sig.metadata or {}).get("abstained")
            before = prev_signals.get((model, ticker))
            before_active = before is not None and not (before.metadata or {}).get("abstained")
            comp = sig.components or {}
            old = (before.components or {}) if before_active else {}
            if not active:
                continue
            if model == "insiders" and sig.value > 0:
                buyers, c_suite = comp.get("buyers", 0), comp.get("c_suite", 0)
                if buyers > old.get("buyers", 0) and (buyers >= 3 or c_suite >= 1):
                    who = "a cluster of insiders" if buyers >= 3 else "the CEO/CFO"
                    out.append(_alert(fund, day, "insider", "good", f"{ticker}: {who} buying", sig.reasoning, ticker))
            elif model == "superinvestors" and sig.value > 0:
                if comp.get("new_positions", 0) > old.get("new_positions", 0) or comp.get("holders", 0) > old.get("holders", 0):
                    out.append(_alert(fund, day, "fund_buy", "good", f"{ticker}: a famous fund is buying", sig.reasoning, ticker))
            elif model == "congress" and sig.value > 0:
                if comp.get("members", 0) > old.get("members", 0):
                    out.append(_alert(fund, day, "congress", "good", f"{ticker}: Congress members buying", sig.reasoning, ticker))
            elif model == "social_watch" and sig.value < 0:
                if not (before_active and before.value < 0):
                    out.append(_alert(fund, day, "hype", "warn", f"{ticker}: pump chatter on X", sig.reasoning, ticker))
        prev_signals.update(signals)

        pumps = {c.ticker for c in decision.clamps if c.limit == "pump_guard" and c.ticker}
        for ticker in sorted(pumps - prev_pumps):
            out.append(_alert(fund, day, "pump", "warn", f"{ticker}: pump alarm",
                              f"{ticker} spiked on unusually heavy trading. {fund} stays out of it for now.", ticker))
        prev_pumps = pumps
        shield = any(c.limit == "market_filter" for c in decision.clamps)
        if shield != prev_shield:
            out.append(_alert(fund, day, "shield", "warn" if shield else "good",
                              f"Market shield {'up' if shield else 'down'} for {fund}",
                              "The S&P 500 fell below its 200-day average: the fund cuts how much it has invested."
                              if shield else "The S&P 500 is back above its 200-day average: the fund invests fully again."))
        prev_shield = shield

    for event in events:
        if event.get("kind") == "tick_failed":
            when = str(event.get("time", ""))[:10]
            out.append(_alert(fund, str(event.get("session") or when), "frozen", "bad", f"{fund} froze itself",
                              f"A day failed to play ({event.get('error', 'unknown error')}). Nothing trades until "
                              "you fix it and press Resume."))
    return out


def all_alerts(paper_dirs: list[Path], limit: int = 100, research_dir: Path = RESEARCH_DIR) -> list[dict]:
    rows: list[dict] = []
    for directory in paper_dirs:
        try:
            rows += alerts_for_dir(directory, research_dir)
        except Exception:  # a broken fund shows its error on its own card
            continue
    rows.sort(key=lambda a: (a["session"], a["id"]), reverse=True)
    return rows[:limit]


def alerts_for_dir(directory: Path, research_dir: Path = RESEARCH_DIR) -> list[dict]:
    deployed = load_deployed(directory)
    ledger = Ledger(directory)
    rows = fund_alerts(deployed.name, ledger.records(), ledger.events(), deployed.spec.capital)
    from hedge_fund.web.scorecard import golive_alert  # late: web imports this module
    passed = golive_alert(deployed, ledger, research_dir)
    if passed:
        rows.append(passed)
    return rows


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _load_sent(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def dispatch(directory: Path, *, send: Callable[[dict], None] | None = None,
             sent_path: Path = SENT_PATH, settings: dict | None = None,
             research_dir: Path = RESEARCH_DIR) -> list[dict]:
    """Send this fund's alerts that haven't gone out yet. The first time a fund
    is seen, its history is marked as sent without sending, so turning alerts
    on doesn't replay months of old news. Returns what was sent."""
    settings = settings or load_settings()
    fund = Path(directory).name
    current = alerts_for_dir(directory, research_dir)
    sent = _load_sent(sent_path)
    known = sent.get(fund)
    fresh = [] if known is None else [a for a in current if a["id"] not in set(known)]
    wanted = [a for a in fresh if a["kind"] in settings["alerts"]["kinds"]]
    sender = send or (push_phone if settings["alerts"]["phone"] else None)
    delivered = []
    for alert in wanted:
        if sender is None:
            break
        sender(alert)
        delivered.append(alert)
    ids = (known or []) + [a["id"] for a in current if a["id"] not in set(known or [])]
    sent[fund] = ids[-_KEEP_SENT:]
    Path(sent_path).parent.mkdir(parents=True, exist_ok=True)
    write_atomic(Path(sent_path), json.dumps(sent))
    return delivered


def ntfy_target() -> str | None:
    topic = os.environ.get(NTFY_TOPIC_VAR, "").strip()
    if not topic:
        return None
    server = os.environ.get(NTFY_SERVER_VAR, "").strip() or DEFAULT_NTFY
    return f"{server.rstrip('/')}/{topic}"


def _header(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii").replace("?", "-")[:120]


def push_phone(alert: dict, session: requests.Session | None = None) -> None:
    """One ntfy push. Raises on failure so the caller can report it."""
    target = ntfy_target()
    if target is None:
        raise RuntimeError("no phone alert topic set (NTFY_TOPIC)")
    http = session or requests
    resp = http.post(target, data=alert["body"].encode("utf-8"), timeout=15, headers={
        "Title": _header(alert["title"]),
        "Tags": _TAGS.get(alert["level"], "bell"),
        "Priority": "high" if alert["level"] == "bad" else "default",
    })
    if resp.status_code >= 300:
        raise RuntimeError(f"ntfy answered HTTP {resp.status_code}")
