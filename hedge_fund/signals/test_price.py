"""Price-only models, slot-weighted blending, the market filter, and the plans."""

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from hedge_fund.backtesting.fund import backtest_fund
from hedge_fund.data.models import Price
from hedge_fund.fund import Fund, FundSpec, load_spec
from hedge_fund.paths import BUNDLED_PLANS_DIR, seed_plans
from hedge_fund.pipeline.stages import assess_fund
from hedge_fund.portfolio.construction import blend_signals
from hedge_fund.models import Signal
from hedge_fund.risk.limits import RiskLimits
from hedge_fund.signals.price import closes_through, LowVolatilityModel, MomentumModel, TrendModel


def weekdays(start: str, n: int) -> list[str]:
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = weekdays("2023-01-02", 420)


class Prices:
    """Canned closes; counts requests so caching can be checked."""

    def __init__(self, series):
        self.series, self.calls = series, 0

    def get_prices(self, ticker, start_date, end_date, *args, **kwargs):
        self.calls += 1
        return [Price(open=c, close=c, high=c, low=c, volume=1, time=f"{d}T00:00:00Z")
                for d, c in sorted(self.series.get(ticker, {}).items()) if start_date <= d <= end_date]

    def __getattr__(self, name):  # every non-price method: no data
        return lambda *a, **k: [] if name != "get_market_cap" else None


def ramp(start, daily):
    return {d: start * (1 + daily) ** i for i, d in enumerate(DAYS)}


UP = ramp(100, 0.002)        # steady climb
DOWN = ramp(100, -0.002)     # steady slide


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

def test_trend_holds_above_the_average_and_stands_aside_below():
    data = Prices({"UP": UP, "DOWN": DOWN})
    as_of = DAYS[300]
    up = TrendModel().predict("UP", as_of, data)
    down = TrendModel().predict("DOWN", as_of, data)
    assert up.value == 1.0 and "above" in up.reasoning
    assert down.value == 0.0 and "below" in down.reasoning


def test_momentum_skips_the_last_month_and_is_signed():
    data = Prices({"UP": UP, "DOWN": DOWN})
    as_of = DAYS[300]
    up = MomentumModel().predict("UP", as_of, data)
    down = MomentumModel().predict("DOWN", as_of, data)
    expected = UP[DAYS[300 - 21]] / UP[DAYS[300 - 252]] - 1
    assert up.components["return"] == pytest.approx(expected)
    assert up.value == pytest.approx(math.tanh(expected / 0.25))
    assert down.value < 0 < up.value


def test_low_vol_gives_calm_stocks_full_weight_and_jumpy_ones_less():
    calm = ramp(100, 0.0005)
    jumpy = {d: 100 * (1.05 if i % 2 else 0.95) for i, d in enumerate(DAYS)}
    data = Prices({"CALM": calm, "JUMPY": jumpy})
    as_of = DAYS[300]
    assert LowVolatilityModel().predict("CALM", as_of, data).value == 1.0
    assert 0 < LowVolatilityModel().predict("JUMPY", as_of, data).value < 0.5


def test_models_abstain_without_enough_history():
    data = Prices({"NEW": {d: 10.0 for d in DAYS[:50]}})
    for model in (TrendModel(), MomentumModel(), LowVolatilityModel()):
        signal = model.predict("NEW", DAYS[49], data)
        assert signal.metadata["abstained"] is True and signal.value == 0.0


def test_history_is_point_in_time_and_fetched_once_per_ticker():
    data = Prices({"UP": UP})
    early = closes_through(data, "UP", DAYS[260], 200)
    assert early[-1] == UP[DAYS[260]]  # nothing after as_of leaks in
    for i in range(261, 300):
        assert closes_through(data, "UP", DAYS[i], 200)[-1] == UP[DAYS[i]]
    assert data.calls == 1


# ---------------------------------------------------------------------------
# slot-weighted blending
# ---------------------------------------------------------------------------

def _sig(ticker, value, name="trend"):
    return Signal(model_name=name, ticker=ticker, date="2024-01-02", value=value)


def test_slot_weighting_leaves_unused_slots_in_cash():
    signals = [_sig("A", 1.0), _sig("B", 0.0), _sig("C", 1.0), _sig("D", 0.5)]
    slot = blend_signals(signals, {"trend": 1.0}, 1.0, mode="long_only",
                         investment_approaches={"trend": "long_only"}, method="slot_weighted")
    assert slot.weights == {"A": 0.25, "B": 0.0, "C": 0.25, "D": 0.125}
    normal = blend_signals(signals, {"trend": 1.0}, 1.0, mode="long_only",
                           investment_approaches={"trend": "long_only"})
    assert sum(normal.weights.values()) == pytest.approx(1.0)  # the default still fills up
    with pytest.raises(ValueError):
        blend_signals(signals, {"trend": 1.0}, 1.0, mode="dollar_neutral",
                      investment_approaches={"trend": "long_only"}, method="slot_weighted")
    with pytest.raises(ValueError, match="dollar_neutral"):
        FundSpec(schema_version=2, name="x", risk={"max_position_pct": 1, "max_gross_exposure": 1},
                 strategies=[{"name": "s", "models": [{"name": "trend"}],
                              "blend": {"mode": "dollar_neutral", "method": "slot_weighted"}}])


# ---------------------------------------------------------------------------
# market filter
# ---------------------------------------------------------------------------

def _spec(market_filter=None, cap=1.0):
    risk = {"max_position_pct": cap, "max_gross_exposure": 1.0}
    if market_filter:
        risk["market_filter"] = market_filter
    return FundSpec(schema_version=2, name="f", benchmark="SPY", risk=risk,
                    strategies=[{"name": "s", "models": [{"name": "trend"}], "blend": {"mode": "long_only"}}])


def test_market_filter_cuts_exposure_only_when_the_benchmark_is_below_its_average():
    as_of = DAYS[300]
    bear = Prices({"SPY": DOWN, "AAA": UP})
    bull = Prices({"SPY": UP, "AAA": UP})
    rule = {"ma_days": 200, "risk_off_gross": 0.3}
    cut = assess_fund(Fund(_spec(rule)), as_of, bear, ["AAA"])
    assert sum(cut.final_weights.values()) == pytest.approx(0.3)
    assert [c.limit for c in cut.clamps] == ["market_filter"]
    full = assess_fund(Fund(_spec(rule)), as_of, bull, ["AAA"])
    assert sum(full.final_weights.values()) == pytest.approx(1.0) and full.clamps == []
    plain = assess_fund(Fund(_spec()), as_of, bear, ["AAA"])
    assert sum(plain.final_weights.values()) == pytest.approx(1.0)


def test_market_filter_needs_enough_benchmark_history():
    short = Prices({"SPY": {d: 100.0 for d in DAYS[:30]}, "AAA": UP})
    with pytest.raises(ValueError, match="market filter"):
        assess_fund(Fund(_spec({"ma_days": 200})), DAYS[29], short, ["AAA"])


def test_limits_without_a_filter_serialize_exactly_as_before():
    limits = RiskLimits(max_position_pct=0.25, max_gross_exposure=1.0)
    assert json.loads(limits.model_dump_json()) == {"max_position_pct": 0.25, "max_gross_exposure": 1.0}
    with_filter = RiskLimits(max_position_pct=0.25, max_gross_exposure=1.0, market_filter={"ma_days": 150})
    assert json.loads(with_filter.model_dump_json())["market_filter"] == {"ma_days": 150, "risk_off_gross": 0.3}


# ---------------------------------------------------------------------------
# the bundled plans
# ---------------------------------------------------------------------------

def test_every_bundled_plan_loads_and_staffs():
    plans = sorted(BUNDLED_PLANS_DIR.glob("*.yaml"))
    assert {p.stem for p in plans} >= {"trend-shield", "momentum-leaders", "ai-quality-check"}
    for path in plans:
        spec = load_spec(path)
        quant_only = all(m.name in ("trend", "momentum", "low_vol") for s in spec.strategies for m in s.models)
        if quant_only:
            Fund(spec)  # builds without any API key


def test_trend_shield_backtests_on_prices_alone():
    series = {"SPY": UP, "EFA": UP, "IEF": DOWN, "VNQ": ramp(100, 0.001), "GLD": DOWN}
    spec = load_spec(BUNDLED_PLANS_DIR / "trend-shield.yaml")
    result = backtest_fund(Fund(spec), DAYS[260], DAYS[330], Prices(series), ["SPY", "EFA", "IEF", "VNQ", "GLD"])
    last = result.cycles[-1]
    held = {t for t, w in last.final_weights.items() if w > 0}
    assert held == {"SPY", "EFA", "VNQ"}                     # downtrends sit out
    assert sum(last.final_weights.values()) == pytest.approx(0.6)  # their slots stay cash


def test_seed_plans_offers_each_plan_once_and_respects_deletions(tmp_path):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "a.yaml").write_text("a: 1\n")
    (bundled / "b.yaml").write_text("b: 1\n")
    mine = tmp_path / "mandates"
    mine.mkdir()
    (mine / "b.yaml").write_text("my edits\n")
    assert seed_plans(mine, bundled) == ["a.yaml"]
    assert (mine / "b.yaml").read_text() == "my edits\n"  # never overwritten
    (mine / "a.yaml").unlink()
    assert seed_plans(mine, bundled) == [] and not (mine / "a.yaml").exists()  # deletion sticks
    (bundled / "c.yaml").write_text("c: 1\n")
    assert seed_plans(mine, bundled) == ["c.yaml"]
