"""The risk engine: limits L1 to L15 and rules 1 to 16 from the Risk Officer's prompt, in code.

Ticket, account state and live price in; APPROVE, APPROVE_REDUCED or VETO out,
with a PASS, FAIL or NOT_VERIFIABLE result and the figure used for every rule.
It is deterministic: the same inputs always give the same decision, and
nothing it is told in words can change a limit.

It fails closed. An empty or unreadable limit, a missing or stale input, or
inputs that contradict each other give NOT_VERIFIABLE, and any rule that is
not PASS is a VETO. The Risk agent must call it and can never approve what it
vetoes (desk/schemas.py also rejects a RISK_DECISION that approves with a rule
not passed).

Where the design document leaves a detail open, the code takes the stricter
reading and marks the spot with "OWNER QUESTION". Those readings are listed in
the stage 3 pull request for the owner to confirm or change. None of them
loosens a rule.

An approval means the ticket is inside the limits. It is not a view that the
trade will work, and nothing here is financial advice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from desk import config as desk_config
from desk.schemas import Message

PASS, FAIL, NA = "PASS", "FAIL", "NOT_VERIFIABLE"
APPROVE, APPROVE_REDUCED, VETO = "APPROVE", "APPROVE_REDUCED", "VETO"
RULES = tuple(range(1, 17))

D0 = Decimal(0)
HUNDRED = Decimal(100)

# Every parameter the Risk Officer's prompt says must be set before any ticket
# can be checked: L1 to L15, the equity source and the trading day.
RISK_PARAMS = (
    "ACCOUNT_EQUITY", "TRADING_DAY",
    "MAX_POSITION_PCT", "MAX_RISK_PER_TRADE_PCT", "MAX_TOTAL_EXPOSURE_PCT", "MAX_OPEN_POSITIONS",
    "MAX_LEVERAGE", "MAX_CHASE_PCT", "MAX_SIGNAL_AGE", "MIN_REWARD_RISK", "MAX_COST_PCT", "FEE_MODEL",
    "MIN_LIQUIDITY", "MAX_DAILY_LOSS_PCT", "MAX_DRAWDOWN_PCT", "MAX_TRADES_PER_DAY",
    "COOLDOWN_AFTER_LOSS", "REENTRY_COOLDOWN",
)

# --- limits -----------------------------------------------------------------------

_FEE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*per\s+side\s*,\s*minimum\s+\$?\s*(\d+(?:\.\d+)?)\s*$", re.I)
_DAY = re.compile(r"^\s*(\d{1,2}):(\d{2})\s+([A-Za-z_]+(?:/[A-Za-z0-9_+-]+)*)\s*$")
_LEADING = re.compile(r"^\s*\$?\s*(\d[\d,]*(?:\.\d+)?)")


@dataclass(frozen=True)
class FeeModel:
    pct_per_side: Decimal
    minimum: Decimal

    def fee(self, notional: Decimal) -> Decimal:
        return max(notional * self.pct_per_side / HUNDRED, self.minimum)


def parse_fee_model(text: str) -> FeeModel:
    # OWNER QUESTION Q1: the document leaves FEE_MODEL as words. The engine
    # needs numbers, so it reads "<percent>% per side, minimum <amount>".
    m = _FEE.match(text)
    if not m:
        raise ValueError('FEE_MODEL must read "<percent>% per side, minimum <amount>", e.g. "0.5% per side, minimum 0"')
    return FeeModel(Decimal(m.group(1)), Decimal(m.group(2)))


@dataclass(frozen=True)
class TradingDay:
    start: time
    tz: ZoneInfo

    def start_of_day(self, at: datetime) -> datetime:
        local = at.astimezone(self.tz)
        start = local.replace(hour=self.start.hour, minute=self.start.minute, second=0, microsecond=0)
        if local < start:
            start -= timedelta(days=1)
        return start


def parse_trading_day(text: str) -> TradingDay:
    # OWNER QUESTION Q1: TRADING_DAY is words in the document. The engine reads
    # "<HH:MM> <IANA timezone>" as the moment each 24-hour trading day starts.
    m = _DAY.match(text)
    if not m:
        raise ValueError('TRADING_DAY must read "<HH:MM> <timezone>", e.g. "00:00 UTC" or "09:30 America/New_York"')
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("TRADING_DAY start time is not a time of day")
    try:
        tz = ZoneInfo(m.group(3))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"TRADING_DAY timezone {m.group(3)} is not known") from exc
    return TradingDay(time(hour, minute), tz)


def parse_min_liquidity(text: str) -> Decimal:
    # OWNER QUESTION Q1: MIN_LIQUIDITY is words. The engine compares its leading
    # number with the liquidity figure the market data supplies, in the same measure.
    m = _LEADING.match(text)
    if not m:
        raise ValueError('MIN_LIQUIDITY must start with a number, e.g. "250000 USD 24h volume"')
    return Decimal(m.group(1).replace(",", ""))


@dataclass(frozen=True)
class Limits:
    max_position_pct: Decimal  # L1
    max_risk_pct: Decimal  # L2
    max_exposure_pct: Decimal  # L3
    max_open_positions: int  # L4
    max_leverage: Decimal  # L5
    max_chase_pct: Decimal  # L6
    max_signal_age: timedelta  # L7
    min_reward_risk: Decimal  # L8
    max_cost_pct: Decimal  # L9
    fee_model: FeeModel  # L9
    min_liquidity: Decimal  # L10
    max_daily_loss_pct: Decimal  # L11
    max_drawdown_pct: Decimal  # L12
    max_trades_per_day: int  # L13
    cooldown_after_loss: timedelta  # L14
    reentry_cooldown: timedelta  # L15
    trading_day: TradingDay
    equity_source: str


class LimitsUnset(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def limits_from_config(cfg: desk_config.DeskConfig) -> Limits:
    """Limits from config/desk.yaml, or LimitsUnset naming every gap. Never a default."""
    problems = [f"{n} is empty" for n in RISK_PARAMS if cfg.states[n].status == "empty"]
    problems += [f"{n} is invalid: {cfg.states[n].problem}" for n in RISK_PARAMS if cfg.states[n].status == "invalid"]
    if problems:
        raise LimitsUnset(problems)
    v = {n: cfg.states[n].value for n in RISK_PARAMS}
    parsed: dict[str, Any] = {}
    for name, fn in (("FEE_MODEL", parse_fee_model), ("TRADING_DAY", parse_trading_day), ("MIN_LIQUIDITY", parse_min_liquidity)):
        try:
            parsed[name] = fn(v[name])
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        raise LimitsUnset(problems)
    dec = lambda n: Decimal(str(v[n]))  # noqa: E731
    return Limits(
        max_position_pct=dec("MAX_POSITION_PCT"),
        max_risk_pct=dec("MAX_RISK_PER_TRADE_PCT"),
        max_exposure_pct=dec("MAX_TOTAL_EXPOSURE_PCT"),
        max_open_positions=int(v["MAX_OPEN_POSITIONS"]),
        max_leverage=dec("MAX_LEVERAGE"),
        max_chase_pct=dec("MAX_CHASE_PCT"),
        max_signal_age=v["MAX_SIGNAL_AGE"],
        min_reward_risk=dec("MIN_REWARD_RISK"),
        max_cost_pct=dec("MAX_COST_PCT"),
        fee_model=parsed["FEE_MODEL"],
        min_liquidity=parsed["MIN_LIQUIDITY"],
        max_daily_loss_pct=dec("MAX_DAILY_LOSS_PCT"),
        max_drawdown_pct=dec("MAX_DRAWDOWN_PCT"),
        max_trades_per_day=int(v["MAX_TRADES_PER_DAY"]),
        cooldown_after_loss=v["COOLDOWN_AFTER_LOSS"],
        reentry_cooldown=v["REENTRY_COOLDOWN"],
        trading_day=parsed["TRADING_DAY"],
        equity_source=str(v["ACCOUNT_EQUITY"]),
    )


# --- inputs --------------------------------------------------------------------------

@dataclass(frozen=True)
class Position:
    ticket_id: str
    instrument: str
    direction: str  # LONG | SHORT
    units: Decimal
    entry_price: Decimal
    stop: Decimal
    current_price: Decimal

    @property
    def value(self) -> Decimal:
        return self.units * self.current_price

    @property
    def unrealised(self) -> Decimal:
        sign = 1 if self.direction == "LONG" else -1
        return sign * (self.current_price - self.entry_price) * self.units

    @property
    def risk_to_stop(self) -> Decimal:
        """What is lost from here if the stop is hit (never negative)."""
        sign = 1 if self.direction == "LONG" else -1
        return max(D0, sign * (self.current_price - self.stop) * self.units)


@dataclass(frozen=True)
class InstrumentEvent:
    instrument: str
    event: str  # EXITED | MISSED | DECLINED | VETOED
    at: datetime
    losing: bool = False  # for EXITED: was it a losing exit


@dataclass(frozen=True)
class AccountState:
    equity: Decimal  # cash + open positions at current prices
    peak_equity: Decimal  # OWNER QUESTION Q3: how deposits count toward the peak
    day_start_equity: Decimal  # equity at the start of the current trading day
    day_realised: Decimal  # realised result so far this trading day (negative = loss)
    open_positions: tuple[Position, ...]
    entries_today: int
    events: tuple[InstrumentEvent, ...]  # exits, misses, declines and vetoes, any instrument
    halted: bool  # a HALT is in force until the owner re-enables trading
    as_of: datetime
    vetoed_tickets: tuple[dict, ...] = ()  # earlier vetoed tickets' key terms, for the discipline watch


@dataclass(frozen=True)
class LivePrice:
    instrument: str
    price: Decimal
    as_of: datetime
    liquidity: Decimal | None  # in the same measure as MIN_LIQUIDITY
    expected_slippage_pct: Decimal | None  # OWNER QUESTION Q2: where this estimate comes from
    lot_size: Decimal | None  # smallest tradable step in units


@dataclass(frozen=True)
class RiskRequest:
    ticket: Message  # TRADE_TICKET, STATUS DRAFT_FOR_RISK
    signal: Message | None  # the SIGNAL_REPORT behind it
    assessment: Message | None  # the SETUP_ASSESSMENT behind it
    account: AccountState | None
    live: LivePrice | None
    now: datetime
    leverage: Decimal = Decimal(1)  # paper broker: no leverage
    related_ticket_ids: tuple[str, ...] | None = None  # OWNER QUESTION Q4: open positions on the same story


# --- outputs -----------------------------------------------------------------------------

@dataclass
class RuleResult:
    rule: int
    status: str
    figure: str


@dataclass
class RiskResult:
    ticket_id: str
    decision: str
    approved_units: Decimal | None
    approved_value: Decimal | None
    max_loss: Decimal | None
    max_loss_pct: Decimal | None
    checks: dict[int, RuleResult]
    veto_reasons: list[str]
    notes: list[str]
    discipline_flags: list[str]
    halt: bool
    account_line: str
    data_as_of: datetime

    @property
    def approved(self) -> bool:
        return self.decision in (APPROVE, APPROVE_REDUCED)

    def to_message_text(self) -> str:
        """The RISK_DECISION message for this result, in the design document's format."""
        def fmt(x: Decimal | None) -> str:
            return "" if x is None else f"{x.normalize():f}"
        size = "" if not self.approved else f"{fmt(self.approved_units)} units, value {fmt(self.approved_value)}"
        loss = "" if not self.approved else f"{fmt(self.max_loss)}, {fmt(self.max_loss_pct)}% of equity, costs included"
        # ";" separates rules in RULE_CHECKS, so it cannot appear inside a figure.
        checks = "; ".join(f"{r.rule} {r.status} {r.figure.replace(';', ',')}" for r in self.checks.values())
        lines = [
            "MESSAGE_TYPE: RISK_DECISION",
            "LABEL: ANALYSIS",
            f"TICKET_ID: {self.ticket_id}",
            f"DECISION: {self.decision}",
            f"APPROVED_SIZE: {size}",
            f"MAX_LOSS_IF_STOPPED: {loss}",
            f"RULE_CHECKS: {checks}",
            f"VETO_REASONS: {' | '.join(self.veto_reasons)}",
            "WHAT_WOULD_CHANGE_THIS: " + ("" if self.approved else _what_would_change(self)),
            f"RISK_NOTES: {' | '.join(self.notes)}",
            f"DISCIPLINE_FLAGS: {' | '.join(self.discipline_flags)}",
            f"ACCOUNT_STATE: {self.account_line}",
            f"DATA_AS_OF: {self.data_as_of.isoformat()}",
            "NOTE: Risk analysis for the Owner's decision. Approval means the ticket is inside the limits, not that it will be profitable. Not financial advice.",
        ]
        return "\n".join(lines)


def _what_would_change(r: RiskResult) -> str:
    facts = {
        7: "price back inside the entry zone and within the chase limit, with a fresh assessment",
        8: "a fresh signal report and setup assessment",
        9: "the crowd stage back to EARLY or BUILDING in a new signal report",
        10: "nothing: the desk does not trade coordinated pumps",
        11: "the cooldown passing",
        12: "the next trading day",
        14: "the next trading day",
        15: "the Owner re-enabling trading after a halt",
        16: "the missing or stale inputs supplied fresh",
    }
    out = [facts[r_.rule] for r_ in r.checks.values() if r_.status != PASS and r_.rule in facts]
    return "; ".join(dict.fromkeys(out)) or "facts that bring the ticket inside the limits; never a higher limit"


# --- the engine ---------------------------------------------------------------------------

URGENCY = re.compile(r"\b(last chance|before it runs|don'?t miss|hurry|right now|asap|now or never|moon|guaranteed|can'?t lose|fomo)\b", re.I)
JUST_UP = re.compile(r"\b(it('s| is) )?(going|goes|pumping|ripping) (up|higher)\b|\bonly goes up\b", re.I)


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    return (part / whole * HUNDRED).quantize(Decimal("0.01")) if whole else D0


def _q(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.0001')).normalize():f}"


def evaluate(request: RiskRequest, limits: Limits | None, unset: list[str] | None = None) -> RiskResult:
    """Check one ticket against every rule. Pass `limits=None` and `unset` when limits are missing."""
    t = request.ticket
    ticket_id = t["TICKET_ID"] or "UNKNOWN"
    checks: dict[int, RuleResult] = {}
    notes: list[str] = []
    flags: list[str] = []
    halt = False

    def put(rule: int, status: str, figure: str) -> None:
        # A rule keeps its worst result if set twice.
        order = {PASS: 0, NA: 1, FAIL: 2}
        old = checks.get(rule)
        if old is None or order[status] > order[old.status]:
            checks[rule] = RuleResult(rule, status, figure)

    def finish(units: Decimal | None, max_loss: Decimal | None, equity: Decimal | None) -> RiskResult:
        for n in RULES:
            if n not in checks:
                put(n, NA, "not checked: an earlier input was missing")
        failing = [r for r in checks.values() if r.status != PASS]
        requested = t["REQUESTED_SIZE"]
        if failing or units is None or units <= 0:
            if not failing:
                put(1, FAIL, "no size fits inside the limits")
                failing = [checks[1]]
            decision, units, max_loss = VETO, None, None
        else:
            decision = APPROVE if requested is not None and units == requested else APPROVE_REDUCED
        price = request.live.price if request.live else None
        acct = request.account
        if acct is not None:
            exposure = sum((p.value for p in acct.open_positions), D0)
            dd = _pct(acct.peak_equity - acct.equity, acct.peak_equity) if acct.peak_equity else D0
            day = _pct(acct.day_realised + sum((p.unrealised for p in acct.open_positions), D0), acct.day_start_equity) if acct.day_start_equity else D0
            account_line = (f"equity {_q(acct.equity)}, open positions {len(acct.open_positions)}, exposure {_pct(exposure, acct.equity) if acct.equity else 0}%, "
                            f"day result {day}%, drawdown from peak {dd}%{', HALTED' if acct.halted or halt else ''}")
        else:
            account_line = "not available"
        stamps = [request.now] + [m.data_as_of for m in (t, request.signal, request.assessment) if m is not None]
        stamps += [x.as_of for x in (request.account, request.live) if x is not None]
        return RiskResult(
            ticket_id=ticket_id,
            decision=decision,
            approved_units=units if decision != VETO else None,
            approved_value=(units * price) if decision != VETO and price is not None else None,
            max_loss=max_loss if decision != VETO else None,
            max_loss_pct=_pct(max_loss, equity) if decision != VETO and max_loss is not None and equity else None,
            checks=dict(sorted(checks.items())),
            veto_reasons=[f"{r.rule}: {r.figure}" for r in sorted(failing, key=lambda r: r.rule)] if decision == VETO else [],
            notes=notes,
            discipline_flags=flags,
            halt=halt or bool(acct and acct.halted),
            account_line=account_line,
            data_as_of=min(stamps),
        )

    # Rule 16 first: without limits and inputs nothing else can be checked.
    if limits is None:
        put(16, NA, "limits unset: " + ", ".join(unset or ["unknown"]))
        notes.append("Limits are unset, so every ticket is vetoed until config/desk.yaml is complete.")
        return finish(None, None, None)
    missing = []
    if t.type != "TRADE_TICKET":
        missing.append(f"a TRADE_TICKET (got {t.type})")
    elif t["STATUS"] != "DRAFT_FOR_RISK":
        missing.append(f"a ticket in DRAFT_FOR_RISK (got {t['STATUS']})")
    if request.signal is None or request.signal.type != "SIGNAL_REPORT":
        missing.append("the signal report")
    if request.assessment is None or request.assessment.type != "SETUP_ASSESSMENT":
        missing.append("the setup assessment")
    if request.account is None:
        missing.append("account state")
    if request.live is None:
        missing.append("live price")
    if missing:
        put(16, NA, "missing: " + ", ".join(missing))
        return finish(None, None, None)

    s, a, acct, live, now = request.signal, request.assessment, request.account, request.live, request.now
    direction = t["DIRECTION"]
    sign = 1 if direction == "LONG" else -1
    low, high = t["ENTRY_ZONE"]
    stop = t["STOP"]
    ref = t["REFERENCE_LEVEL"]

    # ---- Rule 16: inputs present, fresh and consistent --------------------------------
    problems = []
    if acct.equity is None or acct.equity <= 0:
        problems.append(f"equity is {acct.equity}: nothing to size against")
    if acct.peak_equity < acct.equity:
        problems.append("peak equity is below current equity")
    if live.instrument != t["INSTRUMENT"]:
        problems.append(f"live price is for {live.instrument}, ticket is {t['INSTRUMENT']}")
    for name, stamp in (("account state", acct.as_of), ("live price", live.as_of)):
        # OWNER QUESTION Q5: the document says "stale" without a number for live
        # data. The engine uses the signal-age limit L7 and also refuses anything
        # dated in the future.
        if stamp > now:
            problems.append(f"{name} is dated in the future ({stamp.isoformat()})")
        elif now - stamp > limits.max_signal_age:
            problems.append(f"{name} is stale ({now - stamp} old, limit {limits.max_signal_age})")
    if live.liquidity is None:
        problems.append("liquidity figure missing")
    if live.expected_slippage_pct is None:
        problems.append("expected slippage missing")
    if live.lot_size is None or live.lot_size <= 0:
        problems.append("lot size missing")
    for m in (s, a):
        if m["CANDIDATE_ID"] != t["CANDIDATE_ID"] or m["INSTRUMENT"] != t["INSTRUMENT"]:
            problems.append(f"{m.type} is for {m['CANDIDATE_ID']}/{m['INSTRUMENT']}, ticket is {t['CANDIDATE_ID']}/{t['INSTRUMENT']}")
    if a["VERDICT"] != "VALID_SETUP":
        problems.append(f"the assessment verdict is {a['VERDICT']}, not VALID_SETUP")
    else:
        for f in ("REFERENCE_LEVEL", "ENTRY_ZONE", "STOP", "TARGETS"):
            if a[f] != t[f]:
                problems.append(f"ticket {f} differs from the assessment")
        if a["DIRECTION"] != direction:
            problems.append("ticket direction differs from the assessment")
    if s["CROWD_STAGE"] != t["CROWD_STAGE"] or s["AUTHENTICITY"] != t["AUTHENTICITY"]:
        problems.append("ticket crowd stage or authenticity differs from the signal report")
    if t["REQUESTED_SIZE"] is None or t["REQUESTED_SIZE"] <= 0:
        problems.append("requested size missing")
    if problems:
        put(16, NA, "; ".join(problems))
        return finish(None, None, acct.equity)
    put(16, PASS, f"equity {_q(acct.equity)}, live {_q(live.price)} at {live.as_of.isoformat()}, inputs consistent")

    equity = acct.equity
    price = live.price
    entry = high if direction == "LONG" else low  # worst fill inside the zone
    requested = t["REQUESTED_SIZE"]

    # ---- Rule 2: no stop, no trade ----------------------------------------------------
    if stop is None or (direction == "LONG" and stop >= low) or (direction == "SHORT" and stop <= high):
        put(2, FAIL, f"stop {stop} is not on the losing side of the entry zone")
    else:
        put(2, PASS, f"stop {_q(stop)} set before entry")

    # ---- Rule 4: a stop moves only to reduce risk (no stop change in a new ticket) ---------
    put(4, PASS, "no stop change requested")

    # ---- Rule 5: leverage and position count ----------------------------------------------
    same = [p for p in acct.open_positions if p.instrument == t["INSTRUMENT"]]
    new_position = not same
    if request.leverage > limits.max_leverage:
        put(5, FAIL, f"leverage {request.leverage} above L5 {limits.max_leverage}")
    elif new_position and len(acct.open_positions) >= limits.max_open_positions:
        put(5, FAIL, f"{len(acct.open_positions)} open, L4 allows {limits.max_open_positions}")
    else:
        put(5, PASS, f"leverage {request.leverage}, {len(acct.open_positions)} open of L4 {limits.max_open_positions}")

    # ---- Rule 3: never add to a loser ---------------------------------------------------------
    if any(p.direction != direction for p in same):
        put(3, FAIL, "an open position in this instrument runs the other way")
    elif any(p.unrealised < 0 for p in same):
        put(3, FAIL, "adding to a losing position")
    else:
        put(3, PASS, "no open position in this instrument" if not same else "adding to a winner: combined position checked")

    # ---- Rule 7: chase ---------------------------------------------------------------------------
    chase = abs(price - ref) / ref * HUNDRED if ref else None
    if not (low <= price <= high):
        put(7, FAIL, f"live {_q(price)} outside entry zone {_q(low)} to {_q(high)}")
    elif chase is None or chase > limits.max_chase_pct:
        put(7, FAIL, f"live {_q(price)} is {_q(chase or D0)}% from reference {_q(ref)}, L6 {limits.max_chase_pct}%")
    else:
        put(7, PASS, f"live {_q(price)} in zone, {_q(chase)}% from reference, L6 {limits.max_chase_pct}%")

    # ---- Rule 8: age ------------------------------------------------------------------------------
    ages = {"signal": now - s.data_as_of, "assessment": now - a.data_as_of}
    old = [f"{k} {v} old" for k, v in ages.items() if v > limits.max_signal_age]
    if a["VALID_UNTIL"] <= now:
        old.append(f"assessment expired at {a['VALID_UNTIL'].isoformat()}")
    if any(v < timedelta(0) for v in ages.values()):
        old.append("a report is dated in the future")
    put(8, FAIL if old else PASS, "; ".join(old) if old else f"signal {ages['signal']}, assessment {ages['assessment']}, L7 {limits.max_signal_age}")

    # ---- Rule 9: crowd stage ----------------------------------------------------------------------
    # OWNER QUESTION Q6: a FOMO crowd is buying, so the engine treats LONG as
    # "in the direction of the crowd" and SHORT (a fade) as against it.
    stage = s["CROWD_STAGE"]
    with_crowd = direction == "LONG"
    if with_crowd and stage in ("CROWDED", "EXHAUSTION", "UNCLEAR"):
        put(9, FAIL, f"LONG with the crowd at stage {stage}")
    else:
        put(9, PASS, f"{direction} at stage {stage}")

    # ---- Rule 10: authenticity ------------------------------------------------------------------
    auth = s["AUTHENTICITY"]
    if auth == "COORDINATED_PUMP":
        put(10, FAIL, "authenticity COORDINATED_PUMP")
    else:
        put(10, PASS, f"authenticity {auth}")
        if auth in ("PROMOTED", "UNKNOWN"):
            notes.append(f"Authenticity is {auth}: see the signal report's evidence before approving.")

    # ---- Rule 11: cooldowns ---------------------------------------------------------------------
    recent = [e for e in acct.events if e.instrument == t["INSTRUMENT"] and e.event in ("EXITED", "MISSED", "DECLINED", "VETOED") and now - e.at < limits.reentry_cooldown]
    losing = [e for e in acct.events if e.event == "EXITED" and e.losing and now - e.at < limits.cooldown_after_loss]
    if recent:
        e = max(recent, key=lambda e: e.at)
        put(11, FAIL, f"{e.event.lower()} {t['INSTRUMENT']} at {e.at.isoformat()}, inside L15 {limits.reentry_cooldown}")
    elif losing:
        e = max(losing, key=lambda e: e.at)
        put(11, FAIL, f"losing exit at {e.at.isoformat()}, inside L14 {limits.cooldown_after_loss}")
    else:
        put(11, PASS, f"no exit, miss or decline on {t['INSTRUMENT']} inside L15, no losing exit inside L14")

    # ---- Rule 12: entries today -----------------------------------------------------------------
    if acct.entries_today >= limits.max_trades_per_day:
        put(12, FAIL, f"{acct.entries_today} entries today, L13 {limits.max_trades_per_day}")
    else:
        put(12, PASS, f"{acct.entries_today} of L13 {limits.max_trades_per_day} entries today")

    # ---- Rules 14 and 15: the day and the drawdown ----------------------------------------------
    open_result = sum((p.unrealised for p in acct.open_positions), D0)
    day_loss = -(acct.day_realised + open_result)  # positive = loss
    day_cap = acct.day_start_equity * limits.max_daily_loss_pct / HUNDRED
    dd = acct.peak_equity - equity
    dd_cap = acct.peak_equity * limits.max_drawdown_pct / HUNDRED
    if acct.halted:
        put(15, FAIL, "a HALT is in force until the Owner re-enables trading")
    elif dd >= dd_cap:
        halt = True
        put(15, FAIL, f"equity {_q(equity)} is {_pct(dd, acct.peak_equity)}% below peak {_q(acct.peak_equity)}, L12 {limits.max_drawdown_pct}%: HALT")
    if day_loss >= day_cap:
        put(14, FAIL, f"day loss {_q(day_loss)} ({_pct(day_loss, acct.day_start_equity)}%) reached L11 {limits.max_daily_loss_pct}%")

    # ---- Rule 13: reward-to-risk, cost and liquidity ---------------------------------------------
    slip = live.expected_slippage_pct

    def costs(units: Decimal) -> Decimal:
        """Round trip to the stop: fees both sides plus slippage both sides."""
        n_in, n_out = units * entry, units * stop
        return limits.fee_model.fee(n_in) + limits.fee_model.fee(n_out) + (n_in + n_out) * slip / HUNDRED

    def loss_if_stopped(units: Decimal) -> Decimal:
        return units * abs(entry - stop) + costs(units)

    # ---- Rule 1 (with 3, 6, 14, 15): size ------------------------------------------------------
    lot = live.lot_size
    caps: dict[str, Decimal] = {"requested": requested}
    # Rule 6: positions on the same story count as one. OWNER QUESTION Q4: if no
    # one says which open positions are related, all of them are counted as related.
    related_ids = request.related_ticket_ids
    related = [p for p in acct.open_positions if related_ids is None or p.ticket_id in related_ids or p.instrument == t["INSTRUMENT"]]
    related_value = sum((p.value for p in related), D0)
    related_risk = sum((p.risk_to_stop for p in related), D0)
    caps["L1"] = (equity * limits.max_position_pct / HUNDRED - related_value) / entry
    total_value = sum((p.value for p in acct.open_positions), D0)
    caps["L3"] = (equity * limits.max_exposure_pct / HUNDRED - total_value) / entry
    budgets = {"L2": equity * limits.max_risk_pct / HUNDRED - related_risk}
    if not acct.halted and dd < dd_cap:
        budgets["L12"] = dd_cap - dd
    if day_loss < day_cap:
        budgets["L11"] = day_cap - day_loss

    def largest_within(budget: Decimal) -> Decimal:
        if budget <= 0 or entry == stop:
            return D0
        lo, hi = D0, budget / abs(entry - stop)  # loss per unit is at least the distance
        for _ in range(200):
            mid = (lo + hi) / 2
            if loss_if_stopped(mid) <= budget:
                lo = mid
            else:
                hi = mid
        return lo

    for name, budget in budgets.items():
        caps[name] = largest_within(budget)
    raw_units = min(caps.values())
    units = (raw_units / lot).to_integral_value(rounding=ROUND_DOWN) * lot if raw_units > 0 else D0
    binding = min(caps, key=lambda k: caps[k])
    if units <= 0:
        put(1, FAIL, f"no size fits: {binding} allows {_q(max(raw_units, D0))} units, lot {_q(lot)}")
        if binding == "L11":
            put(14, FAIL, f"the day's loss room {_q(budgets.get('L11', D0))} cannot cover one lot")
        if binding == "L12":
            put(15, FAIL, f"the drawdown room {_q(budgets.get('L12', D0))} cannot cover one lot")
        if binding == "L1" and related and related_ids is None:
            notes.append("Rule 6: no one said which open positions share this story, so all of them were counted as related.")
    else:
        put(1, PASS, f"{_q(units)} units (binding: {binding}; requested {_q(requested)})")
        if binding in ("L11", "L12"):
            notes.append(f"Size cut so a stop-out cannot take the {'day past L11' if binding == 'L11' else 'account past L12'}.")
    if 14 not in checks:
        put(14, PASS, f"day loss {_q(max(day_loss, D0))} of cap {_q(day_cap)}; worst case after this ticket stays inside L11")
    if 15 not in checks:
        put(15, PASS, f"drawdown {_pct(max(dd, D0), acct.peak_equity)}% of L12 {limits.max_drawdown_pct}%; worst case stays inside")

    # Rule 6 result
    if related_ids is None and len(acct.open_positions) > 0:
        put(6, PASS, f"all {len(acct.open_positions)} open positions counted as related (none declared)")
    else:
        put(6, PASS, f"{len(related)} related position(s), value {_q(related_value)}, counted with this ticket for L1 to L3")

    # Rule 13 at the approved size (or one lot when nothing fits, so the figure still shows)
    size_for_cost = units if units > 0 else lot
    first_target = t["TARGETS"][0]
    reward = sign * (first_target - entry) * size_for_cost - costs(size_for_cost)
    risk = loss_if_stopped(size_for_cost)
    rr = reward / risk if risk > 0 else D0
    cost_pct = costs(size_for_cost) / (size_for_cost * entry) * HUNDRED
    bad13 = []
    if rr < limits.min_reward_risk:
        bad13.append(f"reward-to-risk {_q(rr)} after costs, L8 {limits.min_reward_risk}")
    if cost_pct > limits.max_cost_pct:
        bad13.append(f"round-trip cost {_q(cost_pct)}%, L9 {limits.max_cost_pct}%")
    if live.liquidity < limits.min_liquidity:
        bad13.append(f"liquidity {_q(live.liquidity)}, L10 {_q(limits.min_liquidity)}")
    if bad13:
        put(13, FAIL, "; ".join(bad13))
        if cost_pct > limits.max_cost_pct:
            put(1, FAIL, f"cannot exit at the stop within L9 at this size ({_q(cost_pct)}%)")
    else:
        put(13, PASS, f"reward-to-risk {_q(rr)} to first target, cost {_q(cost_pct)}%, liquidity {_q(live.liquidity)}")

    # ---- Discipline watch (evidence only; the rules above decide) --------------------------------------
    thesis = t["THESIS"] or ""
    if URGENCY.search(thesis) or URGENCY.search(t["INVALIDATION"] or ""):
        flags.append("urgency wording in the ticket")
    # A flag is evidence for the Owner, never a decision, so a loose match is acceptable.
    if JUST_UP.search(thesis) or len(thesis.split()) < 6:
        flags.append('thesis amounts to "it is going up"')
    if units > 0 and requested > units:
        flags.append(f"requested size {_q(requested)} above what the limits allow ({_q(units)})")
    key = {f: t.raw.get(f) for f in ("INSTRUMENT", "DIRECTION", "ENTRY_ZONE", "STOP", "TARGETS")}
    if any(all(v.get(f) == key[f] for f in key) for v in acct.vetoed_tickets):
        flags.append("a vetoed ticket resubmitted with no change in its terms")
    if any(e.instrument == t["INSTRUMENT"] and e.event == "MISSED" for e in acct.events):
        flags.append("a new ticket on something the desk missed")

    max_loss = loss_if_stopped(units) if units > 0 else None
    return finish(units, max_loss, equity)


def check_stop_change(position: Position, new_stop: Decimal | None) -> RuleResult:
    """Rule 4: a stop moves only in the direction that reduces risk."""
    if new_stop is None:
        return RuleResult(4, FAIL, "removing a stop")
    tighter = new_stop > position.stop if position.direction == "LONG" else new_stop < position.stop
    if new_stop == position.stop:
        return RuleResult(4, PASS, "stop unchanged")
    if not tighter:
        return RuleResult(4, FAIL, f"stop {position.stop} to {new_stop} widens the risk")
    wrong_side = new_stop >= position.current_price if position.direction == "LONG" else new_stop <= position.current_price
    if wrong_side:
        return RuleResult(4, FAIL, f"stop {new_stop} is through the current price {position.current_price}")
    return RuleResult(4, PASS, f"stop {position.stop} to {new_stop} reduces risk")


def evaluate_with_config(request: RiskRequest, cfg: desk_config.DeskConfig) -> RiskResult:
    """The normal entry point: limits come from config/desk.yaml, never from the caller."""
    try:
        limits = limits_from_config(cfg)
    except LimitsUnset as exc:
        return evaluate(request, None, exc.problems)
    return evaluate(request, limits)
