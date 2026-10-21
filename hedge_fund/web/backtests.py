"""Backtests from the dashboard: one at a time, in the background, with a live
progress curve the page can draw as a race against the S&P 500.

Results are saved like `aihf backtest` saves them (the full JSON in
~/.hedge-fund/research/) plus a small `.summary.json` beside it with the
numbers and the curve, so the page and the go-live scorecard can list past
runs without opening multi-megabyte files.

Honest limits, shown on the page too: no trading costs are charged in the
simulation; AI analysts read "blind" prompts (no names or dates) so they
can't recall what happened; Hype Watch and the congress feed can't see the
past, so they sit out.
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import AbstractContextManager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from hedge_fund.backtesting import backtest_fund
from hedge_fund.data.protocol import DataClient
from hedge_fund.data.sessions import completed_through
from hedge_fund.fund import Fund, FundSpec
from hedge_fund.paths import write_atomic

YEARS = (1, 3, 5, 10)
_SUMMARY = ".summary.json"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class Cancelled(Exception):
    pass


def _max_drop(values: list[float]) -> float:
    peak, worst = values[0] if values else 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        if peak:
            worst = max(worst, 1 - v / peak)
    return worst


def summarize(result, mandate_file: str | None, stem: str) -> dict:
    m = result.metrics
    years = (date.fromisoformat(result.end) - date.fromisoformat(result.start)).days / 365.25
    return {
        "id": stem, "mandate": result.fund, "mandate_file": mandate_file, "universe": result.universe,
        "start": result.start, "end": result.end, "years": round(years, 2), "capital": result.capital,
        "rebalance": result.rebalance, "benchmark": result.benchmark,
        "created": datetime.now().isoformat(timespec="seconds"),
        "total_return_pct": m.total_return_pct, "annualized_return_pct": m.annualized_return_pct,
        "benchmark_return_pct": m.benchmark_return_pct, "excess_return_pct": m.excess_return_pct,
        "sharpe_ratio": m.sharpe_ratio, "max_drawdown_pct": m.max_drawdown_pct,
        "bench_max_drawdown_pct": round(_max_drop(result.benchmark_nav), 6),
        "n_cycles": m.n_cycles, "n_orders": m.n_orders,
        "curve": {"dates": result.dates, "nav": result.nav, "benchmark_nav": result.benchmark_nav},
    }


def save(result, research_dir: Path, mandate_file: str | None = None) -> dict:
    research_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result.fund}-{result.start}-{result.end}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    write_atomic(research_dir / f"{stem}.json", result.model_dump_json(indent=2))
    summary = summarize(result, mandate_file, stem)
    write_atomic(research_dir / f"{stem}{_SUMMARY}", json.dumps(summary))
    return summary


def list_summaries(research_dir: Path) -> list[dict]:
    """Past dashboard backtests, newest first, without their curves."""
    out = []
    for path in Path(research_dir).glob(f"*{_SUMMARY}"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        data.pop("curve", None)
        out.append(data)
    return sorted(out, key=lambda s: s.get("created", ""), reverse=True)


def load_summary(research_dir: Path, run_id: str) -> dict:
    if not _ID.match(run_id):
        raise FileNotFoundError(run_id)
    return json.loads((Path(research_dir) / f"{run_id}{_SUMMARY}").read_text())


def window(years: int, end: str | None = None) -> tuple[str, str]:
    if years not in YEARS:
        raise ValueError(f"pick {', '.join(map(str, YEARS))} years")
    end = end or completed_through()
    start = (date.fromisoformat(end) - timedelta(days=round(years * 365.25))).isoformat()
    return start, end


class BacktestRunner:
    """At most one backtest at a time; the page polls `status()`."""

    def __init__(self, research_dir: Path, data: Callable[[], AbstractContextManager[DataClient]],
                 build_fund: Callable[[FundSpec], Fund] | None = None) -> None:
        self.research_dir = Path(research_dir)
        self.data = data
        self.build_fund = build_fund or (lambda spec: Fund(spec, blind=True))
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._status: dict = {"state": "idle"}

    def status(self) -> dict:
        with self._lock:
            s = dict(self._status)
            if "curve" in s:
                s["curve"] = {k: list(v) for k, v in s["curve"].items()}
            return s

    def cancel(self) -> dict:
        self._cancel.set()
        return self.status()

    def start(self, spec: FundSpec, universe: list[str], start: str, end: str,
              mandate_file: str | None = None) -> dict:
        with self._lock:
            if self._status.get("state") == "running":
                raise RuntimeError("a backtest is already running")
            self._cancel.clear()
            self._status = {"state": "running", "mandate": spec.name, "universe": universe, "start": start,
                            "end": end, "done": 0, "total": 0, "benchmark": spec.benchmark, "capital": spec.capital,
                            "curve": {"dates": [], "nav": [], "benchmark_nav": []}}

        def on_valuation(i: int, n: int, value) -> None:
            if self._cancel.is_set():
                raise Cancelled()
            with self._lock:
                st = self._status
                st["done"], st["total"] = i + 1, n
                st["curve"]["dates"].append(value.as_of)
                st["curve"]["nav"].append(value.nav)
                st["curve"]["benchmark_nav"].append(value.benchmark_nav)

        def run() -> None:
            try:
                fund = self.build_fund(spec)
                with self.data() as client:
                    result = backtest_fund(fund, start, end, client, universe, on_valuation=on_valuation)
                summary = save(result, self.research_dir, mandate_file)
                update = {"state": "done", "result": {k: v for k, v in summary.items() if k != "curve"}}
            except Cancelled:
                update = {"state": "cancelled"}
            except Exception as exc:
                update = {"state": "failed", "message": f"{type(exc).__name__}: {exc}"}
            with self._lock:
                self._status.update(update)

        started = self.status()
        threading.Thread(target=run, name="aihf-backtest", daemon=True).start()
        return started

    def wait(self, timeout: float = 30.0) -> dict:
        """Block until the run ends (tests)."""
        deadline = datetime.now().timestamp() + timeout
        while datetime.now().timestamp() < deadline:
            if self.status()["state"] != "running":
                return self.status()
            threading.Event().wait(0.02)
        raise TimeoutError("backtest")
