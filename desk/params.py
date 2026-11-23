"""The desk's 32 parameters and its five agents.

This is a transcription of docs/team-design.md ("Parameters to fill in" and
"Team design"), not a second source of truth: desk/tests/test_design_doc.py
fails if a name, a description, a "used by" list or a limit number here drifts
from the document.

A parameter has no default. `kind` only says what shape a value must have so
the desk can tell a filled value from a typo; it never supplies one.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- agents ---------------------------------------------------------------

@dataclass(frozen=True)
class Agent:
    key: str  # file name in prompts/ and, later, the subagent's name
    short: str  # the word the design document's "Used by" column uses
    name: str
    core_job: str
    consumes: str
    produces: str
    hands_off_to: str
    access: str  # what the design document says to give this agent
    order_tools: bool


# Order: the order the prompts appear in the design document.
AGENTS: tuple[Agent, ...] = (
    Agent(
        key="desk-lead",
        short="Lead",
        name="Desk Lead",
        core_job="Turn the desk's analysis into one checked trade ticket for your decision",
        consumes="Signal reports, setup assessments, risk decisions, execution reports, your instructions",
        produces="Trade ticket, approval request, desk summary",
        hands_off_to="Setup Analyst, Risk Officer, you, Execution Manager",
        access="Messaging tools and read-only market data, and no order tools.",
        order_tools=False,
    ),
    Agent(
        key="signal-scout",
        short="Scout",
        name="Signal Scout",
        core_job="Detect FOMO-driven activity and say how far along the crowd is",
        consumes="Social sentiment, news, price and volume, optional tracked-trader feeds",
        produces="Signal report: what is happening, crowd stage, authenticity",
        hands_off_to="Desk Lead",
        access="Read-only tools.",
        order_tools=False,
    ),
    Agent(
        key="setup-analyst",
        short="Analyst",
        name="Setup Analyst",
        core_job="Decide from market data whether a defined-risk trade exists right now",
        consumes="One signal report, live market data",
        produces="Setup assessment: valid, wait or no setup, with entry zone, stop, targets and time stop",
        hands_off_to="Desk Lead",
        access="Read-only market data and news.",
        order_tools=False,
    ),
    Agent(
        key="risk-officer",
        short="Risk",
        name="Risk and Discipline Officer",
        core_job="Enforce hard limits on position size, chase entries and drawdown",
        consumes="Trade ticket, live price, account equity, open positions, trade history",
        produces="Risk decision: approve, approve reduced or veto, with the maximum size; halt and reduce orders",
        hands_off_to="Desk Lead, Execution Manager, you",
        access="Read-only account, position and market data, and no order tools.",
        order_tools=False,
    ),
    Agent(
        key="execution-manager",
        short="Execution",
        name="Execution and Position Manager",
        core_job="Carry out an approved ticket exactly and manage the position by its exit plan",
        consumes="Approved ticket, your approval, live price, account state, risk orders",
        produces="Execution report, position update",
        hands_off_to="Desk Lead, Risk Officer",
        access="The only agent that gets order tools. It also needs live market data and account data.",
        order_tools=True,
    ),
)

AGENT_BY_KEY = {a.key: a for a in AGENTS}
AGENT_BY_SHORT = {a.short: a for a in AGENTS}

# --- parameters -----------------------------------------------------------

# What shape a value must have. None of these supplies or suggests a value.
TEXT = "text"  # words; a YAML list of words is joined with ", "
NUMBER = "number"  # a plain number
PERCENT = "percent"  # a plain number, read as a percentage; no % sign
COUNT = "count"  # a whole number
DURATION = "duration"  # a number and a unit of time
CHOICE = "choice"  # one of a fixed set of words from the design document

KIND_HINTS = {
    TEXT: "Words. Write it the way you want the agents to read it.",
    NUMBER: "A plain number.",
    PERCENT: "A plain number, read as a percentage. Leave out the % sign.",
    COUNT: "A whole number.",
    DURATION: "A number and a unit of time: seconds, minutes, hours, days or weeks.",
    CHOICE: "One of the listed options.",
}


@dataclass(frozen=True)
class Param:
    name: str
    sets: str  # "What it sets", word for word from the design document
    used_by: tuple[str, ...]  # agent keys
    kind: str
    group: str
    limit: str | None = None  # L1..L15 where the Risk Officer's prompt gives it one
    choices: tuple[str, ...] = ()
    minimum: float | None = None


def _used(*shorts: str) -> tuple[str, ...]:
    return tuple(AGENT_BY_SHORT[s].key for s in shorts)


_ALL = tuple(a.key for a in AGENTS)

# Display groups, in the order the page shows them.
G_MARKET = "What the desk trades"
G_SCOUT = "What the Scout watches"
G_ENTRY = "Entry quality and costs"
G_SIZE = "Position size limits"
G_LOSS = "Loss limits and pace"
G_PROCESS = "Approvals and updates"
GROUPS = (G_MARKET, G_SCOUT, G_ENTRY, G_SIZE, G_LOSS, G_PROCESS)

# Order: the order of the table in the design document.
PARAMS: tuple[Param, ...] = (
    Param("ASSET_CLASS", "What the desk trades", _ALL, TEXT, G_MARKET),
    Param("VENUE", "Exchange, broker or app where orders go", _used("Lead", "Analyst", "Execution"), TEXT, G_MARKET),
    Param("INSTRUMENT_UNIVERSE", "Which instruments are allowed, as a list or a filter", _used("Lead", "Scout"), TEXT, G_MARKET),
    Param("TIMEFRAME", "Intended holding period and chart timeframe", _used("Lead", "Scout", "Analyst"), TEXT, G_MARKET),
    Param("ALLOWED_DIRECTIONS", "Long only, or long and short", _used("Lead", "Analyst"), CHOICE, G_MARKET, choices=("LONG_ONLY", "LONG_AND_SHORT")),
    Param("TRADING_DAY", "When a trading day starts and ends, with timezone", _used("Risk"), TEXT, G_MARKET),
    Param("SOCIAL_SOURCES", "Platforms and feeds the Scout reads", _used("Scout"), TEXT, G_SCOUT),
    Param("TRACKED_TRADER_SOURCES", "Optional leaderboards or traders whose entries count as a signal. Write NONE if unused.", _used("Scout"), TEXT, G_SCOUT),
    Param("MAX_SIGNAL_AGE", "How old a signal or assessment may be at entry", _used("Scout", "Risk"), DURATION, G_SCOUT, limit="L7"),
    Param("MIN_LIQUIDITY", "Minimum liquidity an instrument needs", _used("Analyst", "Risk"), TEXT, G_ENTRY, limit="L10"),
    Param("MIN_REWARD_RISK", "Minimum reward-to-risk after costs", _used("Analyst", "Risk"), NUMBER, G_ENTRY, limit="L8", minimum=0),
    Param("FEE_MODEL", "Fees per trade, including any minimum fee", _used("Analyst", "Risk", "Execution"), TEXT, G_ENTRY, limit="L9"),
    Param("ACCOUNT_EQUITY", "A live account tool, or a figure you keep updated", _used("Risk"), TEXT, G_SIZE),
    Param("MAX_POSITION_PCT", "Largest single position, % of equity", _used("Risk"), PERCENT, G_SIZE, limit="L1", minimum=0),
    Param("MAX_RISK_PER_TRADE_PCT", "Largest loss if a stop is hit, % of equity", _used("Risk"), PERCENT, G_SIZE, limit="L2", minimum=0),
    Param("MAX_TOTAL_EXPOSURE_PCT", "Largest combined open exposure, % of equity", _used("Risk"), PERCENT, G_SIZE, limit="L3", minimum=0),
    Param("MAX_OPEN_POSITIONS", "Most positions open at once", _used("Risk"), COUNT, G_SIZE, limit="L4", minimum=0),
    Param("MAX_LEVERAGE", "Highest leverage allowed. 1 means none.", _used("Risk"), NUMBER, G_SIZE, limit="L5", minimum=1),
    Param("MAX_CHASE_PCT", "How far from the setup's reference level an entry may be, %", _used("Analyst", "Risk"), PERCENT, G_ENTRY, limit="L6", minimum=0),
    Param("MAX_COST_PCT", "Largest round-trip fees plus slippage, % of position", _used("Risk"), PERCENT, G_ENTRY, limit="L9", minimum=0),
    Param("MAX_DAILY_LOSS_PCT", "Loss in one trading day that stops new entries, % of equity", _used("Risk"), PERCENT, G_LOSS, limit="L11", minimum=0),
    Param("MAX_DRAWDOWN_PCT", "Fall from peak equity that halts the desk, %", _used("Risk"), PERCENT, G_LOSS, limit="L12", minimum=0),
    Param("MAX_TRADES_PER_DAY", "Most entries per trading day", _used("Risk"), COUNT, G_LOSS, limit="L13", minimum=0),
    Param("COOLDOWN_AFTER_LOSS", "Wait after a losing exit before any new entry", _used("Risk"), DURATION, G_LOSS, limit="L14"),
    Param("REENTRY_COOLDOWN", "Wait before returning to an instrument the desk exited, missed or passed on", _used("Risk"), DURATION, G_LOSS, limit="L15"),
    Param("LIMIT_CHANGE_COOLDOWN", "Delay before an edited limit takes effect", _used("Risk"), DURATION, G_LOSS),
    Param("APPROVAL_MODE", "`MANUAL` means you approve every ticket. Any other behaviour is yours to define here.", _used("Lead", "Execution"), TEXT, G_PROCESS),
    Param(
        "RISK_EXIT_MODE",
        "`PRE_AUTHORISED` lets Risk-ordered reductions execute at once. `ASK_OWNER` sends them to you first.",
        _used("Risk", "Execution"),
        CHOICE,
        G_PROCESS,
        choices=("PRE_AUTHORISED", "ASK_OWNER"),
    ),
    Param("TICKET_EXPIRY", "How long an approved ticket stays valid", _used("Lead", "Execution"), DURATION, G_PROCESS),
    Param("MAX_SLIPPAGE_PCT", "Worst acceptable fill against the ticket price, %", _used("Execution"), PERCENT, G_ENTRY, minimum=0),
    Param("OWNER_CHANNEL", "Where agents reach you for approvals and alerts", _used("Lead", "Risk", "Execution"), TEXT, G_PROCESS),
    Param("SUMMARY_CADENCE", "How often you get a desk summary and position updates", _used("Lead", "Execution"), TEXT, G_PROCESS),
)

PARAM_NAMES: tuple[str, ...] = tuple(p.name for p in PARAMS)
PARAM_BY_NAME = {p.name: p for p in PARAMS}

assert len(PARAMS) == 32 and len(PARAM_BY_NAME) == 32, "the design document defines exactly 32 parameters"
