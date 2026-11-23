"""Message formats: every type from the design document, and every way to get one wrong."""

import re
from datetime import timezone
from decimal import Decimal
from pathlib import Path

import pytest

from desk import DESIGN_DOC
from desk.schemas import SPECS, MessageError, parse

FIX = Path(__file__).parent / "fixtures" / "messages"
EXAMPLES = {
    "SIGNAL_REPORT": "signal_report.msg",
    "SETUP_ASSESSMENT": "setup_assessment.msg",
    "TRADE_TICKET": "trade_ticket.msg",
    "DESK_SUMMARY": "desk_summary.msg",
    "ESCALATION": "escalation.msg",
    "RISK_DECISION": "risk_decision_veto.msg",
    "RISK_ORDER": "risk_order.msg",
    "EXECUTION_REPORT": "execution_report.msg",
    "POSITION_UPDATE": "position_update.msg",
}


def text(name):
    return (FIX / EXAMPLES[name]).read_text(encoding="utf-8")


def swap(src, field, value):
    out, n = re.subn(rf"^{field}:.*$", f"{field}: {value}".rstrip(), src, flags=re.M)
    assert n == 1, field
    return out


def drop(src, field):
    out, n = re.subn(rf"^{field}:.*\n", "", src, flags=re.M)
    assert n == 1, field
    return out


def rejected(src, *needles):
    with pytest.raises(MessageError) as exc:
        parse(src)
    for needle in needles:
        assert needle in str(exc.value), (needle, str(exc.value))
    return exc.value


# --- the formats match the design document -------------------------------------

def _doc_formats():
    doc = DESIGN_DOC.read_text(encoding="utf-8")
    formats = {}
    for block in re.findall(r"```text\n(.*?)\n```", doc, flags=re.S):
        for chunk in re.split(r"\n(?=MESSAGE_TYPE: )", block):
            if not chunk.startswith("MESSAGE_TYPE: "):
                chunk = chunk[chunk.find("\nMESSAGE_TYPE: ") + 1:] if "\nMESSAGE_TYPE: " in chunk else ""
            if not chunk:
                continue
            lines = chunk.strip().split("\n\n")[0].splitlines()
            mtype = lines[0].split(": ", 1)[1]
            fields = [ln.split(":", 1)[0] for ln in lines[1:] if re.match(r"^[A-Z_]+:", ln)]
            formats.setdefault(mtype, fields)
    return formats


def test_every_format_in_the_doc_has_a_schema_with_the_same_fields_in_order():
    formats = _doc_formats()
    assert set(formats) == set(SPECS)
    for mtype, fields in formats.items():
        assert fields[0] == "LABEL"
        assert [f.name for f in SPECS[mtype].fields] == fields[1:], mtype


# --- valid examples parse ----------------------------------------------------

@pytest.mark.parametrize("mtype", list(EXAMPLES))
def test_valid_example_parses(mtype):
    msg = parse(text(mtype))
    assert msg.type == mtype
    assert msg.data_as_of.tzinfo is not None


def test_typed_values():
    a = parse(text("SETUP_ASSESSMENT"))
    assert a["ENTRY_ZONE"] == (Decimal("1.20"), Decimal("1.24"))
    assert a["STOP"] == Decimal("1.12")
    assert a["TARGETS"] == [Decimal("1.40"), Decimal("1.55")]
    assert a["VALID_UNTIL"].astimezone(timezone.utc).hour == 17
    r = parse(text("RISK_DECISION"))
    assert r["RULE_CHECKS"][7][0] == "FAIL" and len(r["RULE_CHECKS"]) == 16


def test_round_trip():
    for mtype in EXAMPLES:
        msg = parse(text(mtype))
        assert parse(msg.to_text()).values == msg.values


# --- rejections common to every type -------------------------------------------

@pytest.mark.parametrize("mtype", list(EXAMPLES))
def test_missing_data_as_of_is_rejected(mtype):
    rejected(drop(text(mtype), "DATA_AS_OF"), "DATA_AS_OF is missing")


@pytest.mark.parametrize("mtype", list(EXAMPLES))
def test_empty_data_as_of_is_rejected(mtype):
    rejected(swap(text(mtype), "DATA_AS_OF", ""), "DATA_AS_OF is empty")


@pytest.mark.parametrize("bad", ["yesterday", "2026-11-23 13:00", "2026-11-23T13:00:00"])
def test_data_as_of_needs_a_full_time_with_timezone(bad):
    rejected(swap(text("SIGNAL_REPORT"), "DATA_AS_OF", bad), "DATA_AS_OF")


@pytest.mark.parametrize("mtype", list(EXAMPLES))
def test_unlabelled_is_rejected(mtype):
    rejected(drop(text(mtype), "LABEL"), "LABEL is missing")


@pytest.mark.parametrize("mtype,wrong", [
    ("SIGNAL_REPORT", "PROPOSED ACTION"),
    ("TRADE_TICKET", "ANALYSIS"),
    ("RISK_ORDER", "ANALYSIS"),
    ("EXECUTION_REPORT", "PROPOSED ACTION"),
    ("ESCALATION", "OPINION"),
])
def test_wrong_label_is_rejected(mtype, wrong):
    rejected(swap(text(mtype), "LABEL", wrong), "LABEL must be")


def test_no_or_unknown_message_type_is_rejected():
    rejected(drop(text("SIGNAL_REPORT"), "MESSAGE_TYPE"), "MESSAGE_TYPE is missing")
    rejected(swap(text("SIGNAL_REPORT"), "MESSAGE_TYPE", "BUY_NOW"), "not one the design document defines")


@pytest.mark.parametrize("mtype", list(EXAMPLES))
def test_every_field_must_be_present(mtype):
    spec = SPECS[mtype]
    for f in spec.fields:
        rejected(drop(text(mtype), f.name), f"{f.name} is missing")


def test_unknown_duplicate_and_free_text_lines_are_rejected():
    src = text("SIGNAL_REPORT")
    rejected(src + "\nURGENCY: buy before it runs", "URGENCY is not a field")
    rejected(src + "\nINSTRUMENT: OTHER", "INSTRUMENT appears twice")
    rejected(src + "\nthis one is going to the moon", "is not \"FIELD: value\"")


@pytest.mark.parametrize("mtype", [m for m in EXAMPLES if m != "ESCALATION"])
def test_the_disclaimer_note_cannot_be_changed(mtype):
    rejected(swap(text(mtype), "NOTE", "Guaranteed profit."), "NOTE must read exactly")


def test_approval_scope_cannot_be_widened():
    rejected(swap(text("TRADE_TICKET"), "APPROVAL_SCOPE", "Approving authorises any entry."), "APPROVAL_SCOPE must read exactly")


def test_empty_message_is_rejected():
    rejected("", "MESSAGE_TYPE is missing")


# --- per-type rules ----------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("CROWD_STAGE", "MOONING"), ("AUTHENTICITY", "LEGIT"), ("PRIORITY", "HIGH"), ("CONFIDENCE", "very"),
])
def test_signal_report_enums(field, value):
    rejected(swap(text("SIGNAL_REPORT"), field, value), field)


def test_signal_report_tracked_trader_none_is_a_real_answer_but_blank_is_not():
    parse(swap(text("SIGNAL_REPORT"), "TRACKED_TRADER", "NONE"))
    rejected(swap(text("SIGNAL_REPORT"), "TRACKED_TRADER", ""), "TRACKED_TRADER is empty")


@pytest.mark.parametrize("field", ["REFERENCE_LEVEL", "ENTRY_ZONE", "STOP", "TARGETS", "TIME_STOP", "REWARD_RISK_AFTER_COSTS", "LIQUIDITY"])
def test_valid_setup_needs_its_levels(field):
    rejected(swap(text("SETUP_ASSESSMENT"), field, ""), f"{field} is empty")


def test_valid_setup_levels_must_make_sense():
    src = text("SETUP_ASSESSMENT")
    rejected(swap(src, "ENTRY_ZONE", "1.24 to 1.20"), "low is above high")
    rejected(swap(src, "ENTRY_ZONE", "around 1.22"), "low to high")
    rejected(swap(src, "STOP", "1.21, inside the zone"), "LONG stop must be below")
    rejected(swap(src, "STOP", "tight"), "STOP must start with a number")
    rejected(swap(src, "DIRECTION", "NONE"), "needs DIRECTION LONG or SHORT")


def test_wait_needs_a_condition_and_no_setup_needs_a_reason():
    src = swap(text("SETUP_ASSESSMENT"), "VERDICT", "WAIT")
    rejected(src, "WAIT_CONDITION is empty")
    parse(swap(src, "WAIT_CONDITION", "a pullback that holds 1.20, re-check at 15:00Z"))
    src = swap(text("SETUP_ASSESSMENT"), "VERDICT", "NO_SETUP")
    rejected(src, "REASON is empty")
    parse(swap(src, "REASON", "price is vertical with no structure for a stop"))


def test_assessment_needs_valid_until():
    rejected(swap(text("SETUP_ASSESSMENT"), "VALID_UNTIL", ""), "VALID_UNTIL is empty")


def test_draft_ticket_needs_requested_size_and_approved_ticket_needs_risk_figures():
    src = text("TRADE_TICKET")
    rejected(swap(src, "REQUESTED_SIZE", ""), "REQUESTED_SIZE is empty")
    approved = swap(src, "STATUS", "RISK_APPROVED_AWAITING_OWNER")
    e = rejected(approved, "APPROVED_SIZE is empty", "MAX_LOSS_IF_STOPPED is empty", "RISK_NOTES is empty")
    assert e.message_type == "TRADE_TICKET"
    ok = swap(swap(swap(approved, "APPROVED_SIZE", "40"), "MAX_LOSS_IF_STOPPED", "3.60, 3.6%"), "RISK_NOTES", "NONE")
    parse(ok)


def test_ticket_needs_expiry_and_a_stop():
    rejected(swap(text("TRADE_TICKET"), "EXPIRES_AT", ""), "EXPIRES_AT is empty")
    rejected(swap(text("TRADE_TICKET"), "STOP", ""), "STOP is empty")
    rejected(swap(text("TRADE_TICKET"), "DIRECTION", "NONE"), "DIRECTION")


def _all_pass():
    return "; ".join(f"{n} PASS ok" for n in range(1, 17))


def test_risk_decision_cannot_approve_with_a_failed_or_unverifiable_rule():
    src = swap(swap(text("RISK_DECISION"), "DECISION", "APPROVE"), "APPROVED_SIZE", "40")
    src = swap(src, "MAX_LOSS_IF_STOPPED", "3.60, 3.6%")
    rejected(src, "approval cannot carry a rule that did not pass")
    unverifiable = _all_pass().replace("16 PASS ok", "16 NOT_VERIFIABLE equity missing")
    rejected(swap(src, "RULE_CHECKS", unverifiable), "approval cannot carry")
    parse(swap(src, "RULE_CHECKS", _all_pass()))


def test_risk_decision_veto_rules():
    src = text("RISK_DECISION")
    rejected(swap(src, "RULE_CHECKS", _all_pass()), "VETO must name at least one rule")
    rejected(swap(src, "APPROVED_SIZE", "10"), "APPROVED_SIZE must be blank on a VETO")
    rejected(swap(src, "VETO_REASONS", ""), "VETO_REASONS is empty")


@pytest.mark.parametrize("checks", [
    "1 PASS; 2 PASS",
    _all_pass().replace("; 9 PASS ok", ""),
    _all_pass() + "; 17 PASS ok",
    _all_pass() + "; 3 PASS again",
    _all_pass().replace("4 PASS", "4 MAYBE"),
])
def test_rule_checks_must_cover_rules_1_to_16_exactly(checks):
    rejected(swap(text("RISK_DECISION"), "RULE_CHECKS", checks), "RULE_CHECKS")


def test_risk_order_is_always_urgent():
    rejected(swap(text("RISK_ORDER"), "PRIORITY", "NORMAL"), "PRIORITY")
    rejected(swap(text("RISK_ORDER"), "CARRIED_OUT_UNDER", "WHENEVER"), "CARRIED_OUT_UNDER")


def test_execution_cannot_report_an_order_with_a_failed_check():
    src = swap(text("EXECUTION_REPORT"), "STATUS", "PLACED")
    src = swap(src, "ORDERS", "limit buy 1.24 x 40, ref P-1")
    rejected(src, "cannot be PLACED with checks ['D'] failing")


def test_execution_checks_format():
    rejected(swap(text("EXECUTION_REPORT"), "CHECKS", "A PASS, B PASS, C PASS"), "CHECKS")
    rejected(swap(text("EXECUTION_REPORT"), "CHECKS", "A PASS, B PASS, C PASS, D PASS, D PASS"), "CHECKS")


def test_execution_hold_needs_a_reason_and_exit_needs_its_result():
    rejected(swap(text("EXECUTION_REPORT"), "REASON", ""), "REASON is empty")
    exited = swap(swap(text("EXECUTION_REPORT"), "STATUS", "EXITED"), "CHECKS", "A PASS, B PASS, C PASS, D PASS")
    rejected(exited, "EXIT_REASON is empty", "REALISED_RESULT is empty", "FILLS is empty")
