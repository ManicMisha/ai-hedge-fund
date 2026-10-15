"""Free daily prices from Yahoo Finance — no key, prices only.

Yahoo's public chart endpoint gives daily OHLCV bars for US stocks, ETFs and
indexes. It has no point-in-time fundamentals, earnings or insider data, so
every other DataClient method returns empty: the honest answer for "this
source doesn't have it". LLM investor agents therefore abstain on Yahoo
data (no fundamentals to read), while price-based models (trend, momentum,
low volatility) and the paper broker's marks work fully.

One request fetches a ticker's whole daily history, which is then kept in
memory and on disk (~/.hedge-fund/cache/yahoo/) and sliced locally, so a
backtest asks Yahoo once per ticker per day, not once per bar.

Prices are raw closes (not dividend-adjusted), matching how fills are
priced. Dividends are therefore left out of returns for both the fund and
the benchmark.

This is an unofficial endpoint: Yahoo can rate-limit or change it without
notice. Failures raise YahooError — never a silent empty — so a run halts
instead of trading on missing data.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from hedge_fund.data.models import Price
from hedge_fund.data.sessions import completed_through
from hedge_fund.paths import CACHE_DIR, write_atomic

DEFAULT_CACHE_DIR = CACHE_DIR / "yahoo"
_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (aihf; +https://github.com/virattt/ai-hedge-fund)"}
_RETRY_DELAYS = (2, 6)


class YahooError(Exception):
    """Yahoo could not be reached or answered with something unusable."""


def yahoo_symbol(ticker: str) -> str:
    """Yahoo writes share classes with a dash: BRK.B -> BRK-B."""
    return ticker.strip().upper().replace(".", "-")


class YahooClient:
    """DataClient backed by Yahoo Finance daily prices."""

    def __init__(
        self,
        cache_dir: Path | str | None = DEFAULT_CACHE_DIR,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.update(_HEADERS)
        self._memo: dict[str, tuple[str, list[Price]]] = {}

    def __enter__(self) -> YahooClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    def get_prices(self, ticker, start_date, end_date, interval="day", interval_multiplier=1) -> list[Price]:
        if interval != "day" or interval_multiplier != 1:
            raise YahooError("the Yahoo source serves daily bars only")
        bars = self._history(ticker, min(end_date, completed_through()))
        return [b for b in bars if start_date <= b.time[:10] <= end_date]

    def _history(self, ticker: str, needed_through: str) -> list[Price]:
        """The ticker's full daily history, complete through *needed_through*."""
        key = yahoo_symbol(ticker)
        hit = self._memo.get(key) or self._read_disk(key)
        if hit is not None and hit[0] >= needed_through:
            self._memo[key] = hit
            return hit[1]
        through = completed_through()
        bars = [b for b in self._fetch(key) if b.time[:10] <= through]
        self._memo[key] = (through, bars)
        self._write_disk(key, through, bars)
        return bars

    def _fetch(self, symbol: str) -> list[Price]:
        url = _URL.format(symbol=symbol)
        params = {"range": "max", "interval": "1d", "includePrePost": "false", "events": "div,splits"}
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                resp = self._session.get(url, params=params, timeout=self._timeout)
            except requests.RequestException as exc:
                raise YahooError(f"{symbol}: cannot reach Yahoo Finance: {exc}") from exc
            if resp.status_code == 429 and attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            break
        if resp.status_code == 404:
            return []  # Yahoo has no such symbol: genuinely no data
        if resp.status_code != 200:
            raise YahooError(f"{symbol}: Yahoo Finance answered HTTP {resp.status_code}")
        try:
            payload = resp.json()["chart"]
        except (ValueError, KeyError, TypeError) as exc:
            raise YahooError(f"{symbol}: unexpected response from Yahoo Finance") from exc
        return parse_chart(symbol, payload)

    # ------------------------------------------------------------------
    # Disk cache: one file per ticker, with the date it is complete through
    # ------------------------------------------------------------------

    def _path(self, symbol: str) -> Path | None:
        return self._cache_dir / f"{symbol}.json" if self._cache_dir is not None else None

    def _read_disk(self, symbol: str) -> tuple[str, list[Price]] | None:
        path = self._path(symbol)
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return data["through"], [Price(**row) for row in data["bars"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None  # a damaged cache file is refetched, not trusted

    def _write_disk(self, symbol: str, through: str, bars: list[Price]) -> None:
        path = self._path(symbol)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps({"through": through, "bars": [b.model_dump() for b in bars]}))

    # ------------------------------------------------------------------
    # Everything else: Yahoo's free endpoint has no point-in-time data
    # ------------------------------------------------------------------

    def get_financial_metrics(self, ticker, end_date, period="ttm", limit=10):
        return []

    def get_news(self, ticker, end_date, start_date=None, limit=1000):
        return []

    def get_insider_trades(self, ticker, end_date, start_date=None, limit=1000):
        return []

    def get_company_facts(self, ticker):
        return None

    def get_earnings(self, ticker):
        return None

    def get_earnings_history(self, ticker, limit=12):
        return []

    def get_market_cap(self, ticker, end_date):
        return None


def parse_chart(symbol: str, chart: dict) -> list[Price]:
    """Turn Yahoo's chart payload into daily Price bars, one per exchange date."""
    if chart.get("error"):
        code = str((chart["error"] or {}).get("code", ""))
        if code.lower() in ("not found", "no data found"):
            return []
        raise YahooError(f"{symbol}: Yahoo Finance error: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        return []
    result = results[0]
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    zone = ZoneInfo((result.get("meta") or {}).get("exchangeTimezoneName") or "America/New_York")
    by_day: dict[str, Price] = {}
    for i, stamp in enumerate(stamps):
        close = _at(quote.get("close"), i)
        if close is None or close <= 0:
            continue  # halted or missing day: no bar, never a fake one
        day = datetime.fromtimestamp(stamp, zone).date().isoformat()
        by_day[day] = Price(
            open=_at(quote.get("open"), i) or close,
            high=_at(quote.get("high"), i) or close,
            low=_at(quote.get("low"), i) or close,
            close=close,
            volume=int(_at(quote.get("volume"), i) or 0),
            time=f"{day}T00:00:00Z",
        )
    return [by_day[d] for d in sorted(by_day)]


def _at(values, i):
    if not values or i >= len(values):
        return None
    value = values[i]
    return float(value) if value is not None else None
