"""Hype Watch: a warning light for pumps being pushed on X (Twitter).

It asks Grok, with xAI's live X search, whether a stock is being promoted in
the way pump-and-dumps are: coordinated posts, paid shilling, "to the moon"
spam, unrealistic price targets, brand-new accounts repeating each other.

It can only ever vote *against* a stock, never for it. Copying social-media
"top traders" is how retail traders become the exit liquidity for a pump, so
their posts are treated as a reason for caution, not a reason to buy. When it
finds nothing worrying it abstains and stays out of the vote.

It only runs on recent days: X search can't faithfully replay what the feed
looked like months ago, so in backtests it abstains. Each check is one paid
xAI call with a search (a few cents); results are cached per stock per day,
so a plan preview and the run that follows it pay once.
"""

from __future__ import annotations

import json
import os
import re
from typing import ClassVar

import requests

from hedge_fund.data.protocol import DataClient
from hedge_fund.data.sec import days_before
from hedge_fund.llm import load_api_models
from hedge_fund.models import Signal
from hedge_fund.paths import CACHE_DIR, write_atomic
from hedge_fund.signals.smart_money import _Tracker, is_recent

XAI_KEY_VAR = "XAI_API_KEY"
XAI_URL = "https://api.x.ai/v1/responses"

PROMPT = """You are a market-surveillance analyst. Search X for posts from {start} to {end} about the US stock {ticker} (cashtag ${ticker}).

Decide how strongly the chatter looks like a pump: coordinated or copy-pasted posts, paid promotion, "to the moon" or "next 10x" spam, unrealistic price targets, many new or low-follower accounts pushing the same message, or pump groups (Discord/Telegram) being advertised. Ordinary heavy discussion of a large company after earnings or news is NOT a pump.

Reply with JSON only, no other text:
{{"pump_risk": <number from 0 to 1>, "summary": "<one plain sentence on what you saw>"}}"""


def default_grok() -> str:
    return next((mid for _, mid, provider in load_api_models() if provider == "xAI"), "grok-4.7")


class SocialWatchModel(_Tracker):
    """Votes against a stock when X looks like it is being pumped; otherwise abstains."""

    investment_approach = "long_only"
    required_env: ClassVar[tuple[str, ...]] = ()  # optional: abstains without a key

    def __init__(self, *, threshold: float = 0.6, days: int = 7, model: str | None = None) -> None:
        if not 0 < threshold <= 1 or not 1 <= days <= 30:
            raise ValueError("threshold must be in (0, 1] and days 1-30")
        self._threshold, self._days, self._model = threshold, days, model
        self._session = requests.Session()

    @property
    def name(self) -> str:
        return "social_watch"

    def predict(self, ticker: str, date: str, data_client: DataClient) -> Signal:
        if not is_recent(date, 5):
            return self._abstain(ticker, date, "X can't be replayed for past dates, so Hype Watch only checks recent days", "not covered")
        key = os.environ.get(XAI_KEY_VAR, "").strip()
        if not key:
            return self._abstain(ticker, date, "add an xAI key (Keys & model) to turn Hype Watch on", "setup")
        try:
            verdict = self._check(ticker, date, key)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            return self._abstain(ticker, date, f"X search failed ({type(exc).__name__})", "source failure")
        risk = max(0.0, min(1.0, float(verdict.get("pump_risk", 0))))
        summary = str(verdict.get("summary") or "").strip()[:300]
        if risk < self._threshold:
            return self._abstain(ticker, date, f"no pump warning on X (risk {risk:.0%}). {summary}", "no warning")
        return Signal(
            model_name=self.name, ticker=ticker, date=date, value=-risk,
            reasoning=f"Pump warning on X (risk {risk:.0%}): {summary} Stay cautious: social-media hype is a reason to sell into, not to buy.",
            components={"pump_risk": risk},
        )

    def _check(self, ticker: str, date: str, key: str) -> dict:
        safe = re.sub(r"[^A-Z0-9.-]", "_", ticker.upper())
        path = CACHE_DIR / "social" / date / f"{safe}.json"
        if path.exists():
            return json.loads(path.read_text())
        start = days_before(date, self._days)
        body = {
            "model": self._model or default_grok(),
            "input": [{"role": "user", "content": PROMPT.format(ticker=ticker.upper(), start=start, end=date)}],
            "tools": [{"type": "x_search", "from_date": start, "to_date": date}],
        }
        resp = self._session.post(XAI_URL, json=body, timeout=180,
                                  headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        if resp.status_code != 200:
            raise ValueError(f"xAI answered HTTP {resp.status_code}")
        verdict = parse_verdict(response_text(resp.json()))
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(verdict))
        return verdict


def response_text(data: dict) -> str:
    """The assistant's text from a Responses API reply."""
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") in ("output_text", "text") and isinstance(content.get("text"), str):
                parts.append(content["text"])
    if not parts:
        raise ValueError("no text in xAI reply")
    return "\n".join(parts)


def parse_verdict(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON in reply")
    verdict = json.loads(match.group(0))
    if not isinstance(verdict, dict) or "pump_risk" not in verdict:
        raise ValueError("reply missing pump_risk")
    float(verdict["pump_risk"])
    return {"pump_risk": verdict["pump_risk"], "summary": verdict.get("summary", "")}
