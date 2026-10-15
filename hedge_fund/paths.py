"""Where user data lives: ~/.hedge-fund/.

Everything the user owns — mandates, paper funds and their ledgers,
backtest results, API caches, and the .env key file — lives under one home
directory, outside the package. The package directory stays read-only code,
so a pipx install behaves exactly like a checkout.

    mandates/<name>.yaml           templates: the desk, no state
    paper/<name>/                  a deployed fund: fund.yaml, ledger/, broker.json,
                                   control.json, events.jsonl
    research/<fund>-<start>-<end>-<stamp>.json   backtest results, disposable

Textual-free and import-light on purpose: every layer (CLI, TUI, caches)
anchors its paths here, and nothing here may import them back.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

USER_DIR = Path.home() / ".hedge-fund"
MANDATES_DIR = USER_DIR / "mandates"
PAPER_DIR = USER_DIR / "paper"
RESEARCH_DIR = USER_DIR / "research"
CACHE_DIR = USER_DIR / "cache"
ENV_PATH = USER_DIR / ".env"

# The example mandate ships inside the package; it is copied out (never read
# in place) so users edit their copy, not the install.
EXAMPLE_MANDATE = Path(__file__).resolve().parent / "fund" / "example.yaml"


# Ready-made fund plans that ship with the package (fund/plans/). Each is
# copied into mandates/ once; a plan you delete stays deleted, because the
# names already offered are remembered in mandates/.seeded.
BUNDLED_PLANS_DIR = Path(__file__).resolve().parent / "fund" / "plans"
SEEDED_FILE = ".seeded"


def ensure_mandates_dir() -> Path:
    """Create the mandates dir on first use, seeded with the example, and
    offer any bundled fund plan not offered before."""
    if not MANDATES_DIR.exists():
        MANDATES_DIR.mkdir(parents=True)
        shutil.copy(EXAMPLE_MANDATE, MANDATES_DIR / "example.yaml")
    seed_plans(MANDATES_DIR)
    return MANDATES_DIR


def seed_plans(directory: Path, bundled: Path = BUNDLED_PLANS_DIR) -> list[str]:
    """Copy each bundled plan into *directory* once. Never overwrites a file
    and never re-adds one the user removed. Returns the names copied."""
    marker = Path(directory) / SEEDED_FILE
    try:
        offered = set(marker.read_text().split()) if marker.exists() else set()
    except OSError:
        offered = set()
    copied: list[str] = []
    new_names = [p for p in sorted(Path(bundled).glob("*.yaml")) if p.name not in offered]
    for plan in new_names:
        target = Path(directory) / plan.name
        if not target.exists():
            shutil.copy(plan, target)
            copied.append(plan.name)
        offered.add(plan.name)
    if new_names:
        write_atomic(marker, "\n".join(sorted(offered)) + "\n")
    return copied


def write_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* via a sibling temp file and rename, so a crash
    mid-write leaves the old file intact rather than a truncated one."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
