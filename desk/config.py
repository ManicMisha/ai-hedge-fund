"""Load config/desk.yaml, check every value, and fill the agents' prompts.

There are no defaults. An empty value stays empty, and anything that needs it
refuses to run and names what is missing. A value with the wrong shape (a
percentage with a % sign, a duration with no unit) is reported, never
corrected.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from desk import CONFIG_PATH, PROMPTS_DIR
from desk.params import AGENTS, CHOICE, COUNT, DURATION, NUMBER, PARAM_BY_NAME, PARAM_NAMES, PARAMS, PERCENT, TEXT, Param

PLACEHOLDER = re.compile(r"\[([A-Z][A-Z0-9_]*)\]")

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "wks": 604800, "week": 604800, "weeks": 604800,
}
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*$")


class ConfigError(Exception):
    """The configuration file cannot be read at all."""


class MissingConfig(Exception):
    """A step that needs values was asked to run while some are empty or invalid."""

    def __init__(self, missing: list[str], invalid: dict[str, str]) -> None:
        self.missing = missing
        self.invalid = invalid
        parts = []
        if missing:
            parts.append("empty: " + ", ".join(missing))
        if invalid:
            parts.append("invalid: " + "; ".join(f"{k} ({v})" for k, v in invalid.items()))
        super().__init__("Refusing to run. config/desk.yaml has values that are " + " | ".join(parts))


def parse_duration(raw: str) -> timedelta:
    m = _DURATION.match(raw)
    if not m or m.group(2).lower() not in _UNITS:
        raise ValueError("write a number and a unit, such as 30 minutes, 4 hours or 2 days")
    value = float(m.group(1))
    if value < 0:
        raise ValueError("cannot be negative")
    return timedelta(seconds=value * _UNITS[m.group(2).lower()])


def _is_empty(raw: Any) -> bool:
    if raw is None:
        return True
    if isinstance(raw, str):
        return raw.strip() == ""
    if isinstance(raw, (list, tuple, dict)):
        return len(raw) == 0
    return False


def check_value(param: Param, raw: Any) -> Any:
    """Return the value in the shape code uses, or raise ValueError saying what is wrong."""
    if isinstance(raw, str) and PLACEHOLDER.fullmatch(raw.strip()):
        raise ValueError("still a placeholder in square brackets")
    kind = param.kind
    if kind == TEXT:
        if isinstance(raw, list):
            items = [str(x).strip() for x in raw]
            if any(not x for x in items):
                raise ValueError("the list has an empty item")
            return ", ".join(items)
        if isinstance(raw, (dict, bool)):
            raise ValueError("write it as words or as a list of words")
        return str(raw).strip()
    if kind in (NUMBER, PERCENT, COUNT):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            hint = " Leave out the % sign." if isinstance(raw, str) and "%" in raw else ""
            raise ValueError("must be a plain number." + hint)
        if isinstance(raw, float) and not math.isfinite(raw):
            raise ValueError("must be a finite number")
        if kind == COUNT and (isinstance(raw, float) and not raw.is_integer()):
            raise ValueError("must be a whole number")
        if param.minimum is not None and raw < param.minimum:
            raise ValueError(f"must be at least {param.minimum:g}")
        return int(raw) if kind == COUNT else raw
    if kind == DURATION:
        if not isinstance(raw, str):
            raise ValueError("write a number and a unit, such as 30 minutes, 4 hours or 2 days")
        return parse_duration(raw)
    if kind == CHOICE:
        value = str(raw).strip()
        if value not in param.choices:
            raise ValueError("must be one of " + ", ".join(param.choices))
        return value
    raise AssertionError(f"unknown kind {kind}")


def display_value(raw: Any) -> str:
    """How a filled value appears inside a prompt: exactly as the owner wrote it."""
    if isinstance(raw, list):
        return ", ".join(str(x).strip() for x in raw)
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return str(raw).strip()


@dataclass
class ParamState:
    param: Param
    raw: Any
    status: str  # "empty" | "invalid" | "filled"
    problem: str = ""
    value: Any = None


@dataclass
class DeskConfig:
    path: Path
    states: dict[str, ParamState] = field(default_factory=dict)

    @property
    def missing(self) -> list[str]:
        return [n for n in PARAM_NAMES if self.states[n].status == "empty"]

    @property
    def invalid(self) -> dict[str, str]:
        return {n: s.problem for n, s in self.states.items() if s.status == "invalid"}

    @property
    def filled_count(self) -> int:
        return sum(1 for s in self.states.values() if s.status == "filled")

    @property
    def complete(self) -> bool:
        return self.filled_count == len(PARAM_NAMES)

    def require(self, names: tuple[str, ...] | list[str] = PARAM_NAMES) -> dict[str, Any]:
        """Checked values for `names`, or MissingConfig naming every gap."""
        missing = [n for n in names if self.states[n].status == "empty"]
        invalid = {n: self.states[n].problem for n in names if self.states[n].status == "invalid"}
        if missing or invalid:
            raise MissingConfig(missing, invalid)
        return {n: self.states[n].value for n in names}


def load(path: Path = CONFIG_PATH) -> DeskConfig:
    if not path.exists():
        raise ConfigError(f"{path} does not exist. It should list all 32 parameters, each empty to start.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a list of NAME: value lines")
    unknown = sorted(str(k) for k in data if k not in PARAM_BY_NAME)
    if unknown:
        raise ConfigError(f"{path} has names the design document does not define: {', '.join(unknown)}")
    cfg = DeskConfig(path=path)
    for p in PARAMS:
        raw = data.get(p.name)
        if _is_empty(raw):
            cfg.states[p.name] = ParamState(p, raw, "empty")
            continue
        try:
            cfg.states[p.name] = ParamState(p, raw, "filled", value=check_value(p, raw))
        except ValueError as exc:
            cfg.states[p.name] = ParamState(p, raw, "invalid", problem=str(exc))
    return cfg


# --- prompts ----------------------------------------------------------------

def template(agent_key: str, prompts_dir: Path = PROMPTS_DIR) -> str:
    return (prompts_dir / f"{agent_key}.md").read_text(encoding="utf-8")


def placeholders(text: str) -> list[str]:
    seen: list[str] = []
    for name in PLACEHOLDER.findall(text):
        if name not in seen:
            seen.append(name)
    return seen


def render(agent_key: str, cfg: DeskConfig, prompts_dir: Path = PROMPTS_DIR) -> str:
    """The agent's prompt with every placeholder filled, or MissingConfig naming the gaps."""
    text = template(agent_key, prompts_dir)
    names = placeholders(text)
    unknown = [n for n in names if n not in PARAM_BY_NAME]
    if unknown:
        raise ConfigError(f"prompts/{agent_key}.md uses placeholders the design document does not define: {', '.join(unknown)}")
    cfg.require(names)
    out = PLACEHOLDER.sub(lambda m: display_value(cfg.states[m.group(1)].raw), text)
    left = [n for n in placeholders(out) if n in PARAM_BY_NAME]
    if left:  # a value that itself looks like a placeholder
        raise MissingConfig(left, {})
    return out


def render_all(cfg: DeskConfig, prompts_dir: Path = PROMPTS_DIR) -> dict[str, str]:
    """Every agent's prompt, or nothing at all: one gap anywhere stops the whole render."""
    cfg.require()
    return {a.key: render(a.key, cfg, prompts_dir) for a in AGENTS}
