"""The pump detector and the pump guard in the risk stage."""

import json
from datetime import date, timedelta

import pytest

from hedge_fund.data.models import Price
from hedge_fund.fund import Fund, FundSpec
from hedge_fund.pipeline.stages import assess_fund
from hedge_fund.risk.limits import PumpGuard, RiskLimits
from hedge_fund.risk.pump import detect


def weekdays(start, n):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = weekdays("2024-01-01", 300)
RULE = PumpGuard()


def flat(n=80, price=10.0, volume=1_000):
    return [(price, volume)] * n


def test_spike_on_heavy_volume_is_flagged():
    bars = flat() + [(13.0, 6_000)]  # +30% in a day on 6x volume
    spike = detect(bars, RULE)
    assert spike is not None and spike.sessions_ago == 0 and spike.jump == pytest.approx(0.3)
    assert spike.volume_ratio == pytest.approx(6.0)


def test_spike_on_normal_volume_is_not_a_pump():
    assert detect(flat() + [(13.0, 1_200)], RULE) is None


def test_steady_climb_is_not_a_pump():
    bars = [(10 * 1.01 ** i, 1_000) for i in range(80)]  # +1% a day, normal volume
    assert detect(bars, RULE) is None


def test_five_day_run_up_counts_and_the_flag_expires():
    run = flat() + [(11.0, 5_000), (12.0, 5_000), (13.0, 5_000), (14.5, 5_000), (15.0, 5_000)]
    assert detect(run, RULE) is not None  # +50% in five sessions on 5x volume
    later = run + [(15.0, 1_000)] * 10
    assert detect(later, RULE) is not None and detect(later, RULE).sessions_ago == 10
    assert detect(later + [(15.0, 1_000)], RULE) is None  # 11 sessions on: cooled down


def test_without_volume_data_price_alone_decides():
    assert detect([(10.0, 0)] * 80 + [(13.0, 0)], RULE) is not None


# ---------------------------------------------------------------------------
# in the risk stage
# ---------------------------------------------------------------------------

class Prices:
    def __init__(self, series):
        self.series = series

    def get_prices(self, ticker, start_date, end_date, *args, **kwargs):
        return [Price(open=c, close=c, high=c, low=c, volume=v, time=f"{d}T00:00:00Z")
                for d, (c, v) in sorted(self.series[ticker].items()) if start_date <= d <= end_date]

    def __getattr__(self, name):
        return lambda *a, **k: [] if name != "get_market_cap" else None


def series(pump_at=None):
    out = {}
    for i, d in enumerate(DAYS):
        price, volume = 100 * 1.001 ** i, 1_000
        if pump_at is not None and i >= pump_at:
            price, volume = price * 1.6, (8_000 if i == pump_at else 1_500)
        out[d] = (price, volume)
    return out


def spec(guard=True):
    risk = {"max_position_pct": 1.0, "max_gross_exposure": 1.0}
    if guard:
        risk["pump_guard"] = {}
    return FundSpec(schema_version=2, name="g", benchmark="SPY", risk=risk,
                    strategies=[{"name": "s", "models": [{"name": "trend"}], "blend": {"mode": "long_only"}}])


def test_guard_stands_aside_from_the_pumped_ticker_only():
    data = Prices({"SPY": series(), "CALM": series(), "PUMP": series(pump_at=280)})
    decision = assess_fund(Fund(spec()), DAYS[282], data, ["CALM", "PUMP"])
    assert decision.final_weights["PUMP"] == 0.0 and decision.final_weights["CALM"] > 0
    [clamp] = decision.clamps
    assert clamp.limit == "pump_guard" and clamp.ticker == "PUMP" and clamp.after == 0.0
    contrib = decision.strategies[0].final_contribution
    assert contrib["PUMP"] == 0.0  # the record stays consistent with the book
    unguarded = assess_fund(Fund(spec(guard=False)), DAYS[282], data, ["CALM", "PUMP"])
    assert unguarded.final_weights["PUMP"] > 0
    cooled = assess_fund(Fund(spec()), DAYS[295], data, ["CALM", "PUMP"])
    assert cooled.final_weights["PUMP"] > 0 and cooled.clamps == []


def test_guard_is_off_the_wire_when_unset_and_refuses_hedged_books():
    limits = RiskLimits(max_position_pct=0.25, max_gross_exposure=1.0)
    assert json.loads(limits.model_dump_json()) == {"max_position_pct": 0.25, "max_gross_exposure": 1.0}
    with pytest.raises(ValueError, match="pump_guard"):
        FundSpec(schema_version=2, name="x", risk={"max_position_pct": 1, "max_gross_exposure": 1, "pump_guard": {}},
                 strategies=[{"name": "s", "models": [{"name": "momentum"}], "blend": {"mode": "dollar_neutral"}}])
