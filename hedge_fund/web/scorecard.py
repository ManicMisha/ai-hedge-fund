"""The go-live scorecard: the bar a paper fund must clear before anyone should
even think about real money.

Information only. Passing every check does not switch anything on: this app
has no way to place a real order, and that's deliberate. The checks follow
what professional allocators look at before backing a strategy: enough
time, beating the index after costs and per unit of risk, smaller drops,
consistency rather than one lucky streak, clean operation, and a backtest
that agrees with the live record.
"""

from __future__ import annotations

import math
from pathlib import Path
from statistics import mean, stdev

from hedge_fund.paths import RESEARCH_DIR

MIN_DAYS = 126            # about six months of trading days
COST_PER_TRADE = 0.001    # 0.10% of each trade's value: spread + fees, a cautious estimate
MAX_DROP = 0.25
MIN_MONTHS = 3
CLEAN_WINDOW = 30
BACKTEST_YEARS = 2.0


def _sharpe(values: list[float]) -> float | None:
    rets = [b / a - 1 for a, b in zip(values, values[1:]) if a]
    if len(rets) < 20:
        return None
    sd = stdev(rets)
    return mean(rets) / sd * math.sqrt(252) if sd > 0 else None


def _max_drop(values: list[float]) -> float:
    peak, worst = values[0] if values else 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        if peak:
            worst = max(worst, 1 - v / peak)
    return worst


def _months(records) -> list[tuple[float, float]]:
    """(fund, S&P) return for every complete calendar month."""
    by_month: dict[str, list] = {}
    for r in records:
        by_month.setdefault(r.session[:7], []).append(r)
    complete = sorted(by_month)[:-1]  # the latest month may still be in progress
    out = []
    for prev, cur in zip(complete, complete[1:]):
        a, b = by_month[prev][-1], by_month[cur][-1]
        out.append((b.nav / a.nav - 1, b.benchmark_close / a.benchmark_close - 1))
    return out


def scorecard(records: list, events: list[dict], capital: float, mandate: str,
              backtests: list[dict] | None = None) -> dict:
    # A paper fund's first session is its starting point: it holds cash at that
    # close, so both lines start there.
    navs = [r.nav for r in records] or [capital]
    bench = [r.benchmark_close for r in records]
    days = len(records)
    ret = navs[-1] / capital - 1 if capital else 0.0
    bench_ret = bench[-1] / bench[0] - 1 if bench else 0.0
    traded = sum(f.quantity * f.price for r in records if r.executed for f in r.executed.fills)
    costs = traded * COST_PER_TRADE / capital if capital else 0.0
    drop, bench_drop = _max_drop(navs), _max_drop(bench)
    sharpe, bench_sharpe = _sharpe(navs), _sharpe(bench)
    months = _months(records)
    won = sum(1 for f, b in months if f > b)
    recent = {r.session for r in records[-CLEAN_WINDOW:]}
    failures = [e for e in events if e.get("kind") == "tick_failed"
                and (str(e.get("session")) in recent or not records)]
    matching = [b for b in (backtests or []) if b.get("mandate") == mandate and b.get("years", 0) >= BACKTEST_YEARS]
    good_bt = [b for b in matching if b["excess_return_pct"] > 0 and b["max_drawdown_pct"] < b.get("bench_max_drawdown_pct", 1)]

    checks = [
        {"id": "time", "name": "Six months on paper", "ok": days >= MIN_DAYS,
         "detail": f"{days} of {MIN_DAYS} trading days", "progress": [min(days, MIN_DAYS), MIN_DAYS],
         "why": "Anything shorter is mostly luck."},
        {"id": "beat", "name": "Beats the S&P 500", "ok": days > 0 and ret > bench_ret,
         "detail": f"{ret:+.1%} vs {bench_ret:+.1%}" if days else "not played yet",
         "why": "Otherwise an index fund does the job for less."},
        {"id": "costs", "name": "Still ahead after costs", "ok": days > 0 and ret - costs > bench_ret,
         "detail": f"about {costs:.2%} in trading costs" if days else "not played yet",
         "why": "Paper trades are free; real ones cost about 0.1% each in spreads and fees."},
        {"id": "drops", "name": "Smaller drops", "ok": days > 0 and drop < bench_drop and drop <= MAX_DROP,
         "detail": f"worst {drop:.1%} vs S&P {bench_drop:.1%}" if days else "not played yet",
         "why": f"Big drops are what make people quit. Also never worse than {MAX_DROP:.0%}."},
        {"id": "risk", "name": "Better return for its risk", "ok": sharpe is not None and bench_sharpe is not None and sharpe > bench_sharpe,
         "detail": (f"Sharpe {sharpe:.2f} vs S&P {bench_sharpe:.2f}" if sharpe is not None and bench_sharpe is not None
                    else "needs 20+ days"),
         "why": "Beating the index by taking wild risks doesn't count."},
        {"id": "steady", "name": "Wins most months", "ok": len(months) >= MIN_MONTHS and won * 2 >= len(months),
         "detail": f"ahead in {won} of {len(months)} full months" if months else "no full month yet",
         "progress": [min(len(months), MIN_MONTHS), MIN_MONTHS] if len(months) < MIN_MONTHS else None,
         "why": "One lucky week shouldn't carry the whole record."},
        {"id": "clean", "name": "Runs cleanly", "ok": days >= CLEAN_WINDOW and not failures,
         "detail": (f"{len(failures)} failed day(s) recently" if failures
                    else f"no failures in the last {CLEAN_WINDOW} days" if days >= CLEAN_WINDOW else f"needs {CLEAN_WINDOW} days"),
         "why": "A fund that breaks needs fixing before it holds anything real."},
        {"id": "backtest", "name": "Backtest agrees", "ok": bool(good_bt),
         "detail": (f"{len(good_bt)} of {len(matching)} long backtests passed" if matching
                    else f"run a {BACKTEST_YEARS:.0f}+ year backtest of {mandate}"),
         "why": "Live luck and backtest luck rarely line up by accident."},
    ]
    for c in checks:
        if c.get("progress") is None:
            c.pop("progress", None)
    passed = sum(c["ok"] for c in checks)
    return {"passed": passed, "total": len(checks), "all_passed": passed == len(checks), "checks": checks,
            "estimated_costs_pct": costs}


def backtests_for(research_dir: Path = RESEARCH_DIR) -> list[dict]:
    from hedge_fund.web.backtests import list_summaries  # late: avoid a cycle
    return list_summaries(research_dir)


def golive_alert(deployed, ledger, research_dir: Path = RESEARCH_DIR) -> dict | None:
    """An alert the day a fund first clears every check."""
    records = ledger.records()
    if len(records) < MIN_DAYS:
        return None
    card = scorecard(records, ledger.events(), deployed.spec.capital, deployed.spec.name, backtests_for(research_dir))
    if not card["all_passed"]:
        return None
    from hedge_fund.alerts import _alert
    alert = _alert(deployed.name, records[-1].session, "golive", "good",
                   f"{deployed.name} passed every go-live check",
                   "On paper it beat the S&P 500 after costs with smaller drops. A minimum bar, not a guarantee: "
                   "if you ever use real money, start small and talk to a licensed adviser.")
    alert["id"] = f"{deployed.name}:golive"  # once per fund, whenever it first happens
    return alert
