"""The pump detector: a sudden price spike on abnormal volume.

Pure arithmetic over daily (close, volume) pairs; `detect` sees only bars
on or before the assessment date. See PumpGuard in limits.py for the rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from hedge_fund.risk.limits import PumpGuard


@dataclass(frozen=True)
class Spike:
    sessions_ago: int      # 0 = the assessment session itself
    jump: float            # the larger of the 1-day and 5-day rise that fired
    volume_ratio: float | None  # None when the source has no volume


def lookback(rule: PumpGuard) -> int:
    """Bars `detect` needs: the cooldown, the 5-day run-up and the volume average."""
    return rule.cooldown_days + rule.volume_window + 10


def detect(bars: list[tuple[float, int]], rule: PumpGuard) -> Spike | None:
    """The most recent qualifying spike within the cooldown, or None.

    A day qualifies when its 1-day or 5-day rise meets the rule AND its
    volume is at least `volume_multiple` times the average of the
    `volume_window` sessions before the five-session run-up that led to it. Where the source reports no volume
    (all zeros), price alone decides, so the guard never silently switches off.
    """
    n = len(bars)
    for back in range(min(rule.cooldown_days, n - 2) + 1):
        i = n - 1 - back
        if i < 1:
            break
        close = bars[i][0]
        one_day = close / bars[i - 1][0] - 1
        five_day = close / bars[i - 5][0] - 1 if i >= 5 else one_day
        if one_day < rule.jump_1d and five_day < rule.jump_5d:
            continue
        # Normal volume is measured before the 5-session run-up, so a pump
        # lasting several days can't raise its own baseline and hide itself.
        before = max(0, i - 5)
        window = [v for _, v in bars[max(0, before - rule.volume_window):before]] \
            or [v for _, v in bars[max(0, i - rule.volume_window):i]]
        average = sum(window) / len(window) if window else 0.0
        if average > 0:
            ratio = bars[i][1] / average
            if ratio < rule.volume_multiple:
                continue
        else:
            ratio = None
        return Spike(sessions_ago=back, jump=max(one_day, five_day), volume_ratio=ratio)
    return None
