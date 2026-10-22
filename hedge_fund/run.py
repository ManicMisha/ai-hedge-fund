"""Run the AI hedge fund.

Two modes, one engine. A backtest replays a mandate over history; a paper
fund is that same mandate deployed against a universe and advanced one
completed session at a time, keeping a ledger. Both call `advance`; only
the clock, the broker, and where the state lives differ.

Usage::

    aihf
        No arguments: the interactive app (a Textual TUI). Backtest a
        mandate, deploy and advance a paper fund, or build a new mandate.

    aihf backtest ~/.hedge-fund/mandates/example.yaml --universe AAPL,MSFT
        Replay the mandate over history (default: the last 18 months up to
        the latest completed session). Full result JSON on stdout; a copy
        lands in ~/.hedge-fund/research/. --start/--end/--out/--model.

    aihf paper create alpha --mandate ~/.hedge-fund/mandates/example.yaml --universe AAPL,MSFT
        Deploy a paper fund: snapshot the mandate, fix the universe, open an
        empty ledger and a book with the mandate's capital.

    aihf paper tick alpha
        Advance the fund by exactly one completed session and append it to
        the ledger. Idempotent; what a scheduler calls after each close:

            30 18 * * 1-5  aihf paper tick alpha >> ~/.hedge-fund/paper/alpha/tick.log 2>&1

        A missed day is never skipped — the next tick advances the oldest
        unrecorded session. --session YYYY-MM-DD names it explicitly.

    aihf paper status alpha | list | halt alpha --reason "..." | resume alpha

    aihf web
        The dashboard in your browser at http://localhost:8000: your paper
        funds, the analysts' views, the trade log, Play / Halt controls.

    aihf play-all
        Catch every paper fund up to the latest finished day and send any
        new phone alerts. For Windows Task Scheduler or cron, so funds play
        even when the dashboard isn't open. Appends to ~/.hedge-fund/autoplay.log.

    aihf fomo
        One round of the FOMO desk: save new FOMO trades, re-pick the top
        traders, read their latest trades and paper-copy them. Paper only.

A mandate is the desk — strategies, staff, risk, capital, cadence — and
never names tickers; the universe is fixed when a fund is deployed (paper)
or given per study (backtest).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console

from hedge_fund.backtesting import backtest_fund
# FDClient / CachedDataClient open the active market-data source: Financial
# Datasets when its key is set, otherwise free Yahoo prices (data/source.py).
from hedge_fund.data.source import open_client as FDClient, with_cache as CachedDataClient
from hedge_fund.data.sessions import completed_through
from hedge_fund.fund import Fund, load_spec, normalize_universe
from hedge_fund.paper import (
    deploy,
    Ledger,
    LedgerError,
    list_deployed,
    load_deployed,
    NothingDue,
    redo,
    tick,
    validate_fund_name,
)
from hedge_fund.paths import ensure_mandates_dir, PAPER_DIR, RESEARCH_DIR
from hedge_fund.pipeline import FundHalted, SessionRecord
from hedge_fund.tui.keys import apply_credentials
from hedge_fund.tui.shared import _BACKTEST_WEEKS


def main() -> None:
    apply_credentials()
    ensure_mandates_dir()
    parser = _parser()
    args = parser.parse_args()

    if args.model:
        os.environ["HEDGE_FUND_LLM_MODEL"] = args.model

    if args.command is None:
        # The interactive experience is the Textual app. Import it lazily so
        # the non-interactive paths never pay to load Textual.
        from hedge_fund.tui.app import HedgeFundApp

        HedgeFundApp().run()
        return

    if args.command == "web":
        from hedge_fund.web import serve

        serve(port=args.port, open_browser=not args.no_browser)
        return

    if args.command == "play-all":
        _play_all()
        return

    if args.command == "fomo":
        _fomo()
        return

    console = Console(stderr=True)  # status + summary on stderr; stdout stays pure JSON
    try:
        if args.command == "backtest":
            _backtest(args, parser, console)
        else:
            _paper(args, parser, console)
    except (FundHalted, NothingDue, LedgerError) as exc:
        console.print(f"[red]{exc}[/]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aihf",
        description="Run the AI hedge fund. No arguments: launch the interactive "
        "app. `backtest` replays a mandate over history; `paper` deploys and "
        "advances a paper fund.",
    )
    parser.add_argument(
        "--model",
        help="LLM the investor agents reason with, e.g. claude-opus-5-5 "
        "(default: HEDGE_FUND_LLM_MODEL env, else the built-in default); quant "
        "models ignore it",
    )
    commands = parser.add_subparsers(dest="command")

    backtest = commands.add_parser(
        "backtest", help="replay a mandate over history",
        description="Replay a mandate over history: every benchmark session from "
        "--start to --end, decisions at a rebalance session's close executed at "
        "the next close. Full result JSON on stdout, a copy in research/.",
    )
    backtest.add_argument("mandate", help="path to a mandate YAML, e.g. ~/.hedge-fund/mandates/example.yaml")
    backtest.add_argument("--universe", required=True, help=_UNIVERSE_HELP)
    backtest.add_argument("--start", help=f"first session YYYY-MM-DD (default: {_BACKTEST_WEEKS} weeks before --end)")
    backtest.add_argument("--end", help="last session YYYY-MM-DD (default: the latest completed session)")
    backtest.add_argument("--out", help="also write the result JSON to this file")

    web = commands.add_parser(
        "web", help="open the dashboard in your browser",
        description="Serve the dashboard on this computer only (127.0.0.1) and open it "
        "in your browser: your paper funds, the analysts' views, the trade log, and "
        "Play / Halt controls. Ctrl+C stops it.",
    )
    web.add_argument("--port", type=int, default=8000, help="port to serve on (default: 8000)")
    web.add_argument("--no-browser", action="store_true", help="don't open a browser tab")

    commands.add_parser(
        "play-all", help="catch every paper fund up and send alerts",
        description="Play every paper fund's due days (up to 30 each), skip frozen funds "
        "and funds missing a key, then send any new phone alerts. Safe to run as often as "
        "you like: a fund that's up to date does nothing.",
    )

    commands.add_parser(
        "fomo", help="one round of the FOMO copy desk (paper only)",
        description="Save new FOMO trades, re-pick the top traders to follow, read their latest "
        "trades and paper-copy them. Schedule it every few minutes to keep the desk running.",
    )

    paper = commands.add_parser("paper", help="deploy and advance a paper fund")
    actions = paper.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create", help="deploy a mandate as a paper fund")
    create.add_argument("name", help="fund name; becomes ~/.hedge-fund/paper/<name>/")
    create.add_argument("--mandate", required=True, help="path to a mandate YAML")
    create.add_argument("--universe", required=True, help=_UNIVERSE_HELP)

    advance = actions.add_parser("tick", help="advance the fund by one completed session")
    advance.add_argument("name")
    advance.add_argument("--session", help="the session to record, YYYY-MM-DD; must be the next unrecorded one")
    advance.add_argument("--again", action="store_true",
                         help="run the latest recorded session again and replace its record")

    status = actions.add_parser("status", help="NAV, last session, pending decision, halt state")
    status.add_argument("name")

    actions.add_parser("list", help="every deployed paper fund")

    halt = actions.add_parser("halt", help="set the kill switch: ticks refuse to trade")
    halt.add_argument("name")
    halt.add_argument("--reason", required=True)

    resume = actions.add_parser("resume", help="clear the kill switch")
    resume.add_argument("name")
    return parser


_UNIVERSE_HELP = "tickers to trade, comma or space separated, e.g. AAPL,MSFT,NVDA"


def _universe(text: str, parser: argparse.ArgumentParser) -> list[str]:
    try:
        return normalize_universe(text.replace(",", " ").split())
    except ValueError as exc:
        parser.error(str(exc))


def _fund_dir(name: str, parser: argparse.ArgumentParser) -> Path:
    try:
        validate_fund_name(name)
    except ValueError as exc:
        parser.error(str(exc))
    directory = PAPER_DIR / name
    if not directory.is_dir():
        parser.error(f"no paper fund named {name!r} under {PAPER_DIR} (see `aihf paper list`)")
    return directory


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------

def _backtest(args, parser: argparse.ArgumentParser, console: Console) -> None:
    universe = _universe(args.universe, parser)
    try:
        spec = load_spec(args.mandate)
    except ValueError as exc:
        parser.error(str(exc))
    end = args.end or completed_through()
    start = args.start or (_date.fromisoformat(end) - timedelta(weeks=_BACKTEST_WEEKS)).isoformat()
    # A backtest blinds the investor agents' prompts (no ticker, industry or
    # calendar dates): the LLM may have been trained on what those companies
    # did over the window, and that memory would otherwise score as skill.
    fund = Fund(spec, blind=True)

    with FDClient() as raw:
        fd = CachedDataClient(raw)
        with console.status(
            f"[cyan]{spec.name}: backtesting {start} → {end} "
            f"({spec.rebalance} rebalance vs {spec.benchmark}) "
            f"over {', '.join(universe)}…",
            spinner="dots",
        ):
            result = backtest_fund(fund, start, end, fd, universe)

    payload = result.model_dump_json(indent=2)
    print(payload)
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = RESEARCH_DIR / f"{spec.name}-{result.start}-{result.end}-{stamp}.json"
    saved.write_text(payload)
    # A small summary beside it, so the dashboard and the go-live scorecard can see this run.
    import json as _json
    from hedge_fund.web.backtests import summarize
    (RESEARCH_DIR / f"{saved.stem}.summary.json").write_text(
        _json.dumps(summarize(result, Path(args.mandate).stem, saved.stem)))
    if args.out:
        Path(args.out).write_text(payload)
    m = result.metrics
    console.print(
        f"[bold]{spec.name}[/] {result.start} → {result.end}  ·  "
        f"{len(result.dates)} sessions  ·  {m.n_cycles} rebalances  ·  {m.n_orders} orders  ·  "
        f"return {m.total_return_pct:+.1%} vs {spec.benchmark} {m.benchmark_return_pct:+.1%}  ·  "
        f"sharpe {m.sharpe_ratio:.2f}  ·  max drawdown {m.max_drawdown_pct:.1%}"
    )
    console.print(f"[dim]saved to {saved}[/]")


# ---------------------------------------------------------------------------
# play-all
# ---------------------------------------------------------------------------

def _play_all() -> None:
    from contextlib import contextmanager

    from hedge_fund import alerts
    from hedge_fund.autoplay import describe, play_all
    from hedge_fund.paths import USER_DIR
    from hedge_fund.settings import load_settings
    from hedge_fund.web.views import missing_keys

    @contextmanager
    def data():
        with FDClient() as raw:
            yield CachedDataClient(raw)

    settings = load_settings()
    phone = settings["alerts"]["phone"] and alerts.ntfy_target() is not None

    def after(directory: Path) -> None:
        sent = alerts.dispatch(directory, send=alerts.push_phone if phone else None, settings=settings)
        for alert in sent:
            print(f"  alert sent: {alert['title']}")

    stamp = datetime.now().isoformat(timespec="seconds")
    lines = [f"[{stamp}] {describe(row)}" for row in play_all(PAPER_DIR, data, check_keys=missing_keys, after_fund=after)]
    if not lines:
        lines = [f"[{stamp}] no paper funds yet"]
    print("\n".join(lines))
    log = USER_DIR / "autoplay.log"
    try:
        with log.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _fomo() -> None:
    from hedge_fund import alerts
    from hedge_fund.fomo import Desk, event_alert
    from hedge_fund.fomo import leaderboard as board
    from hedge_fund.paths import USER_DIR
    from hedge_fund.settings import load_settings

    settings = load_settings()
    desk = Desk(USER_DIR / "fomo", {k: v for k, v in settings["fomo"].items() if k != "enabled"})
    if board.due_windows(desk.store) or desk.state["last_collect"] is None:
        desk.collect()
    events = desk.poll()
    phone = settings["alerts"]["phone"] and "fomo" in settings["alerts"]["kinds"] and alerts.ntfy_target()
    for event in events:
        print(event["text"])
        alert = event_alert(event)
        if phone and alert:
            try:
                alerts.push_phone(alert)
            except Exception as exc:
                print(f"  (phone alert failed: {exc})")
    status = desk.status()
    for source, err in status["errors"].items():
        print(f"{source}: {err['message']}")
    print(f"FOMO desk: ${status['nav']:,.2f} ({status['return_pct']:+.1%}), following {status['followed']} traders, "
          f"{len(status['positions'])} open copies, {status['coverage']['hours']}h of FOMO trades saved.")


# ---------------------------------------------------------------------------
# paper
# ---------------------------------------------------------------------------

def _paper(args, parser: argparse.ArgumentParser, console: Console) -> None:
    if args.action == "create":
        universe = _universe(args.universe, parser)
        try:
            validate_fund_name(args.name)
            spec = load_spec(args.mandate)
        except ValueError as exc:
            parser.error(str(exc))
        try:
            directory = deploy(args.name, spec, universe, root=PAPER_DIR)
        except FileExistsError as exc:
            parser.error(str(exc))
        console.print(
            f"[bold]{args.name}[/] deployed at {directory}  ·  mandate {spec.name}  ·  "
            f"{', '.join(universe)}  ·  ${spec.capital:,.0f}  ·  {spec.rebalance}"
        )
        console.print("[dim]next: `aihf paper tick "
                      f"{args.name}` after each close (cron it), `aihf paper status {args.name}` any time[/]")
        return

    if args.action == "list":
        for directory in list_deployed(PAPER_DIR):
            print(_status_line(directory))
        return

    directory = _fund_dir(args.name, parser)

    if args.action == "tick":
        deployed = load_deployed(directory)
        with FDClient() as raw:
            fd = CachedDataClient(raw)
            verb = "running the latest session again" if args.again else "advancing one session"
            with console.status(
                f"[cyan]{deployed.name}: {verb} over {', '.join(deployed.universe)}…", spinner="dots",
            ):
                record = (redo(directory, fd, build_fund=None) if args.again
                          else tick(directory, fd, session=args.session))
        print(record.model_dump_json(indent=2))
        console.print(_tick_summary(deployed.name, record))
        return

    if args.action == "status":
        print(_status_line(directory))
        ledger = Ledger(directory)
        latest = ledger.latest()
        if latest is not None and latest.decision is not None:
            print("  pending decision, to execute at the next close:")
            for ticker, weight in sorted(latest.decision.final_weights.items()):
                print(f"    {ticker}: {weight:+.2%}")
        if latest is not None and latest.positions:
            print("  book:")
            for ticker, shares in sorted(latest.positions.items()):
                print(f"    {ticker}: {shares:+d} @ {latest.marks[ticker]:,.2f}")
        return

    ledger = Ledger(directory)
    if args.action == "halt":
        ledger.halt(args.reason)
        console.print(f"[red]{args.name} halted:[/] {args.reason}")
    elif args.action == "resume":
        ledger.resume()
        console.print(f"[green]{args.name} resumed[/]")


def _status_line(directory: Path) -> str:
    try:
        deployed = load_deployed(directory)
    except ValueError as exc:
        return f"{directory.name:<18} unavailable: {exc}"
    ledger = Ledger(directory)
    latest = ledger.latest()
    halted = ledger.halted()
    if latest is None:
        state = f"not started  ·  ${deployed.spec.capital:,.0f}"
    else:
        ret = latest.nav / deployed.spec.capital - 1
        state = (f"{len(ledger.sessions())} sessions  ·  last {latest.session}  ·  "
                 f"NAV ${latest.nav:,.2f} ({ret:+.2%})")
    flag = f"  ·  HALTED: {halted}" if halted else ""
    return (f"{deployed.name:<18} {deployed.spec.name}  ·  {' '.join(deployed.universe)}  ·  "
            f"{deployed.spec.rebalance}  ·  {state}{flag}")


def _tick_summary(name: str, record: SessionRecord) -> str:
    parts = [f"[bold]{name}[/] · session {record.session} · NAV ${record.nav:,.2f}"]
    if record.executed is not None:
        parts.append(f"executed {len(record.executed.orders)} orders from {record.executed.as_of}")
    if record.decision is not None:
        n_signals = sum(len(sr.signals) for sr in record.decision.strategies)
        parts.append(f"decided on {n_signals} signals, {len(record.decision.clamps)} clamps; executes next close")
    if record.executed is None and record.decision is None:
        parts.append("marked the book; no rebalance due")
    return "  ·  ".join(parts)


if __name__ == "__main__":
    main()
