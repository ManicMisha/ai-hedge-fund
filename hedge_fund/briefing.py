"""The manager's briefing: an AI reads the whole intel board and explains it.

Advisory only. The briefing never places, sizes or vetoes a trade: those
rules stay in code, where they can be tested and can't be talked out of a
stop-loss. The briefing's job is to tell you, in plain English, what the
team agrees on, where it's split, what the risks are, and what to watch.

One AI call per briefing, on the model picked under Keys & model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from hedge_fund.paths import write_atomic

SYSTEM = """You are the manager of a small paper-trading fund run by a team of AI and maths agents.
You receive the team's latest notes and the state of each desk. Write a short briefing for the owner,
who is not a finance professional.

Rules:
- Use only facts in the notes. Never invent prices, numbers, names or news.
- Plain English, short sentences, no jargon without a two-word explanation.
- Be honest about uncertainty. Never promise returns. This is paper money.
- Under 250 words. Use exactly these headings, each on its own line starting with "## ":
## Where the team agrees
## Where it's split
## Risks right now
## What I'd watch next
"""


def digest(board: dict, funds: list[dict], desk: dict | None) -> str:
    """A compact, factual summary of everything the team knows, for the model."""
    lines = ["DESKS:"]
    for f in funds:
        if f.get("error"):
            continue
        lead = f.get("lead")
        lines.append(f"- Stock fund {f['name']} ({f['mandate']}): value ${f['nav']:,.0f}, return {f['return_pct']:+.1%}"
                     + (f", vs S&P 500 {lead:+.1%}" if lead is not None else "") + f", {f['sessions']} days played"
                     + (", FROZEN" if f.get("halted") else ""))
    if desk:
        lines.append(f"- FOMO copy desk: value ${desk['nav']:,.0f}, return {desk['return_pct']:+.1%}"
                     + (f", vs holding SOL {desk['return_pct'] - desk['sol_return_pct']:+.1%}" if desk.get("sol_return_pct") is not None else "")
                     + f", {desk['copies']} copies, {len(desk['positions'])} open"
                     + (f", average lag cost {desk['avg_lag']:+.1%}" if desk.get("avg_lag") is not None else "")
                     + (f", risk-off: {'; '.join(desk['risk_off'])}" if desk.get("risk_off") else "")
                     + (f", FOMO crowd at {desk['heat']:.1f}x normal" if desk.get("heat") else ""))
    lines.append("\nAGREEMENT MAP (stock or token: verdict; for / against / vetoed):")
    for row in board["agreement"][:20]:
        lines.append(f"- {row['subject']}: {row['verdict']}; for: {', '.join(row['bull']) or '-'}; against: {', '.join(row['bear']) or '-'}"
                     f"; vetoed by: {', '.join(row['veto']) or '-'}; caution: {', '.join(row['caution']) or '-'}")
    lines.append("\nLATEST NOTES (newest first):")
    for n in board["notes"][:40]:
        lines.append(f"- [{n['t'][:16]}] {n['desk']} / {n['agent']} on {n['subject'] or 'the market'}: {n['text']}")
    return "\n".join(lines)[:12_000]


def write_briefing(llm, board: dict, funds: list[dict], desk: dict | None, out_dir: Path) -> dict:
    text = llm.complete(SYSTEM, digest(board, funds, desk)).strip()
    if not text:
        raise RuntimeError("the model returned an empty briefing")
    briefing = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": getattr(llm, "model", ""), "text": text[:4000]}
    out_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(out_dir / f"{briefing['t'][:19].replace(':', '-')}.json", json.dumps(briefing))
    return briefing


def latest_briefing(out_dir: Path) -> dict | None:
    files = sorted(Path(out_dir).glob("*.json"))
    for path in reversed(files):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return None
