"""Copy desk: trackers that watch what informed traders file, and follow.

Three trackers, each reading a public record on or before the assessment
date (by the day it was *filed*, never the day the trade happened, because
that is when anyone could first have known):

- insiders        open-market buys by a company's own officers and directors
                  (SEC Form 4, filed within 2 business days of the trade)
- superinvestors  what a short list of famous concentrated funds own and added
                  (SEC 13F, filed up to 45 days after each quarter)
- congress        buys disclosed by US senators and representatives
                  (Bargo's free feed: last 3 months only)

They only ever vote to buy, and abstain when there is nothing to copy, so a
quiet stock leaves its slot in cash instead of dragging the others down.
Sales are ignored on purpose: people sell for taxes, houses and divorces, and
the research finds insider *sales* tell you little; *buys* are the signal.

What the evidence says, honestly:
- Insider buys: about 0.4%/month above the market historically (Jeng,
  Metrick & Zeckhauser), strongest for clusters of insiders and for CEOs and
  CFOs. It is public, well known, and has weakened.
- 13F copying: the big funds' highest-conviction positions have beaten the
  market on average (Cohen, Polk & Silli, "Best Ideas"), but the data is up to
  4.5 months old by the time you see it.
- Congress: mixed. Early studies found senators beating the market; later
  ones found no edge after the 2012 STOCK Act. Weighted lightly by default.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from typing import ClassVar

import requests

from hedge_fund.data.protocol import DataClient
from hedge_fund.data.sec import (
    days_before, Filing, Holding, parse_form4, same_company, SecClient, SecError, SecSetupError, USER_AGENT_VAR,
)
from hedge_fund.models import Signal
from hedge_fund.paths import CACHE_DIR, write_atomic
from hedge_fund.signals.base import QuantModel

_C_SUITE = re.compile(r"\b(CEO|CFO|COO|CHIEF|PRESIDENT|CHAIR)", re.IGNORECASE)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class _Tracker(QuantModel):
    """Shared plumbing: abstain with a plain reason, never crash a run."""

    kind: ClassVar[str] = "tracker"
    required_env: ClassVar[tuple[str, ...]] = ()

    def _abstain(self, ticker: str, as_of: str, why: str, reason: str = "nothing to copy") -> Signal:
        return Signal(model_name=self.name, ticker=ticker, date=as_of, value=0.0, reasoning=f"abstained: {why}",
                      metadata={"abstained": True, "abstain_reason": reason})


class _SecTracker(_Tracker):
    required_env: ClassVar[tuple[str, ...]] = (USER_AGENT_VAR,)
    _sec: SecClient | None = None

    def sec(self) -> SecClient:
        if self._sec is None:
            self._sec = SecClient()
        return self._sec


# ---------------------------------------------------------------------------
# Insiders (Form 4)
# ---------------------------------------------------------------------------

class InsiderModel(_SecTracker):
    """Follows open-market purchases by a company's officers and directors."""

    investment_approach = "long_only"

    def __init__(self, *, window_days: int = 90, min_value: float = 10_000) -> None:
        if not 7 <= window_days <= 365 or min_value < 0:
            raise ValueError("window_days must be 7-365 and min_value non-negative")
        self._window, self._min_value = window_days, min_value

    @property
    def name(self) -> str:
        return "insiders"

    def predict(self, ticker: str, date: str, data_client: DataClient) -> Signal:
        start = days_before(date, self._window)
        try:
            sec = self.sec()
            company = sec.company(ticker)
            if company is None:
                return self._abstain(ticker, date, "the SEC lists no company for this ticker (funds and ETFs have no insiders)")
            cik, _ = company
            filings, complete_from = sec.filings(cik, ("4",))
            if complete_from is not None and start < complete_from:
                return self._abstain(ticker, date, f"SEC's recent filing list only reaches back to {complete_from}", "not covered")
            buys: dict[str, dict] = {}
            sellers: set[str] = set()
            for filing in filings:
                if not start <= filing.filed <= date:
                    continue
                for tx in parse_form4(sec.document(cik, filing.accession, filing.document)):
                    if tx.code == "S" and not tx.acquired:
                        sellers.add(tx.owner)
                    if tx.code != "P" or not tx.acquired:
                        continue
                    if not (tx.is_officer or tx.is_director):
                        continue  # 10% holders are usually funds, not people with inside knowledge
                    row = buys.setdefault(tx.owner, {"title": tx.title, "value": 0.0, "filed": filing.filed})
                    row["value"] += tx.shares * tx.price
                    row["filed"] = max(row["filed"], filing.filed)
        except SecSetupError as exc:
            return self._abstain(ticker, date, str(exc), "setup")
        except SecError as exc:
            return self._abstain(ticker, date, f"SEC unavailable ({exc})", "source failure")

        buyers = {name: row for name, row in buys.items() if row["value"] >= self._min_value}
        if not buyers:
            note = f"; {len(sellers)} insider(s) sold, which is usually not a signal" if sellers else ""
            return self._abstain(ticker, date, f"no insider open-market buys in the last {self._window} days{note}")

        c_suite = [n for n, r in buyers.items() if _C_SUITE.search(r["title"] or "")]
        total = sum(r["value"] for r in buyers.values())
        value = 0.3 * len(buyers) + 0.2 * len(c_suite) + (0.2 if len(buyers) >= 3 else 0.0) + (0.1 if total >= 1_000_000 else 0.0)
        value = min(1.0, value)
        top = sorted(buyers.items(), key=lambda kv: kv[1]["value"], reverse=True)[:3]
        who = ", ".join(f"{n.title()} ({r['title'] or 'insider'}, ${r['value']:,.0f}, filed {r['filed']})" for n, r in top)
        cluster = " A cluster buy, the strongest kind." if len(buyers) >= 3 else ""
        return Signal(
            model_name=self.name, ticker=ticker, date=date, value=value,
            reasoning=f"{len(buyers)} insider(s) bought ${total:,.0f} on the open market in {self._window} days: {who}.{cluster}",
            components={"buyers": float(len(buyers)), "c_suite": float(len(c_suite)), "dollars": total},
        )


# ---------------------------------------------------------------------------
# Superinvestors (13F)
# ---------------------------------------------------------------------------

DEFAULT_FUNDS = {
    "Berkshire Hathaway": "0001067983",
    "Pershing Square": "0001336528",
    "Appaloosa": "0001656456",
    "Baupost": "0001061768",
    "Duquesne Family Office": "0001536411",
}


class SuperinvestorModel(_SecTracker):
    """Follows the stocks a few famous concentrated funds own and add to."""

    investment_approach = "long_only"

    def __init__(self, *, funds: dict[str, str] | None = None, max_age_days: int = 140,
                 big_position: float = 0.05) -> None:
        funds = DEFAULT_FUNDS if funds is None else funds
        if not funds or not 50 <= max_age_days <= 400 or not 0 < big_position < 1:
            raise ValueError("superinvestors needs at least one fund, max_age_days 50-400, big_position in (0, 1)")
        self._funds = {name: f"{int(cik):010d}" for name, cik in funds.items()}
        self._max_age, self._big = max_age_days, big_position
        self._tables: dict[str, list[Holding]] = {}

    @property
    def name(self) -> str:
        return "superinvestors"

    def _table(self, cik: str, filing: Filing) -> list[Holding]:
        if filing.accession not in self._tables:
            self._tables[filing.accession] = [h for h in self.sec().holdings(cik, filing) if not h.put_call]
        return self._tables[filing.accession]

    def predict(self, ticker: str, date: str, data_client: DataClient) -> Signal:
        try:
            company = self.sec().company(ticker)
        except SecSetupError as exc:
            return self._abstain(ticker, date, str(exc), "setup")
        except SecError as exc:
            return self._abstain(ticker, date, f"SEC unavailable ({exc})", "source failure")
        if company is None:
            return self._abstain(ticker, date, "the SEC lists no company for this ticker")
        _, company_name = company

        votes: list[tuple[str, float, str]] = []
        failures = 0
        for fund, cik in self._funds.items():
            try:
                filings, _ = self.sec().filings(cik, ("13F-HR",))
                known = [f for f in filings if f.filed <= date]
                if not known or known[0].filed < days_before(date, self._max_age):
                    continue
                latest = self._table(cik, known[0])
                previous = self._table(cik, known[1]) if len(known) > 1 else None
            except SecError:
                failures += 1
                continue
            shares, value = _position(latest, company_name)
            if shares <= 0:
                if previous is not None and _position(previous, company_name)[0] > 0:
                    votes.append((fund, 0.0, f"{fund} sold out (filed {known[0].filed})"))
                continue
            total = sum(h.value for h in latest) or 1.0
            weight = value / total
            score, notes = 0.5, [f"{weight:.0%} of its portfolio"]
            if weight >= self._big:
                score += 0.3
                notes.append("a top holding")
            if previous is not None:
                before = _position(previous, company_name)[0]
                if before <= 0:
                    score += 0.2
                    notes.append("new this quarter")
                elif shares >= before * 1.10:
                    score += 0.2
                    notes.append(f"added {shares / before - 1:.0%}")
                elif shares <= before * 0.75:
                    score -= 0.3
                    notes.append(f"cut {1 - shares / before:.0%}")
            votes.append((fund, max(0.0, min(1.0, score)), f"{fund}: {', '.join(notes)} (filed {known[0].filed})"))

        if not votes:
            if failures == len(self._funds):
                return self._abstain(ticker, date, "SEC unavailable for every tracked fund", "source failure")
            return self._abstain(ticker, date, f"none of the {len(self._funds)} tracked funds own it")
        holders = [v for v in votes if v[1] > 0]
        best = max(v[1] for v in votes)
        value = min(1.0, best + 0.1 * max(0, len(holders) - 1))
        return Signal(
            model_name=self.name, ticker=ticker, date=date, value=value,
            reasoning="; ".join(v[2] for v in votes) + ". 13F filings are up to 4.5 months old.",
            components={"holders": float(len(holders))},
        )


def _position(table: list[Holding], company_name: str) -> tuple[float, float]:
    """Shares and value across every share class of the company."""
    rows = [h for h in table if same_company(h.issuer, company_name)]
    return sum(h.shares for h in rows), sum(h.value for h in rows)


# ---------------------------------------------------------------------------
# Congress (Bargo)
# ---------------------------------------------------------------------------

BARGO_URL = "https://www.bargo.ai/free-apis/congress/v1"
BARGO_KEY_VAR = "BARGO_API_KEY"
BARGO_CREDIT = "Congress data: Bargo (https://www.bargo.ai)"
_COVERAGE_DAYS = 90


class CongressModel(_Tracker):
    """Follows stock purchases disclosed by members of Congress."""

    investment_approach = "long_only"

    def __init__(self, *, window_days: int = 45) -> None:
        if not 7 <= window_days <= 60:
            raise ValueError("window_days must be 7-60 (the free feed only covers about 3 months)")
        self._window = window_days
        self._memo: dict[str, list[dict]] = {}
        self._session = requests.Session()

    @property
    def name(self) -> str:
        return "congress"

    def trades(self, ticker: str) -> list[dict]:
        """Today's copy of the ticker's recent disclosures (one request per ticker per day)."""
        today = _today()
        if ticker in self._memo:
            return self._memo[ticker]
        path = CACHE_DIR / "congress" / today / f"{re.sub(r'[^A-Z0-9.-]', '_', ticker.upper())}.json"
        if path.exists():
            rows = json.loads(path.read_text())
        else:
            headers = {"Accept": "application/json"}
            key = os.environ.get(BARGO_KEY_VAR, "").strip()
            if key:
                headers["Authorization"] = f"Bearer {key}"
            try:
                resp = self._session.get(f"{BARGO_URL}/trades/{ticker.upper()}", params={"limit": 100},
                                         headers=headers, timeout=20)
            except requests.RequestException as exc:
                raise SecError(f"cannot reach Bargo: {exc}") from exc
            if resp.status_code == 404:
                rows = []
            elif resp.status_code == 429:
                raise SecError("Bargo's free daily limit is used up (a free key raises it)")
            elif resp.status_code != 200:
                raise SecError(f"Bargo answered HTTP {resp.status_code}")
            else:
                rows = resp.json().get("trades") or []
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, json.dumps(rows))
        self._memo[ticker] = rows
        return rows

    def predict(self, ticker: str, date: str, data_client: DataClient) -> Signal:
        start = days_before(date, self._window)
        if start < days_before(_today(), _COVERAGE_DAYS):
            return self._abstain(ticker, date, "the free congress feed only covers the last 3 months", "not covered")
        try:
            rows = self.trades(ticker)
        except SecError as exc:
            return self._abstain(ticker, date, str(exc), "source failure")
        buys: dict[str, dict] = {}
        for row in rows:
            disclosed = str(row.get("disclosure_date") or "")[:10]
            if not disclosed or not start <= disclosed <= date:
                continue
            if not str(row.get("type") or "").lower().startswith("purchase"):
                continue
            member = str(row.get("member") or "unknown member")
            entry = buys.setdefault(member, {"top": 0.0, "chamber": row.get("chamber") or "", "disclosed": disclosed})
            entry["top"] = max(entry["top"], _amount_top(str(row.get("amount_range") or "")))
            entry["disclosed"] = max(entry["disclosed"], disclosed)
        if not buys:
            return self._abstain(ticker, date, f"no purchases disclosed by Congress in {self._window} days. {BARGO_CREDIT}")
        big = sum(1 for b in buys.values() if b["top"] > 50_000)
        value = min(1.0, 0.3 * len(buys) + 0.2 * big)
        who = ", ".join(f"{m} ({b['chamber']}, up to ${b['top']:,.0f}, disclosed {b['disclosed']})"
                        for m, b in sorted(buys.items(), key=lambda kv: kv[1]["top"], reverse=True)[:3])
        return Signal(
            model_name=self.name, ticker=ticker, date=date, value=value,
            reasoning=f"{len(buys)} member(s) of Congress disclosed buying: {who}. Disclosures can lag trades by 45 days. {BARGO_CREDIT}",
            components={"members": float(len(buys))},
        )


def _amount_top(text: str) -> float:
    """Upper end of a disclosure band like "$1,001 - $15,000"."""
    numbers = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text)]
    return max(numbers) if numbers else 0.0


def is_recent(day: str, days: int) -> bool:
    return date.fromisoformat(day) >= date.fromisoformat(days_before(_today(), days))


__all__ = ["InsiderModel", "SuperinvestorModel", "CongressModel", "DEFAULT_FUNDS", "BARGO_CREDIT", "is_recent"]
