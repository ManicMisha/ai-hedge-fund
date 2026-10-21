"""Auto-play: bring every paper fund up to date, one finished day at a time.

Used by the dashboard's background timer and by `aihf play-all` (for
Windows Task Scheduler or cron). Each fund goes through the same `tick`
as the Play button, so a missed week is caught up day by day, in order,
with every decision made on the data it would have had at the time. Paper
trading only: nothing here can place a real order.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable

from hedge_fund.data.protocol import DataClient
from hedge_fund.fund import Fund, FundSpec
from hedge_fund.paper import Ledger, list_deployed, load_deployed, NothingDue, tick
from hedge_fund.pipeline.session import FundHalted

MAX_CATCH_UP_DAYS = 30


def catch_up(directory: Path, data: DataClient, *, build_fund: Callable[[FundSpec], Fund] | None = None,
             max_days: int = MAX_CATCH_UP_DAYS) -> dict:
    """Play every due session for one fund, up to *max_days*. Never raises:
    a failure is returned (tick has already frozen the fund and logged it)."""
    played: list[dict] = []
    error = None
    while len(played) < max_days:
        try:
            record = tick(directory, data, build_fund=build_fund)
        except NothingDue:
            break
        except FundHalted as exc:
            error = str(exc)
            break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            break
        played.append({"session": record.session, "nav": record.nav,
                       "traded": bool(record.executed and record.executed.fills),
                       "decided": record.decision is not None})
    last = played[-1] if played else {}
    return {
        "played": len(played),
        "sessions": [p["session"] for p in played],
        "session": last.get("session"),
        "nav": last.get("nav"),
        "traded": any(p["traded"] for p in played),
        "decided": any(p["decided"] for p in played),
        "error": error,
        "more_due": len(played) >= max_days,
    }


def play_all(
    paper_root: Path,
    data: Callable[[], AbstractContextManager[DataClient]],
    *,
    build_fund: Callable[[FundSpec], Fund] | None = None,
    check_keys: Callable[[FundSpec], str | None] | None = None,
    after_fund: Callable[[Path], object] | None = None,
) -> list[dict]:
    """Catch up every fund. Frozen funds and funds missing a key are skipped,
    with the reason. *after_fund* runs after each fund (alerts)."""
    results = []
    for directory in list_deployed(paper_root):
        row: dict = {"fund": directory.name}
        try:
            deployed = load_deployed(directory)
            frozen = Ledger(directory).halted()
            problem = check_keys(deployed.spec) if check_keys else None
            if frozen:
                row.update(status="frozen", message=frozen)
            elif problem:
                row.update(status="skipped", message=problem)
            else:
                with data() as client:
                    out = catch_up(directory, client, build_fund=build_fund)
                status = "failed" if out["error"] else "played" if out["played"] else "up_to_date"
                row.update(status=status, message=out["error"], **{k: v for k, v in out.items() if k != "error"})
        except Exception as exc:  # one broken fund must not stop the others
            row.update(status="failed", message=f"{type(exc).__name__}: {exc}")
        if after_fund is not None:
            try:
                after_fund(directory)
            except Exception as exc:
                row["alert_error"] = f"{type(exc).__name__}: {exc}"
        results.append(row)
    return results


def describe(row: dict) -> str:
    """One plain line per fund, for the terminal and the log."""
    name, status = row["fund"], row["status"]
    if status == "played":
        more = " (more days still due: run again)" if row.get("more_due") else ""
        return f"{name}: played {row['played']} day(s) through {row['session']}, value ${row['nav']:,.0f}{more}"
    if status == "up_to_date":
        return f"{name}: already up to date"
    if status == "frozen":
        return f"{name}: frozen ({row['message']}), skipped"
    if status == "skipped":
        return f"{name}: skipped, {row['message']}"
    return f"{name}: FAILED, {row.get('message')}. The fund froze itself; fix the problem and resume it."
