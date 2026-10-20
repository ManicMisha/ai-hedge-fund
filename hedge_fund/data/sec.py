"""SEC EDGAR: insider trades (Form 4) and fund holdings (13F), free.

EDGAR asks every program to identify itself with a name and contact email
in the User-Agent header and to stay under 10 requests a second. Set
SEC_USER_AGENT (e.g. "Jane Doe jane@example.com") in ~/.hedge-fund/.env or
from the dashboard; without it this client refuses to start rather than
send anonymous traffic.

Filings never change once accepted, so each document is cached on disk
forever (~/.hedge-fund/cache/sec/). Company filing lists are refreshed at
most once a day; the ticker map once a week.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

from hedge_fund.paths import CACHE_DIR, write_atomic

USER_AGENT_VAR = "SEC_USER_AGENT"
DEFAULT_CACHE_DIR = CACHE_DIR / "sec"
_MIN_INTERVAL = 0.12          # seconds between requests: under the 10/s limit
_TICKER_MAP_MAX_AGE_DAYS = 7
_RETRY_DELAYS = (1, 3)


class SecError(Exception):
    """EDGAR could not be reached or answered with something unusable."""


class SecSetupError(SecError):
    """No contact details for the User-Agent EDGAR requires."""


@dataclass(frozen=True)
class Filing:
    form: str
    filed: str          # YYYY-MM-DD, the day EDGAR accepted it
    accession: str      # 0001234567-26-000123
    document: str       # primary document file name (may include an xsl folder)


@dataclass(frozen=True)
class InsiderTransaction:
    owner: str
    title: str          # officer title, "Director", "10% owner", or ""
    is_officer: bool
    is_director: bool
    code: str           # P = open-market purchase, S = sale, A = award, ...
    acquired: bool
    shares: float
    price: float
    date: str


@dataclass(frozen=True)
class Holding:
    issuer: str
    title_of_class: str
    cusip: str
    shares: float
    value: float        # market value as reported (dollars since 2023)
    put_call: str       # "" for the shares themselves


class SecClient:
    _lock = threading.Lock()
    _last_request = 0.0

    def __init__(self, user_agent: str | None = None, cache_dir: Path | str | None = DEFAULT_CACHE_DIR,
                 session: requests.Session | None = None, timeout: float = 30.0) -> None:
        agent = (user_agent or os.environ.get(USER_AGENT_VAR, "")).strip()
        if "@" not in agent:
            raise SecSetupError("Add your name and email for SEC requests (Keys & model, or SEC_USER_AGENT)")
        self._cache = Path(cache_dir) if cache_dir is not None else None
        self._timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": agent, "Accept-Encoding": "gzip, deflate"})
        self._memo: dict[str, object] = {}

    # ---- identity -----------------------------------------------------------

    def ticker_map(self) -> dict[str, tuple[str, str]]:
        """TICKER -> (10-digit CIK, company name), from the SEC's own list."""
        if "tickers" in self._memo:
            return self._memo["tickers"]  # type: ignore[return-value]
        path = self._path("company_tickers.json")
        raw = None
        if path is not None and path.exists():
            age = (time.time() - path.stat().st_mtime) / 86400
            if age < _TICKER_MAP_MAX_AGE_DAYS:
                raw = path.read_text()
        if raw is None:
            raw = self._get("https://www.sec.gov/files/company_tickers.json")
            self._save(path, raw)
        rows = json.loads(raw).values()
        mapping = {str(r["ticker"]).upper(): (f"{int(r['cik_str']):010d}", str(r["title"])) for r in rows}
        self._memo["tickers"] = mapping
        return mapping

    def company(self, ticker: str) -> tuple[str, str] | None:
        """(CIK, name) for a ticker, or None for anything EDGAR doesn't list (most ETFs)."""
        key = ticker.upper().replace(".", "-")
        found = self.ticker_map().get(key) or self.ticker_map().get(ticker.upper())
        return found

    # ---- filings ------------------------------------------------------------

    def filings(self, cik: str, forms: tuple[str, ...]) -> tuple[list[Filing], str | None]:
        """The company's recent filings of *forms*, newest first, plus the date
        the list is complete from: None when it holds the whole history, else
        the oldest date in EDGAR's recent page (about the last 1,000 filings).
        Callers use it to tell "nothing filed" from "not covered"."""
        data = self._submissions(cik)
        recent = data.get("filings", {}).get("recent", {})
        out = [
            Filing(form=f, filed=d, accession=a, document=p)
            for f, d, a, p in zip(recent.get("form", []), recent.get("filingDate", []),
                                  recent.get("accessionNumber", []), recent.get("primaryDocument", []))
            if f in forms
        ]
        dates = recent.get("filingDate", [])
        older_pages = data.get("filings", {}).get("files") or []
        complete_from = min(dates) if dates and older_pages else None
        return sorted(out, key=lambda x: x.filed, reverse=True), complete_from

    def _submissions(self, cik: str) -> dict:
        key = f"sub-{cik}"
        if key in self._memo:
            return self._memo[key]  # type: ignore[return-value]
        today = datetime.now(timezone.utc).date().isoformat()
        path = self._path(f"submissions/CIK{cik}.json")
        if path is not None and path.exists():
            try:
                cached = json.loads(path.read_text())
                if cached.get("_fetched") == today:
                    self._memo[key] = cached
                    return cached
            except ValueError:
                pass
        data = json.loads(self._get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
        data["_fetched"] = today
        self._save(path, json.dumps(data))
        self._memo[key] = data
        return data

    def document(self, cik: str, accession: str, name: str) -> str:
        """A file inside a filing. Accepted filings never change: cached forever."""
        name = name.rsplit("/", 1)[-1]  # "xslF345X05/form4.xml" is a styled view of "form4.xml"
        folder = accession.replace("-", "")
        path = self._path(f"docs/{int(cik)}/{folder}/{name}")
        if path is not None and path.exists():
            return path.read_text()
        text = self._get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{folder}/{name}")
        self._save(path, text)
        return text

    def filing_files(self, cik: str, accession: str) -> list[str]:
        raw = self.document(cik, accession, "index.json")
        try:
            return [item["name"] for item in json.loads(raw)["directory"]["item"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise SecError(f"unreadable filing index for {accession}") from exc

    def holdings(self, cik: str, filing: Filing) -> list[Holding]:
        """The information table of a 13F-HR filing."""
        for name in self.filing_files(cik, filing.accession):
            if not name.lower().endswith(".xml") or name.lower() == "primary_doc.xml":
                continue
            text = self.document(cik, filing.accession, name)
            if "infoTable" in text:
                return parse_13f(text)
        return []

    # ---- plumbing -----------------------------------------------------------

    def _get(self, url: str) -> str:
        for attempt in range(len(_RETRY_DELAYS) + 1):
            with SecClient._lock:
                wait = SecClient._last_request + _MIN_INTERVAL - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                SecClient._last_request = time.monotonic()
            try:
                resp = self._session.get(url, timeout=self._timeout)
            except requests.RequestException as exc:
                raise SecError(f"cannot reach SEC EDGAR: {exc}") from exc
            if resp.status_code in (429, 503) and attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            if resp.status_code != 200:
                raise SecError(f"SEC EDGAR answered HTTP {resp.status_code} for {url}")
            return resp.text
        raise SecError("SEC EDGAR kept refusing requests")  # pragma: no cover

    def _path(self, relative: str) -> Path | None:
        return self._cache / relative if self._cache is not None else None

    @staticmethod
    def _save(path: Path | None, text: str) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, text)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _strip_namespaces(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _text(el: ET.Element | None, path: str) -> str:
    found = el.find(path) if el is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


def _number(text: str) -> float:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return 0.0


def parse_form4(xml: str) -> list[InsiderTransaction]:
    """Non-derivative transactions from a Form 4 ownership document."""
    try:
        root = _strip_namespaces(ET.fromstring(xml.strip()))
    except ET.ParseError as exc:
        raise SecError("unreadable Form 4") from exc
    owners = root.findall("reportingOwner")
    owner = _text(owners[0], "reportingOwnerId/rptOwnerName") if owners else ""
    rel = owners[0].find("reportingOwnerRelationship") if owners else None
    is_director = _text(rel, "isDirector") in ("1", "true")
    is_officer = _text(rel, "isOfficer") in ("1", "true")
    title = _text(rel, "officerTitle") or ("Director" if is_director else
                                           "10% owner" if _text(rel, "isTenPercentOwner") in ("1", "true") else "")
    out = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        out.append(InsiderTransaction(
            owner=owner, title=title, is_officer=is_officer, is_director=is_director,
            code=_text(tx, "transactionCoding/transactionCode"),
            acquired=_text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value") == "A",
            shares=_number(_text(tx, "transactionAmounts/transactionShares/value")),
            price=_number(_text(tx, "transactionAmounts/transactionPricePerShare/value")),
            date=_text(tx, "transactionDate/value")[:10],
        ))
    return out


def parse_13f(xml: str) -> list[Holding]:
    """Rows of a 13F information table."""
    try:
        root = _strip_namespaces(ET.fromstring(xml.strip()))
    except ET.ParseError as exc:
        raise SecError("unreadable 13F information table") from exc
    return [
        Holding(
            issuer=_text(row, "nameOfIssuer"), title_of_class=_text(row, "titleOfClass"),
            cusip=_text(row, "cusip"), shares=_number(_text(row, "shrsOrPrnAmt/sshPrnamt")),
            value=_number(_text(row, "value")), put_call=_text(row, "putCall"),
        )
        for row in root.iter("infoTable")
    ]


# ---------------------------------------------------------------------------
# Matching a ticker's company to a 13F issuer name
# ---------------------------------------------------------------------------

_NOISE = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "LLC", "LP",
    "HOLDINGS", "HLDGS", "HLDG", "GROUP", "GRP", "THE", "NEW", "DEL", "DE", "CL", "CLASS", "COM", "SA", "NV",
    "AG", "SE", "OF", "AND", "A", "B", "C", "SHS", "ORD", "CAP", "STK",
}


def normalize_name(name: str) -> tuple[str, ...]:
    text = name.upper().replace("&", " AND ").replace("'", "").replace("\u2019", "")
    words = re.sub(r"[^A-Z0-9 ]", " ", text).split()
    return tuple(w for w in words if w not in _NOISE)


def _abbreviates(short: str, full: str) -> bool:
    """"PETE" for "PETROLEUM", "AMER" for "AMERICA": same first letter, and
    the short form's letters appear in order in the long one."""
    if short == full:
        return True
    if len(short) < 3 or short[0] != full[0] or len(short) > len(full):
        return False
    it = iter(full)
    return all(ch in it for ch in short)


def same_company(a: str, b: str) -> bool:
    """Conservative match between a company name and a 13F issuer name: the
    same words in any order, or word-for-word abbreviations, or near-identical
    spelling. "COCA COLA CO" never matches "COCA COLA CONSOLIDATED"."""
    x, y = normalize_name(a), normalize_name(b)
    if not x or not y:
        return False
    if sorted(x) == sorted(y):
        return True
    if len(x) == len(y) and all(_abbreviates(p, q) or _abbreviates(q, p) for p, q in zip(x, y)):
        return True
    return SequenceMatcher(None, " ".join(x), " ".join(y)).ratio() >= 0.92


def days_before(day: str, n: int) -> str:
    return date.fromordinal(date.fromisoformat(day).toordinal() - n).isoformat()
