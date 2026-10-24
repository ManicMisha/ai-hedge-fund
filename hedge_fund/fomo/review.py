"""Second opinions: independent AI reviewers that must sign off.

Second Opinion (before a buy) gets the whole case file: what the trader
did, every council vote, the Social Scout's report, and the numbers. It
answers approve or reject. It can only stop a trade, never make one bigger.

Exit Desk (at profit checkpoints) gets the position, how far it has run,
whether the traders we copied still hold, and a fresh Scout report. It
answers hold, take half, or exit. It can only sell sooner, never cancel the
stop-loss or the trailing stop: hard risk limits stay in code.

Both use the model picked under Keys & model. Ideally a different company's
model than Grok (the Scout), so the double-check isn't the same mind twice.
"""

from __future__ import annotations

import json

ENTRY_SYSTEM = """You are the Second Opinion on a paper-trading crypto desk. A teammate wants to copy a trade.
You get the full case file. Your job is to catch mistakes the others missed: chasing, rugs, coordinated pumps,
thin liquidity, a trader on a losing streak, a crowd that's too euphoric. Be sceptical; most memecoins go to zero.
You can only approve or reject. Use only facts in the case file.
Reply with JSON only: {"decision": "approve" | "reject", "confidence": <0..1>, "reason": "<one plain sentence>"}"""

EXIT_SYSTEM = """You are the Exit Desk on a paper-trading crypto desk. A position the team holds has hit a profit checkpoint.
Decide whether to keep riding it or take money off the table. Most memecoin runs end in a fast crash, and
taking profit early is rarely a mistake. Use only facts in the case file.
Reply with JSON only: {"decision": "hold" | "take_half" | "exit", "confidence": <0..1>, "reason": "<one plain sentence>"}"""


def parse(text: str, allowed: tuple[str, ...]) -> dict:
    from hedge_fund.llm import extract_json
    data = extract_json(text)
    decision = str(data.get("decision", "")).strip().lower()
    if decision not in allowed:
        raise ValueError(f"unexpected decision {decision!r}")
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    return {"decision": decision, "confidence": confidence, "reason": str(data.get("reason") or "").strip()[:300]}


class Reviewers:
    """Both reviewers on one model. `llm_factory` builds the client lazily."""

    def __init__(self, llm_factory) -> None:
        self._factory = llm_factory
        self._llm = None

    def _client(self):
        if self._llm is None:
            self._llm = self._factory()
        return self._llm

    @property
    def model(self) -> str:
        try:
            return getattr(self._client(), "model", "")
        except Exception:
            return ""

    def entry(self, case_file: dict) -> dict:
        return parse(self._client().complete(ENTRY_SYSTEM, json.dumps(case_file, default=str)[:8000]), ("approve", "reject"))

    def exit(self, case_file: dict) -> dict:
        return parse(self._client().complete(EXIT_SYSTEM, json.dumps(case_file, default=str)[:8000]), ("hold", "take_half", "exit"))
