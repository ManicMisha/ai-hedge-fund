"""Risk limits — hard caps the analysts cannot override.

"Conviction requests, risk disposes": portfolio construction proposes target
weights, and this stage clamps them against the fund's limits. Everything
here is deterministic arithmetic — the LLM's influence over the book ends at
the Signal, and no clamp is ever negotiable.

Exposure removed by a clamp is NOT redistributed to other names; it stays in
cash. Redistributing would let the risk stage *increase* positions, which
inverts its job.
"""

from __future__ import annotations

from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer


class MarketFilter(BaseModel):
    """Cut the fund's total exposure while its benchmark is in a downtrend.

    When the benchmark's latest close is below its `ma_days` moving average,
    the gross-exposure cap drops to `risk_off_gross` for that decision. It
    only ever lowers the cap. Bear markets usually unfold over months, which
    is what gives a trend rule time to step aside; the price is lagging in
    sharp rebounds and paying for some false alarms.
    """

    model_config = ConfigDict(extra="forbid")

    ma_days: int = Field(default=200, ge=20, le=400, description="benchmark moving-average length, trading days")
    risk_off_gross: float = Field(default=0.3, gt=0, le=1.0,
                                  description="max gross exposure while the benchmark is below its average")


class PumpGuard(BaseModel):
    """Stand aside from a ticker that just spiked on abnormal volume.

    A pump looks the same on every chart: a sudden jump in price together
    with a surge in trading volume. A ticker is flagged when, on any of the
    last `cooldown_days` sessions, it rose at least `jump_1d` in a day or
    `jump_5d` over five days AND traded at least `volume_multiple` times its
    average volume of the `volume_window` sessions before. While flagged its
    target weight is zero: the fund neither buys into the spike nor keeps
    holding through the likely dump. It only ever removes exposure.
    """

    model_config = ConfigDict(extra="forbid")

    jump_1d: float = Field(default=0.25, gt=0, le=10, description="one-day rise that counts as a spike (0.25 = +25%)")
    jump_5d: float = Field(default=0.40, gt=0, le=10, description="five-day rise that counts as a spike")
    volume_multiple: float = Field(default=4.0, ge=1, le=100, description="volume vs its recent average")
    volume_window: int = Field(default=50, ge=10, le=250, description="sessions in the volume average")
    cooldown_days: int = Field(default=10, ge=1, le=60, description="sessions a flag lasts after the last spike")


class RiskLimits(BaseModel):
    """The fund's hard limits, set in its FundSpec."""

    model_config = ConfigDict(extra="forbid")

    max_position_pct: float = Field(
        gt=0, le=1.0, description="max |weight| per ticker, as a fraction of equity"
    )
    max_gross_exposure: float = Field(
        gt=0, description="max sum of |weights| across the book (1.0 = unlevered)"
    )
    market_filter: MarketFilter | None = Field(
        default=None, description="optional: lower the gross cap while the benchmark trends down"
    )
    pump_guard: PumpGuard | None = Field(
        default=None, description="optional: stand aside from tickers spiking on abnormal volume"
    )

    @model_serializer(mode="wrap")
    def _omit_absent_filter(self, handler):
        # Ledgers hash their records' JSON. A fund without a market filter must
        # serialize exactly as it did before the option existed, or every
        # older ledger would fail verification.
        data = handler(self)
        if isinstance(data, dict):
            for optional in ("market_filter", "pump_guard"):
                if getattr(self, optional) is None:
                    data.pop(optional, None)
        return data


class ClampEvent(BaseModel):
    """One limit firing — recorded so every clamp is explainable."""

    limit: Literal["max_position_pct", "max_gross_exposure", "market_filter", "pump_guard"]
    ticker: str | None = None  # None for the portfolio-level gross clamp
    before: float
    after: float


class RiskResult(BaseModel):
    """Clamped weights plus the audit trail of every limit that fired."""

    weights: dict[str, float]
    clamps: list[ClampEvent]
    scale_factor: float | None = None  # common multiplier when preserving proportions


def apply_limits(
    weights: dict[str, float], limits: RiskLimits, *, preserve_proportions: bool = False,
) -> RiskResult:
    """Clamp target weights against the fund's hard limits.

    With preserve_proportions, reduce every weight by a common factor instead
    of clipping individual names. This preserves strategy neutrality when the
    same factor is applied to each strategy's contribution.

    Without preserve_proportions, apply the existing two-stage reduction:
    1. Per-ticker cap: any |weight| above max_position_pct is clamped to the
       cap, preserving sign. One ClampEvent per clamped ticker.
    2. Gross cap: if the summed |weights| still exceed max_gross_exposure,
       every weight is scaled down proportionally. Scaling only shrinks, so
       it can never re-violate the per-ticker cap.
    """
    if not isfinite(limits.max_position_pct) or not isfinite(limits.max_gross_exposure):
        raise ValueError("risk limits must be finite")
    if not 0 < limits.max_position_pct <= 1 or limits.max_gross_exposure <= 0:
        raise ValueError("risk limits must be positive, with max_position_pct at most 1")
    for ticker, weight in weights.items():
        if not isfinite(weight):
            raise ValueError(f"{ticker}: target weight must be finite")
    if not isfinite(sum(abs(w) for w in weights.values())):
        raise ValueError("fund gross exposure must be finite")
    if preserve_proportions:
        return _scale_proportionally(weights, limits)

    clamped: dict[str, float] = {}
    clamps: list[ClampEvent] = []

    for ticker in sorted(weights):
        w = weights[ticker]
        cap = limits.max_position_pct
        if abs(w) > cap:
            new_w = cap if w > 0 else -cap
            clamps.append(ClampEvent(
                limit="max_position_pct", ticker=ticker, before=w, after=new_w,
            ))
            clamped[ticker] = new_w
        else:
            clamped[ticker] = w

    gross = sum(abs(w) for w in clamped.values())
    if gross > limits.max_gross_exposure:
        scale = limits.max_gross_exposure / gross
        clamped = {t: w * scale for t, w in clamped.items()}
        clamps.append(ClampEvent(
            limit="max_gross_exposure", before=gross, after=limits.max_gross_exposure,
        ))

    return RiskResult(weights=clamped, clamps=clamps)


def _scale_proportionally(weights: dict[str, float], limits: RiskLimits) -> RiskResult:
    largest = max((abs(w) for w in weights.values()), default=0.0)
    factor = min(1.0, limits.max_position_pct / largest) if largest else 1.0
    clamped = {t: weights[t] * factor for t in sorted(weights)}
    clamps = [
        ClampEvent(limit="max_position_pct", ticker=t, before=weights[t], after=clamped[t])
        for t in sorted(weights) if abs(weights[t]) > limits.max_position_pct
    ]
    gross = sum(abs(w) for w in clamped.values())
    if gross > limits.max_gross_exposure:
        gross_factor = limits.max_gross_exposure / gross
        clamped = {t: w * gross_factor for t, w in clamped.items()}
        factor *= gross_factor
        clamps.append(ClampEvent(limit="max_gross_exposure", before=gross, after=limits.max_gross_exposure))
    return RiskResult(weights=clamped, clamps=clamps, scale_factor=factor)
