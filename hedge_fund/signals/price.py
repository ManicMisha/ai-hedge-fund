"""Price-only alpha models: trend, momentum and low volatility.

No LLM and no fundamentals — each reads a ticker's daily closes up to the
assessment date and nothing after it. They cost nothing to run, work on the
free Yahoo source, and lean on three of the best-documented patterns in
markets:

- trend      price above its long moving average (Faber's 10-month rule)
- momentum   the past year's return, skipping the latest month
             (Jegadeesh & Titman; Asness, Moskowitz & Pedersen)
- low_vol    calmer stocks have earned about the same as wild ones with
             smaller swings (Blitz & van Vliet)

None of these is a promise: published patterns weaken after publication
(McLean & Pontiff found about a third on average), and momentum has crashed
hard after market bottoms. They are views; sizing and limits stay in code.
"""

from __future__ import annotations

import math
from datetime import date as _date
from datetime import timedelta
from statistics import pstdev
from weakref import WeakKeyDictionary

from hedge_fund.data.protocol import DataClient
from hedge_fund.data.sessions import completed_through
from hedge_fund.models import Signal
from hedge_fund.signals.base import QuantModel

# Per data client: ticker -> (start, end, [(day, close, volume), ...]). One fetch per
# ticker covers every later assessment date in a backtest; the slice below
# keeps each assessment point-in-time.
_HISTORY: WeakKeyDictionary = WeakKeyDictionary()
_TRADING_TO_CALENDAR = 1.5  # calendar days per trading day, with holiday slack


def closes_through(data_client: DataClient, ticker: str, as_of: str, trading_days: int) -> list[float]:
    """Up to *trading_days* + 1 daily closes ending on or before *as_of*, oldest first."""
    return [close for close, _ in bars_through(data_client, ticker, as_of, trading_days)]


def bars_through(data_client: DataClient, ticker: str, as_of: str, trading_days: int) -> list[tuple[float, int]]:
    """Up to *trading_days* + 1 daily (close, volume) pairs ending on or before *as_of*."""
    start = (_date.fromisoformat(as_of) - timedelta(days=int(trading_days * _TRADING_TO_CALENDAR) + 10)).isoformat()
    end = max(as_of, completed_through())
    try:
        per_client = _HISTORY.setdefault(data_client, {})
    except TypeError:  # a client that can't be weakly referenced: no sharing
        per_client = {}
    cached = per_client.get(ticker)
    if cached is None or cached[0] > start or cached[1] < as_of:
        bars = data_client.get_prices(ticker, start, end)
        series = sorted((b.time[:10], b.close, b.volume) for b in bars)
        cached = (start, end, series)
        per_client[ticker] = cached
    rows = [(close, volume) for day, close, volume in cached[2] if day <= as_of]
    return rows[-(trading_days + 1):]


class _PriceModel(QuantModel):
    """Shared plumbing: fetch closes, abstain honestly when there are too few."""

    _lookback: int

    def _closes(self, ticker: str, date: str, data_client: DataClient) -> list[float] | None:
        closes = closes_through(data_client, ticker, date, self._lookback)
        if len(closes) < self._lookback + 1 or any(not math.isfinite(c) or c <= 0 for c in closes):
            return None
        return closes

    def _abstain(self, ticker: str, date: str, have: int) -> Signal:
        return Signal(
            model_name=self.name, ticker=ticker, date=date, value=0.0,
            reasoning=f"abstained: needs {self._lookback + 1} daily closes, has {have}",
            metadata={"abstained": True, "abstain_reason": "insufficient price history"},
        )


class TrendModel(_PriceModel):
    """Own it while it trades above its long moving average; otherwise stand aside."""

    investment_approach = "long_only"

    def __init__(self, *, ma_days: int = 200) -> None:
        if not 20 <= ma_days <= 400:
            raise ValueError("ma_days must be between 20 and 400")
        self._lookback = ma_days

    @property
    def name(self) -> str:
        return "trend"

    def predict(self, ticker: str, date: str, data_client: DataClient) -> Signal:
        closes = self._closes(ticker, date, data_client)
        if closes is None:
            return self._abstain(ticker, date, len(closes_through(data_client, ticker, date, self._lookback)))
        last = closes[-1]
        average = sum(closes[-self._lookback:]) / self._lookback
        gap = last / average - 1
        above = gap > 0
        return Signal(
            model_name=self.name, ticker=ticker, date=date, value=1.0 if above else 0.0,
            reasoning=(f"Price {last:,.2f} is {abs(gap):.1%} {'above' if above else 'below'} its "
                       f"{self._lookback}-day average of {average:,.2f}: "
                       f"{'uptrend, hold' if above else 'downtrend, stand aside'}."),
            components={"gap": gap},
        )


class MomentumModel(_PriceModel):
    """The past year's return, skipping the most recent month (12-1 momentum)."""

    investment_approach = "long_short"

    def __init__(self, *, lookback_days: int = 252, skip_days: int = 21, scale: float = 0.25) -> None:
        if not 0 <= skip_days < lookback_days or not 60 <= lookback_days <= 400 or scale <= 0:
            raise ValueError("momentum needs 0 <= skip_days < lookback_days (60-400) and scale > 0")
        self._lookback, self._skip, self._scale = lookback_days, skip_days, scale

    @property
    def name(self) -> str:
        return "momentum"

    def predict(self, ticker: str, date: str, data_client: DataClient) -> Signal:
        closes = self._closes(ticker, date, data_client)
        if closes is None:
            return self._abstain(ticker, date, len(closes_through(data_client, ticker, date, self._lookback)))
        past = closes[-1 - self._skip] / closes[0] - 1
        value = math.tanh(past / self._scale)
        months = round(self._lookback / 21)
        return Signal(
            model_name=self.name, ticker=ticker, date=date, value=value,
            reasoning=(f"{'Up' if past >= 0 else 'Down'} {abs(past):.0%} over the past {months} months, "
                       f"leaving out the latest month: {'strong' if value > 0.5 else 'mild' if value > 0 else 'negative'} momentum."),
            components={"return": past},
        )


class LowVolatilityModel(_PriceModel):
    """Prefers calmer stocks: full conviction at or below the target volatility."""

    investment_approach = "long_only"

    def __init__(self, *, window_days: int = 252, target_vol: float = 0.20) -> None:
        if not 60 <= window_days <= 400 or not 0 < target_vol < 2:
            raise ValueError("window_days must be 60-400 and target_vol between 0 and 2")
        self._lookback, self._target = window_days, target_vol

    @property
    def name(self) -> str:
        return "low_vol"

    def predict(self, ticker: str, date: str, data_client: DataClient) -> Signal:
        closes = self._closes(ticker, date, data_client)
        if closes is None:
            return self._abstain(ticker, date, len(closes_through(data_client, ticker, date, self._lookback)))
        returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        vol = pstdev(returns) * math.sqrt(252)
        value = 1.0 if vol <= self._target else self._target / vol
        return Signal(
            model_name=self.name, ticker=ticker, date=date, value=value,
            reasoning=(f"Swings about {vol:.0%} a year against a {self._target:.0%} target: "
                       f"{'calm, full weight' if value >= 1 else 'jumpy, smaller weight'}."),
            components={"volatility": vol},
        )
