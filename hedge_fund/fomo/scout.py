"""Social Scout: scrolls social media and the web for everything about a token.

One Grok call with xAI's two built-in search tools: `x_search` (X/Twitter
posts) and `web_search` (the open web: news, Reddit, crypto forums, token
sites, and Instagram/TikTok/YouTube posts where they're public on the web).
Instagram and Snapchat can't be searched directly by any program: they have
no public search API and scraping them breaks their rules, so the Scout
only sees what they publish to the open web.

The Scout reports facts and flags, not orders: sentiment, pump risk, red
flags (anonymous team, honeypot reports, influencer dumps, copy-paste
shilling), green flags (real community, listings, working product), and
which places it actually checked. Reports are cached so a token isn't
searched twice in a row; a review of a held position asks for a fresh one.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

XAI_URL = "https://api.x.ai/v1/responses"

PROMPT = """You are the Social Scout on a crypto trading team. Search X and the web (news, Reddit, crypto forums,
the token's own site, public Instagram/TikTok/YouTube posts) for the Solana token {symbol} (contract {mint})
over the last {days} days. {purpose}

Report what you actually found. Do not guess. If you found little, say so.
Reply with JSON only:
{{"sentiment": <-1 very negative .. 1 very positive>,
  "pump_risk": <0..1, how much it looks like a coordinated pump-and-dump>,
  "buzz": <0..1, how much people are talking about it>,
  "red_flags": [<short phrases>], "green_flags": [<short phrases>],
  "sources_checked": [<e.g. "X", "news", "Reddit", "token website">],
  "summary": "<two plain sentences>"}}"""

ENTRY = "A top trader just bought it. Should the team follow? Look for anything that says this is a trap."
REVIEW = ("The team holds it and it has risen a lot. Is the excitement still building, or are people taking profits, "
          "is the team or an influencer selling, is the buzz fading?")


def _clip(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def clean_report(raw: dict) -> dict:
    flags = lambda k: [str(x)[:80] for x in (raw.get(k) or []) if str(x).strip()][:6]
    return {
        "sentiment": _clip(raw.get("sentiment"), -1, 1, 0.0),
        "pump_risk": _clip(raw.get("pump_risk"), 0, 1, 0.0),
        "buzz": _clip(raw.get("buzz"), 0, 1, 0.0),
        "red_flags": flags("red_flags"), "green_flags": flags("green_flags"),
        "sources_checked": flags("sources_checked") or ["X"],
        "summary": str(raw.get("summary") or "").strip()[:400],
    }


def _text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts = [c.get("text", "") for item in data.get("output") or [] if item.get("type") == "message"
             for c in item.get("content") or [] if c.get("type") in ("output_text", "text")]
    if not parts:
        raise ValueError("no text in the reply")
    return "\n".join(parts)


class SocialScout:
    def __init__(self, cache_dir: Path, *, key: str | None = None, model: str | None = None, session=None,
                 clock=time.time) -> None:
        self.cache_dir = Path(cache_dir)
        self._key = key
        self.model = model
        self.session = session or requests.Session()
        self.clock = clock

    @property
    def key(self) -> str:
        return (self._key or os.environ.get("XAI_API_KEY", "")).strip()  # read live: a key added later just works

    @property
    def ready(self) -> bool:
        return bool(self.key)

    def report(self, symbol: str, mint: str, purpose: str = "entry", max_age_s: float = 2 * 3600) -> dict | None:
        """The Scout's report, from cache if fresh enough. None without an xAI key."""
        if not self.key:
            return None
        path = self.cache_dir / f"{re.sub(r'[^A-Za-z0-9]', '_', mint)[:60]}-{purpose}.json"
        try:
            cached = json.loads(path.read_text())
            if self.clock() - cached["at"] < max_age_s:
                return {**cached["report"], "cached": True}
        except (OSError, ValueError, KeyError):
            pass
        from hedge_fund.signals.social import default_grok
        days = 2 if purpose == "entry" else 3
        today = datetime.now(timezone.utc).date()
        resp = self.session.post(XAI_URL, timeout=150, headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                                 json={"model": self.model or default_grok(),
                                       "tools": [{"type": "x_search", "from_date": (today - timedelta(days=days)).isoformat()},
                                                 {"type": "web_search"}],
                                       "input": [{"role": "user", "content": PROMPT.format(
                                           symbol=symbol, mint=mint, days=days, purpose=ENTRY if purpose == "entry" else REVIEW)}]})
        if resp.status_code != 200:
            raise RuntimeError(f"xAI answered HTTP {resp.status_code}")
        match = re.search(r"\{.*\}", _text(resp.json()), re.DOTALL)
        if not match:
            raise ValueError("the Scout's reply had no JSON")
        report = clean_report(json.loads(match.group(0)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": self.clock(), "report": report}))
        return report


def describe(report: dict) -> str:
    """One line for the case thread."""
    mood = "positive" if report["sentiment"] > 0.25 else "negative" if report["sentiment"] < -0.25 else "mixed"
    bits = [f"Checked {', '.join(report['sources_checked'])}. Mood {mood}, buzz {report['buzz']:.0%}, pump risk {report['pump_risk']:.0%}."]
    if report["red_flags"]:
        bits.append("Red flags: " + "; ".join(report["red_flags"][:3]) + ".")
    if report["green_flags"]:
        bits.append("Good signs: " + "; ".join(report["green_flags"][:3]) + ".")
    if report["summary"]:
        bits.append(report["summary"])
    return " ".join(bits)
