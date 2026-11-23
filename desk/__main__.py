"""Command line for the desk.

    python -m desk check            what is filled in, what is still missing
    python -m desk render           fill the agents' prompts (refuses while anything is missing)
    python -m desk web [--port N]   the desk page at http://localhost:8787 (this computer only)

Paper trading only. Every figure is simulated. Not financial advice.
"""

from __future__ import annotations

import argparse
import sys

from desk import config
from desk.params import AGENTS, GROUPS, PARAMS


def cmd_check(args: argparse.Namespace) -> int:
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    total = len(PARAMS)
    print(f"Desk configuration: {cfg.filled_count} of {total} values filled in ({cfg.path})")
    for g in GROUPS:
        print(f"\n{g}")
        for p in PARAMS:
            if p.group != g:
                continue
            s = cfg.states[p.name]
            mark = {"filled": "ok   ", "empty": "EMPTY", "invalid": "FIX  "}[s.status]
            extra = f"  <- {s.problem}" if s.problem else ""
            print(f"  [{mark}] {p.name}{extra}")
    print()
    for a in AGENTS:
        names = config.placeholders(config.template(a.key))
        gaps = [n for n in names if cfg.states[n].status != "filled"]
        state = "ready" if not gaps else f"waiting on {len(gaps)} value(s)"
        print(f"  {a.name}: {state}")
    if cfg.complete:
        print("\nAll 32 values are filled in.")
        return 0
    print("\nThe desk will not run until every value above is filled in and valid.")
    return 1


def cmd_render(args: argparse.Namespace) -> int:
    try:
        cfg = config.load()
        prompts = config.render_all(cfg)
    except config.MissingConfig as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for key, text in prompts.items():
        print(f"{key}: {len(text)} characters, every placeholder filled")
    print("Rendered in memory only. Writing the agent files comes with the agents stage.")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from desk.web.server import serve

    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m desk", description="The FOMO desk (paper trading only).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="show which of the 32 values are filled in").set_defaults(func=cmd_check)
    sub.add_parser("render", help="fill the agents' prompts; refuses while anything is missing").set_defaults(func=cmd_render)
    web = sub.add_parser("web", help="open the desk page on this computer")
    web.add_argument("--port", type=int, default=8787)
    web.add_argument("--no-browser", action="store_true")
    web.set_defaults(func=cmd_web)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
