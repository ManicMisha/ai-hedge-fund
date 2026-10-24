"""The intel board: every agent's latest word, from every desk, in one place.

Stock analysts, trackers, the risk officer, the market shield, the FOMO
desk's council and the manager all post here. Nothing new is stored: notes
are read from what each desk already records (fund ledgers, the FOMO desk's
trade feed), so the board can never disagree with what actually happened.

From the notes the board builds an agreement map: for each stock or token,
who is for it, who is against, and who vetoed, so you can see at a glance
where the team lines up and where it's split.
"""

from __future__ import annotations

from pathlib import Path

from hedge_fund.paper import Ledger, load_deployed

STRONG = 0.25  # weaker views than this are noise on the board

ROLES = {
    "stock": "Stock analyst", "tracker": "Smart-money tracker", "risk": "Risk control",
    "council": "FOMO council", "manager": "Manager",
}
CRYPTO_AGENTS = {
    "Copy Scout": "Finds the trade: which top FOMO trader bought what",
    "Pool Guard": "Checks there's enough money in the pool to get out",
    "Too-Late Check": "Stops us buying after the move already happened",
    "Crowd Count": "Counts how many top traders are piling in",
    "Track Record": "Remembers whose trades made us money, benches the rest",
    "Market Shield": "Cuts risk when SOL or the S&P 500 turns down",
    "FOMO Meter": "Measures crowd euphoria on FOMO",
    "Hype Watch": "Searches X for coordinated pumping",
}
_VERDICT = {"go": "bull", "boost": "bull", "veto": "veto", "caution": "caution", "info": "info"}
# Safety checks: their "go" means "no objection", not "I like it".
GATEKEEPERS = {"Pool Guard", "Too-Late Check", "Market Shield", "FOMO Meter", "Hype Watch", "Track Record"}


def _name(agent_id: str) -> tuple[str, str]:
    from hedge_fund.web.views import analyst_view
    view = analyst_view(agent_id)
    return view["name"], ("tracker" if view["kind"] == "tracker" else "stock")


def stock_notes(directory: Path) -> list[dict]:
    """The latest decision of one paper fund, as notes."""
    deployed = load_deployed(directory)
    records = Ledger(directory).records()
    decision = next((r.decision for r in reversed(records) if r.decision is not None), None)
    if decision is None:
        return []
    when, desk = decision.as_of, deployed.name
    notes = []
    for sr in decision.strategies:
        for sig in sr.signals:
            if (sig.metadata or {}).get("abstained") or abs(sig.value) < STRONG:
                continue
            name, kind = _name(sig.model_name)
            notes.append({"t": when, "desk": desk, "agent": name, "kind": kind, "subject": sig.ticker,
                          "stance": "bull" if sig.value > 0 else "bear", "strength": round(abs(sig.value), 2),
                          "text": (sig.reasoning or "")[:240]})
    for c in decision.clamps:
        if c.limit == "pump_guard":
            notes.append({"t": when, "desk": desk, "agent": "Pump Guard", "kind": "risk", "subject": c.ticker, "stance": "veto",
                          "strength": 1.0, "text": f"{c.ticker} spiked on heavy volume: standing aside."})
        elif c.limit == "market_filter":
            notes.append({"t": when, "desk": desk, "agent": "Market Shield", "kind": "risk", "subject": None, "stance": "caution",
                          "strength": 1.0, "text": f"S&P 500 below its 200-day average: invested cut from {c.before:.0%} to {c.after:.0%}."})
        else:
            notes.append({"t": when, "desk": desk, "agent": "Risk Officer", "kind": "risk", "subject": c.ticker, "stance": "caution",
                          "strength": 0.5, "text": f"Capped {c.ticker or 'total exposure'} from {c.before:.0%} to {c.after:.0%}."})
    held = sorted(((t, w) for t, w in decision.final_weights.items() if abs(w) > 1e-6), key=lambda kv: -abs(kv[1]))
    notes.append({"t": when, "desk": desk, "agent": "Manager", "kind": "manager", "subject": None, "stance": "info", "strength": 0.0,
                  "text": "Plan: " + (", ".join(f"{t} {w:.0%}" for t, w in held[:6]) if held else "all cash") + "."})
    return notes


def crypto_notes(events: list[dict], risk_off: list[str] | None = None) -> list[dict]:
    notes = []
    for e in events:
        subject = e.get("symbol")
        for v in e.get("votes") or []:
            stance = _VERDICT.get(v["verdict"], "info")
            if stance == "bull" and v["verdict"] == "go" and v["agent"] in GATEKEEPERS:
                stance = "clear"
            notes.append({"t": e["t"], "desk": "FOMO desk", "agent": v["agent"], "kind": "council", "subject": subject,
                          "stance": stance, "strength": round(min(1.0, abs(v.get("multiplier", 1) - 1) + 0.5), 2),
                          "text": v["text"], "in_feed": False})  # shown as chips on the manager's message
        if e["kind"] in ("buy", "sell", "skip", "follow"):
            notes.append({"t": e["t"], "desk": "FOMO desk", "agent": "Manager", "kind": "manager", "subject": subject,
                          "stance": {"buy": "bull", "sell": "info", "skip": "veto"}.get(e["kind"], "info"), "strength": 0.0,
                          "text": e["text"], "votes": e.get("votes") or []})
    for reason in risk_off or []:
        notes.append({"t": events[0]["t"] if events else "", "desk": "FOMO desk", "agent": "Market Shield", "kind": "council",
                      "subject": None, "stance": "caution", "strength": 1.0, "text": f"Risk-off: {reason}."})
    return notes


def agreement(notes: list[dict]) -> list[dict]:
    """Per stock or token: the latest stance of each agent, and the verdict."""
    latest: dict[tuple[str, str, str], dict] = {}
    for n in sorted(notes, key=lambda n: n["t"]):
        if not n["subject"] or n["agent"] == "Manager" or n["stance"] == "info":
            continue
        latest[(n["subject"], n["desk"], n["agent"])] = n
    by_subject: dict[str, dict] = {}
    for (subject, desk, agent), n in latest.items():
        row = by_subject.setdefault(subject, {"subject": subject, "desks": set(), "bull": [], "bear": [], "veto": [], "caution": [],
                                              "clear": [], "last": ""})
        row["desks"].add(desk)
        row[n["stance"]].append(agent)
        row["last"] = max(row["last"], n["t"])
    rows = []
    for row in by_subject.values():
        bulls, bears, vetoes = len(row["bull"]), len(row["bear"]), len(row["veto"])
        if vetoes:
            verdict = "Blocked"
        elif not bears and (bulls >= 3 or (bulls >= 2 and len(row["clear"]) >= 3 and not row["caution"])):
            verdict = "Team agrees: buy"
        elif bulls and bears:
            verdict = "Split"
        elif bears > bulls:
            verdict = "Team agrees: avoid"
        elif bulls:
            verdict = "Leaning buy"
        else:
            verdict = "Cautious"
        rows.append({**row, "desks": sorted(row["desks"]), "verdict": verdict, "score": bulls - bears - 2 * vetoes})
    return sorted(rows, key=lambda r: (r["last"], r["score"]), reverse=True)


def roster(notes: list[dict]) -> list[dict]:
    agents: dict[str, dict] = {}
    for n in sorted(notes, key=lambda n: n["t"]):
        a = agents.setdefault(n["agent"], {"agent": n["agent"], "kind": n["kind"], "notes": 0, "last": None, "desks": set()})
        a["notes"] += 1
        a["last"] = n
        a["desks"].add(n["desk"])
    out = []
    for a in agents.values():
        role = CRYPTO_AGENTS.get(a["agent"]) or ROLES.get(a["kind"], "")
        out.append({**a, "desks": sorted(a["desks"]), "role": role})
    return sorted(out, key=lambda a: (a["kind"] != "manager", a["agent"]))


def board(paper_dirs: list[Path], fomo_events: list[dict], risk_off: list[str] | None = None, limit: int = 150) -> dict:
    notes: list[dict] = []
    for directory in paper_dirs:
        try:
            notes += stock_notes(directory)
        except Exception:  # a broken fund shows its error on its own card
            continue
    notes += crypto_notes(fomo_events, risk_off)
    notes.sort(key=lambda n: n["t"], reverse=True)
    feed = [n for n in notes if n.get("in_feed", True)]
    return {"notes": feed[:limit], "agreement": agreement(notes)[:40], "agents": roster(notes)}
