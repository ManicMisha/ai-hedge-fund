"""Typed message formats, from the "Output formats" in docs/team-design.md.

Every agent message is plain text, one `FIELD: value` per line, with the field
names the design document gives. `parse()` turns that text into a typed
`Message` or raises `MessageError` with every problem found. A message is
rejected when it:

- has no MESSAGE_TYPE, or one the document does not define;
- has no LABEL, or a label other than the one its type allows;
- has no DATA_AS_OF, or one that is not a full date and time with a timezone;
- misses a field, repeats one, or adds one the format does not have;
- leaves a required field empty, or puts a value in the wrong shape;
- changes the fixed NOTE or APPROVAL_SCOPE text;
- contradicts itself (an approval with a failed rule check, an order placed
  with a failed check A to D).

Shapes the document leaves open are fixed here so code can read them; the
agents are told these shapes in the agents stage:

- times: ISO 8601 with a timezone, e.g. 2026-11-23T14:05:00Z
- a price or figure: a number first, then any words, e.g. "1.25, top of the base"
- ENTRY_ZONE: "low to high", e.g. "1.20 to 1.26"
- TARGETS: prices separated by ";", each optionally followed by words
- RULE_CHECKS: "1 PASS figure; 2 PASS figure; ... 16 PASS figure", every rule once
- CHECKS (execution): "A PASS, B PASS, C PASS, D PASS"

No message is a guarantee or financial advice, and the fixed NOTE lines say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

ANALYSIS = "ANALYSIS"
PROPOSED_ACTION = "PROPOSED ACTION"

# --- field kinds -------------------------------------------------------------

TEXT = "text"
ENUM = "enum"
TIME = "time"
PRICE = "price"  # a leading number, then optional words
ZONE = "zone"
PRICES = "prices"
RULES = "rules"
CHECKS = "checks"
FIXED = "fixed"

_NUM = re.compile(r"^\s*\$?\s*([+-]?\d[\d,]*(?:\.\d+)?|[+-]?\.\d+)")
_ZONE = re.compile(r"^\s*\$?\s*([\d,]*\.?\d+)\s*(?:to|-|–)\s*\$?\s*([\d,]*\.?\d+)\s*$", re.I)
_EMPTY = {"", "NONE", "N/A"}


class MessageError(ValueError):
    def __init__(self, problems: list[str], message_type: str | None = None) -> None:
        self.problems = problems
        self.message_type = message_type
        head = f"Rejected {message_type or 'message'}: "
        super().__init__(head + "; ".join(problems))


def parse_number(raw: str) -> Decimal:
    m = _NUM.match(raw)
    if not m:
        raise ValueError("must start with a number")
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - regex already guards this
        raise ValueError("must start with a number") from exc


def parse_zone(raw: str) -> tuple[Decimal, Decimal]:
    m = _ZONE.match(raw)
    if not m:
        raise ValueError('must be "low to high", e.g. 1.20 to 1.26')
    low, high = (Decimal(g.replace(",", "")) for g in m.groups())
    if low > high:
        raise ValueError("low is above high")
    return low, high


def parse_prices(raw: str) -> list[Decimal]:
    parts = [p for p in raw.split(";") if p.strip()]
    if not parts:
        raise ValueError("needs at least one price")
    return [parse_number(p) for p in parts]


def parse_time(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("must be an ISO 8601 date and time, e.g. 2026-11-23T14:05:00Z") from exc
    if dt.tzinfo is None:
        raise ValueError("must include a timezone, e.g. 2026-11-23T14:05:00Z")
    return dt


RULE_STATES = ("PASS", "FAIL", "NOT_VERIFIABLE")
_RULE = re.compile(r"^\s*(\d{1,2})\s*[:=.]?\s*(PASS|FAIL|NOT_VERIFIABLE)\b\s*(.*)$")


def parse_rules(raw: str) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for part in raw.split(";"):
        if not part.strip():
            continue
        m = _RULE.match(part)
        if not m:
            raise ValueError(f'"{part.strip()[:40]}" is not "<rule> PASS|FAIL|NOT_VERIFIABLE <figure>"')
        n = int(m.group(1))
        if n in out:
            raise ValueError(f"rule {n} appears twice")
        out[n] = (m.group(2), m.group(3).strip())
    if sorted(out) != list(range(1, 17)):
        missing = [n for n in range(1, 17) if n not in out]
        extra = [n for n in out if not 1 <= n <= 16]
        raise ValueError(f"must give every rule 1 to 16 once (missing {missing}, unknown {extra})")
    return out


_CHECK = re.compile(r"^\s*([ABCD])\s*[:=]?\s*(PASS|FAIL)\s*$")


def parse_checks(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(","):
        m = _CHECK.match(part)
        if not m:
            raise ValueError(f'"{part.strip()[:20]}" is not "<A-D> PASS|FAIL"')
        if m.group(1) in out:
            raise ValueError(f"check {m.group(1)} appears twice")
        out[m.group(1)] = m.group(2)
    if sorted(out) != ["A", "B", "C", "D"]:
        raise ValueError("must give each of A, B, C and D once")
    return out


# --- field and message specs ------------------------------------------------

Fields = dict[str, str]
Cond = Callable[[Fields], bool]
ALWAYS: Cond = lambda f: True


@dataclass(frozen=True)
class F:
    name: str
    kind: str = TEXT
    options: tuple[str, ...] = ()
    required: Cond = ALWAYS
    fixed: str = ""


@dataclass(frozen=True)
class Spec:
    type: str
    labels: tuple[str, ...]
    fields: tuple[F, ...]
    checks: tuple[Callable[[dict[str, Any]], str | None], ...] = ()


def _is(field_name: str, *values: str) -> Cond:
    return lambda f: f.get(field_name, "").strip() in values


def _not(field_name: str, *values: str) -> Cond:
    return lambda f: f.get(field_name, "").strip() not in values


PRIORITY = ("URGENT", "NORMAL")
CROWD_STAGES = ("EARLY", "BUILDING", "CROWDED", "EXHAUSTION", "UNCLEAR")
AUTHENTICITY = ("ORGANIC", "PROMOTED", "COORDINATED_PUMP", "UNKNOWN")
CONFIDENCE = re.compile(r"^(LOW|MEDIUM|HIGH)\b")
VERDICTS = ("VALID_SETUP", "WAIT", "NO_SETUP", "INSUFFICIENT_DATA")
TICKET_STATUSES = ("DRAFT_FOR_RISK", "RISK_APPROVED_AWAITING_OWNER", "OWNER_APPROVED", "DECLINED", "VETOED", "EXPIRED", "MISSED")
DECISIONS = ("APPROVE", "APPROVE_REDUCED", "VETO")
RISK_ORDERS = ("HALT_NEW_ENTRIES", "REDUCE", "CLOSE")
EXIT_MODES = ("PRE_AUTHORISED", "ASK_OWNER")
EXEC_STATUSES = ("PLACED", "FILLED", "PARTIAL", "HOLD", "MISSED", "CANCELLED", "REJECTED_BY_VENUE", "EXITED")
EXIT_REASONS = ("STOP", "TARGET", "TIME_STOP", "RISK_ORDER", "OWNER_INSTRUCTION")

NOTE_ANALYSIS_NO_REC = "Analysis only. Not a recommendation, a guarantee or financial advice."
NOTE_ANALYSIS = "Analysis only. Not a guarantee and not financial advice."
NOTE_TICKET = "Proposed action for the Owner's decision. Not a guarantee and not financial advice."
APPROVAL_SCOPE = "Approving authorises the entry inside the entry zone before expiry, and the exit plan above. Nothing else."
NOTE_RISK_DECISION = "Risk analysis for the Owner's decision. Approval means the ticket is inside the limits, not that it will be profitable. Not financial advice."
NOTE_RISK_ORDER = "Risk-reducing action. Not a guarantee and not financial advice."
NOTE_EXEC = "Factual report of what was done. Not a recommendation, a guarantee or financial advice."
NOTE_POSITION = "Factual status. Not a recommendation, a guarantee or financial advice."


def _confidence(v: dict[str, Any]) -> str | None:
    if "CONFIDENCE" in v and not CONFIDENCE.match(v["CONFIDENCE"]):
        return "CONFIDENCE must start with LOW, MEDIUM or HIGH"
    return None


_VALID = _is("VERDICT", "VALID_SETUP")


def _valid_setup_levels(v: dict[str, Any]) -> str | None:
    if v.get("VERDICT") != "VALID_SETUP":
        return None
    if v.get("DIRECTION") not in ("LONG", "SHORT"):
        return "a VALID_SETUP needs DIRECTION LONG or SHORT"
    low, high = v["ENTRY_ZONE"]
    stop = v["STOP"]
    if v["DIRECTION"] == "LONG" and stop >= low:
        return "a LONG stop must be below the entry zone"
    if v["DIRECTION"] == "SHORT" and stop <= high:
        return "a SHORT stop must be above the entry zone"
    return None


def _ticket_levels(v: dict[str, Any]) -> str | None:
    if v.get("DIRECTION") not in ("LONG", "SHORT"):
        return "DIRECTION must be LONG or SHORT"
    low, high = v["ENTRY_ZONE"]
    if v["DIRECTION"] == "LONG" and v["STOP"] >= low:
        return "a LONG stop must be below the entry zone"
    if v["DIRECTION"] == "SHORT" and v["STOP"] <= high:
        return "a SHORT stop must be above the entry zone"
    return None


def _risk_consistent(v: dict[str, Any]) -> str | None:
    checks = v["RULE_CHECKS"]
    failing = [n for n, (state, _) in checks.items() if state != "PASS"]
    if v["DECISION"] in ("APPROVE", "APPROVE_REDUCED") and failing:
        return f"an approval cannot carry a rule that did not pass (rules {failing})"
    if v["DECISION"] == "VETO" and not failing:
        return "a VETO must name at least one rule as FAIL or NOT_VERIFIABLE"
    if v["DECISION"] == "VETO" and v.get("APPROVED_SIZE"):
        return "APPROVED_SIZE must be blank on a VETO"
    return None


def _exec_consistent(v: dict[str, Any]) -> str | None:
    if v["STATUS"] in ("PLACED", "FILLED", "PARTIAL"):
        failed = [k for k, s in v["CHECKS"].items() if s != "PASS"]
        if failed:
            return f"an order cannot be {v['STATUS']} with checks {failed} failing"
    return None


SPECS: dict[str, Spec] = {}


def _spec(spec: Spec) -> None:
    SPECS[spec.type] = spec


_spec(Spec("SIGNAL_REPORT", (ANALYSIS,), (
    F("PRIORITY", ENUM, PRIORITY),
    F("CANDIDATE_ID"),
    F("INSTRUMENT"),
    F("SIGNAL_TYPES"),
    F("WHAT_HAPPENED"),
    F("ATTENTION"),
    F("PRICE_ACTION"),
    F("CROWD_STAGE", ENUM, CROWD_STAGES),
    F("STAGE_EVIDENCE"),
    F("AUTHENTICITY", ENUM, AUTHENTICITY),
    F("AUTHENTICITY_EVIDENCE"),
    F("CATALYST"),
    F("TRACKED_TRADER"),
    F("FIRST_SEEN", TIME),
    F("DATA_AS_OF", TIME),
    F("CONFIDENCE"),
    F("GAPS"),
    F("NOTE", FIXED, fixed=NOTE_ANALYSIS_NO_REC),
), (_confidence,)))

_spec(Spec("SETUP_ASSESSMENT", (ANALYSIS,), (
    F("CANDIDATE_ID"),
    F("INSTRUMENT"),
    F("VERDICT", ENUM, VERDICTS),
    F("DIRECTION", ENUM, ("LONG", "SHORT", "NONE")),
    F("REFERENCE_LEVEL", PRICE, required=_VALID),
    F("CURRENT_PRICE", PRICE, required=_not("VERDICT", "INSUFFICIENT_DATA")),
    F("EXTENSION_PCT", PRICE, required=_not("VERDICT", "INSUFFICIENT_DATA")),
    F("ENTRY_ZONE", ZONE, required=_VALID),
    F("STOP", PRICE, required=_VALID),
    F("RISK_PER_UNIT_PCT", PRICE, required=_VALID),
    F("TARGETS", PRICES, required=_VALID),
    F("REWARD_RISK_AFTER_COSTS", PRICE, required=_VALID),
    F("TIME_STOP", required=_VALID),
    F("LIQUIDITY", required=_VALID),
    F("EVENT_RISK"),
    F("WAIT_CONDITION", required=_is("VERDICT", "WAIT")),
    F("REASON", required=_is("VERDICT", "NO_SETUP", "INSUFFICIENT_DATA")),
    F("DISAGREEMENTS"),
    F("DATA_AS_OF", TIME),
    F("VALID_UNTIL", TIME),
    F("CONFIDENCE"),
    F("GAPS"),
    F("NOTE", FIXED, fixed=NOTE_ANALYSIS_NO_REC),
), (_confidence, _valid_setup_levels)))

_APPROVED_TICKET = _is("STATUS", "RISK_APPROVED_AWAITING_OWNER", "OWNER_APPROVED")
_spec(Spec("TRADE_TICKET", (PROPOSED_ACTION,), (
    F("TICKET_ID"),
    F("CANDIDATE_ID"),
    F("STATUS", ENUM, TICKET_STATUSES),
    F("INSTRUMENT"),
    F("DIRECTION", ENUM, ("LONG", "SHORT")),
    F("THESIS"),
    F("CROWD_STAGE", ENUM, CROWD_STAGES),
    F("AUTHENTICITY", ENUM, AUTHENTICITY),
    F("REFERENCE_LEVEL", PRICE),
    F("ENTRY_ZONE", ZONE),
    F("STOP", PRICE),
    F("TARGETS", PRICES),
    F("TIME_STOP"),
    F("REQUESTED_SIZE", PRICE, required=_is("STATUS", "DRAFT_FOR_RISK")),
    F("APPROVED_SIZE", PRICE, required=_APPROVED_TICKET),
    F("MAX_LOSS_IF_STOPPED", required=_APPROVED_TICKET),
    F("ESTIMATED_COSTS"),
    F("INVALIDATION"),
    F("RISK_NOTES", required=_APPROVED_TICKET),
    F("EXPIRES_AT", TIME),
    F("DATA_AS_OF", TIME),
    F("APPROVAL_SCOPE", FIXED, fixed=APPROVAL_SCOPE),
    F("NOTE", FIXED, fixed=NOTE_TICKET),
), (_ticket_levels,)))

_spec(Spec("DESK_SUMMARY", (ANALYSIS,), (
    F("PERIOD"),
    F("OPEN_POSITIONS"),
    F("CLOSED_SINCE_LAST"),
    F("CANDIDATES"),
    F("RISK_STATE"),
    F("DISCIPLINE_FLAGS"),
    F("NEEDS_OWNER"),
    F("DATA_AS_OF", TIME),
    F("NOTE", FIXED, fixed=NOTE_ANALYSIS),
)))

_spec(Spec("ESCALATION", (ANALYSIS, PROPOSED_ACTION), (
    F("PRIORITY", ENUM, PRIORITY),
    F("WHAT_HAPPENED"),
    F("WHAT_IS_NEEDED"),
    F("DATA_AS_OF", TIME),
)))

_spec(Spec("RISK_DECISION", (ANALYSIS,), (
    F("TICKET_ID"),
    F("DECISION", ENUM, DECISIONS),
    F("APPROVED_SIZE", PRICE, required=_not("DECISION", "VETO")),
    F("MAX_LOSS_IF_STOPPED", required=_not("DECISION", "VETO")),
    F("RULE_CHECKS", RULES),
    F("VETO_REASONS", required=_is("DECISION", "VETO")),
    F("WHAT_WOULD_CHANGE_THIS", required=_is("DECISION", "VETO")),
    F("RISK_NOTES", required=lambda f: False),
    F("DISCIPLINE_FLAGS", required=lambda f: False),
    F("ACCOUNT_STATE"),
    F("DATA_AS_OF", TIME),
    F("NOTE", FIXED, fixed=NOTE_RISK_DECISION),
), (_risk_consistent,)))

_spec(Spec("RISK_ORDER", (PROPOSED_ACTION,), (
    F("PRIORITY", ENUM, ("URGENT",)),
    F("ORDER", ENUM, RISK_ORDERS),
    F("POSITIONS"),
    F("RULE"),
    F("CARRIED_OUT_UNDER", ENUM, EXIT_MODES),
    F("ACCOUNT_STATE"),
    F("DATA_AS_OF", TIME),
    F("NOTE", FIXED, fixed=NOTE_RISK_ORDER),
)))

_EXITED = _is("STATUS", "EXITED")
_spec(Spec("EXECUTION_REPORT", (ANALYSIS,), (
    F("TICKET_ID"),
    F("STATUS", ENUM, EXEC_STATUSES),
    F("CHECKS", CHECKS),
    F("ORDERS", required=_not("STATUS", "HOLD", "MISSED")),
    F("FILLS", required=_is("STATUS", "FILLED", "PARTIAL", "EXITED")),
    F("SLIPPAGE_VS_TICKET", required=_is("STATUS", "FILLED", "PARTIAL", "EXITED")),
    F("STOP_STATUS", required=_is("STATUS", "FILLED", "PARTIAL")),
    F("EXIT_REASON", ENUM, EXIT_REASONS, required=_EXITED),
    F("REALISED_RESULT", required=_EXITED),
    F("REASON", required=_is("STATUS", "HOLD", "MISSED", "CANCELLED", "REJECTED_BY_VENUE")),
    F("DATA_AS_OF", TIME),
    F("NOTE", FIXED, fixed=NOTE_EXEC),
), (_exec_consistent,)))

_spec(Spec("POSITION_UPDATE", (ANALYSIS,), (
    F("TICKET_ID"),
    F("INSTRUMENT"),
    F("SIZE", PRICE),
    F("AVERAGE_ENTRY", PRICE),
    F("CURRENT_PRICE", PRICE),
    F("UNREALISED_RESULT"),
    F("DISTANCE_TO_STOP"),
    F("TARGETS_HIT"),
    F("TIME_STOP_AT", TIME),
    F("NEXT_ACTION_UNDER_TICKET"),
    F("DATA_AS_OF", TIME),
    F("NOTE", FIXED, fixed=NOTE_POSITION),
)))

MESSAGE_TYPES = tuple(SPECS)

# --- parsing --------------------------------------------------------------------

_LINE = re.compile(r"^([A-Z][A-Z_]*):[ \t]?(.*)$")


@dataclass
class Message:
    type: str
    label: str
    raw: dict[str, str]  # every field as written
    values: dict[str, Any] = field(default_factory=dict)  # typed: Decimal, datetime, tuples, dicts

    @property
    def data_as_of(self) -> datetime:
        return self.values["DATA_AS_OF"]

    def __getitem__(self, name: str) -> Any:
        return self.values.get(name)

    def to_text(self) -> str:
        spec = SPECS[self.type]
        lines = [f"MESSAGE_TYPE: {self.type}", f"LABEL: {self.label}"]
        lines += [f"{f.name}: {self.raw.get(f.name, '')}".rstrip() for f in spec.fields]
        return "\n".join(lines)


def _convert(f: F, raw: str) -> Any:
    if f.kind == TEXT:
        return raw
    if f.kind == ENUM:
        if raw not in f.options:
            raise ValueError("must be one of " + ", ".join(f.options))
        return raw
    if f.kind == TIME:
        return parse_time(raw)
    if f.kind == PRICE:
        return parse_number(raw)
    if f.kind == ZONE:
        return parse_zone(raw)
    if f.kind == PRICES:
        return parse_prices(raw)
    if f.kind == RULES:
        return parse_rules(raw)
    if f.kind == CHECKS:
        return parse_checks(raw)
    if f.kind == FIXED:
        if raw != f.fixed:
            raise ValueError(f'must read exactly "{f.fixed}"')
        return raw
    raise AssertionError(f.kind)


def parse(text: str) -> Message:
    """Parse one message. Raises MessageError listing every problem."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for n, line in enumerate(text.strip().splitlines(), 1):
        if not line.strip():
            continue
        m = _LINE.match(line.strip())
        if not m:
            problems.append(f'line {n} is not "FIELD: value"')
            continue
        name, value = m.group(1), m.group(2).strip()
        if name in seen:
            problems.append(f"{name} appears twice")
            continue
        seen[name] = value

    mtype = seen.pop("MESSAGE_TYPE", "")
    if not mtype:
        raise MessageError(["MESSAGE_TYPE is missing"] + problems)
    if mtype not in SPECS:
        raise MessageError([f"MESSAGE_TYPE {mtype} is not one the design document defines"] + problems)
    spec = SPECS[mtype]

    label = seen.pop("LABEL", "")
    if not label:
        problems.append("LABEL is missing: every message is ANALYSIS or PROPOSED ACTION")
    elif label not in spec.labels:
        problems.append(f"LABEL must be {' or '.join(spec.labels)} for {mtype}")

    known = {f.name for f in spec.fields}
    for name in seen:
        if name not in known:
            problems.append(f"{name} is not a field of {mtype}")

    values: dict[str, Any] = {}
    for f in spec.fields:
        if f.name not in seen:
            problems.append(f"{f.name} is missing")
            continue
        raw = seen[f.name]
        needed = f.required(seen)
        # Words: only a blank line is empty ("NONE" is a real answer, e.g. TRACKED_TRADER).
        # Typed fields: blank, NONE or N/A all mean "not given".
        if f.kind in (TEXT, FIXED) or (f.kind == ENUM and raw in f.options):
            absent = raw == ""
        else:
            absent = raw.upper() in _EMPTY
        if absent:
            if needed:
                problems.append(f"{f.name} is empty")
            continue
        try:
            values[f.name] = _convert(f, raw)
        except ValueError as exc:
            problems.append(f"{f.name} {exc}")

    if not problems:
        for check in spec.checks:
            issue = check(values)
            if issue:
                problems.append(issue)
    if problems:
        raise MessageError(problems, mtype)
    return Message(type=mtype, label=label, raw={k: v for k, v in seen.items() if k in known}, values=values)
