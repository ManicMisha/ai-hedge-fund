"""The FOMO trading desk: a five-agent, paper-trading-only desk.

The specification is docs/team-design.md. Nothing in this package can place a
real order: there is no live broker, and no exchange or wallet key is handled.
Every figure the desk produces is simulated. Nothing here is a guarantee or
financial advice.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DESIGN_DOC = REPO_ROOT / "docs" / "team-design.md"
PROMPTS_DIR = REPO_ROOT / "prompts"
CONFIG_PATH = REPO_ROOT / "config" / "desk.yaml"
