"""The risk engine: a test for every rule, for missing data and for empty limits.

All numbers here are test values chosen to exercise the code. None of them is
a suggested setting, and none is written to config/desk.yaml.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml

from desk import CONFIG_PATH, config, risk
from desk.risk import FAIL, NA, PASS, AccountState, InstrumentEvent, LivePrice, Position, RiskRequest
from desk.schemas import parse
from desk.tests.test_config import SAMPLE

FIX = Path(__file__).parent / "fixtures" / "messages"
NOW = datetime(2026, 11, 23, 13, 10, tzinfo=timezone.utc)

TEST_LIMITS = dict(
    SAMPLE,
    FEE_MODEL="0.1% per side, minimum 0",
    TRADING_DAY="00:00 UTC",
    MIN_LIQUIDITY="100000 USD 24h volume",
    MIN_REWARD_RISK=1.2,
    ACCOUNT_EQUITY="paper ledger",
)


def cfg_with(tmp_path, **changes):
    p = tmp_path / "desk.yaml"
    p.write_text(yaml.safe_dump(dict(TEST_LIMITS, **changes)), encoding="utf-8")
    return config.load(p)


@pytest.fixture()
def limits(tmp_path):
    return risk.limits_from_config(cfg_with(tmp_path))


def msg(name, **swaps):
    text = (FIX / name).read_text(encoding="utf-8")
    for field, value in swaps.items():
        lines = text.splitlines()
        hits = [i for i, ln in enumerate(lines) if ln.startswith(field + ":")]
        assert len(hits) == 1, field
        lines[hits[0]] = f"{field}: {value}".rstrip()
        text = "\n".join(lines)
    return parse(text)


def account(**kw):
    base = dict(
        equity=D(1000), peak_equity=D(1000), day_start_equity=D(1000), day_realised=D(0),
        open_positions=(), entries_today=0, events=(), halted=False, as_of=NOW,
    )
    base.update(kw)
    return AccountState(**base)


def live(**kw):
    base = dict(instrument="TESTCOIN", price=D("1.22"), as_of=NOW, liquidity=D(2_500_000), expected_slippage_pct=D("0.1"), lot_size=D("0.01"))
    base.update(kw)
    return LivePrice(**base)


def request(ticket=None, signal=None, assessment=None, acct=None, price=None, **kw):
    return RiskRequest(
        ticket=ticket or msg("trade_ticket.msg"),
        signal=signal or msg("signal_report.msg"),
        assessment=assessment or msg("setup_assessment.msg"),
        account=acct or account(),
        live=price or live(),
        now=kw.pop("now", NOW),
        **kw,
    )


def run(limits, **kw):
    return risk.evaluate(request(**kw), limits)


def status(result, rule):
    return result.checks[rule].status


def assert_veto(result, rule, expected=FAIL, text=None):
    assert result.decision == risk.VETO
    assert result.approved_units is None
    assert status(result, rule) == expected, result.checks[rule]
    if text:
        assert text in result.checks[rule].figure, result.checks[rule].figure


def pos(**kw):
    base = dict(ticket_id="T-OLD", instrument="OTHERCOIN", direction="LONG", units=D(10), entry_price=D(10), stop=D(9), current_price=D(10))
    base.update(kw)
    return Position(**base)


# --- the happy path, and the shape of every result --------------------------------------

def test_clean_ticket_is_approved_with_every_rule_passing(limits):
    r = run(limits)
    assert r.decision == risk.APPROVE
    assert r.approved_units == D(50)
    assert sorted(r.checks) == list(range(1, 17))
    assert all(c.status == PASS for c in r.checks.values()), [c for c in r.checks.values() if c.status != PASS]
    assert D(0) < r.max_loss <= D(10)  # L2: 1% of 1000, costs included


def test_every_result_has_all_16_rules_even_when_inputs_are_missing(limits):
    r = risk.evaluate(request(acct=None) if False else RiskRequest(ticket=msg("trade_ticket.msg"), signal=None, assessment=None, account=None, live=None, now=NOW), limits)
    assert sorted(r.checks) == list(range(1, 17))
    assert r.decision == risk.VETO


@pytest.mark.parametrize("variant", ["approve", "reduced", "veto"])
def test_result_renders_as_a_valid_risk_decision_message(limits, variant):
    if variant == "approve":
        r = run(limits)
    elif variant == "reduced":
        r = run(limits, ticket=msg("trade_ticket.msg", REQUESTED_SIZE="500"))
    else:
        r = run(limits, price=live(price=D("1.30")))
    m = parse(r.to_message_text())
    assert m["DECISION"] == r.decision
    assert {n: s for n, (s, _) in m["RULE_CHECKS"].items()} == {n: c.status for n, c in r.checks.items()}


def test_deterministic(limits):
    a, b = run(limits), run(limits)
    assert a.to_message_text() == b.to_message_text()


# --- Rule 1: size -----------------------------------------------------------------------

def test_rule1_never_more_than_requested(limits):
    r = run(limits, ticket=msg("trade_ticket.msg", REQUESTED_SIZE="5"))
    assert r.decision == risk.APPROVE and r.approved_units == D(5)


def test_rule1_reduced_to_the_smallest_cap_and_rounded_down(limits):
    r = run(limits, ticket=msg("trade_ticket.msg", REQUESTED_SIZE="500"))
    assert r.decision == risk.APPROVE_REDUCED
    # L2: loss if stopped stays within 1% of equity, costs included
    assert r.max_loss <= D(10)
    assert r.approved_units == r.approved_units.quantize(D("0.01"))
    assert "requested size" in " ".join(r.discipline_flags)


def test_rule1_position_cap_L1(tmp_path):
    lim = risk.limits_from_config(cfg_with(tmp_path, MAX_POSITION_PCT=2))  # 20 of 1000 at worst entry 1.24
    r = run(lim)
    assert r.decision == risk.APPROVE_REDUCED
    assert r.approved_units * D("1.24") <= D(20)
    assert "binding: L1" in r.checks[1].figure


def test_rule1_exposure_cap_L3(tmp_path):
    lim = risk.limits_from_config(cfg_with(tmp_path, MAX_TOTAL_EXPOSURE_PCT=11, MAX_POSITION_PCT=100))
    r = run(lim, acct=account(open_positions=(pos(),)), related_ticket_ids=())  # 100 already open, cap 110
    assert r.approved_units * D("1.24") <= D(10)
    assert "binding: L3" in r.checks[1].figure


def test_rule1_veto_when_no_lot_fits(tmp_path):
    lim = risk.limits_from_config(cfg_with(tmp_path, MAX_POSITION_PCT=0))
    assert_veto(run(lim), 1)


def test_rule1_lot_size_rounds_down(limits):
    r = run(limits, ticket=msg("trade_ticket.msg", REQUESTED_SIZE="500"), price=live(lot_size=D(7)))
    assert r.approved_units % 7 == 0


# --- Rule 2: no stop, no trade ------------------------------------------------------

def test_rule2_stop_on_the_wrong_side_is_vetoed(limits):
    t = msg("trade_ticket.msg")
    a = msg("setup_assessment.msg")
    t.values["STOP"] = a.values["STOP"] = D("1.30")  # past the schema on purpose: the engine checks again
    r = run(limits, ticket=t, assessment=a)
    assert r.decision == risk.VETO and status(r, 2) == FAIL


# --- Rule 3: never add to a loser --------------------------------------------------------

def test_rule3_adding_to_a_losing_position_is_vetoed(limits):
    loser = pos(instrument="TESTCOIN", units=D(5), entry_price=D("1.30"), stop=D("1.10"), current_price=D("1.22"))
    assert_veto(run(limits, acct=account(open_positions=(loser,))), 3, text="losing")


def test_rule3_adding_to_a_winner_is_checked_as_one_combined_position(tmp_path):
    lim = risk.limits_from_config(cfg_with(tmp_path, MAX_POSITION_PCT=8))
    winner = pos(instrument="TESTCOIN", units=D(40), entry_price=D("1.10"), stop=D("1.15"), current_price=D("1.22"))
    r = run(lim, acct=account(open_positions=(winner,)))
    assert status(r, 3) == PASS
    assert (r.approved_units or 0) * D("1.24") + D(40) * D("1.22") <= D(80)


def test_rule3_opposite_direction_is_vetoed(limits):
    short = pos(instrument="TESTCOIN", direction="SHORT", units=D(5), entry_price=D("1.30"), stop=D("1.40"), current_price=D("1.22"))
    assert_veto(run(limits, acct=account(open_positions=(short,))), 3)


# --- Rule 4: stops only tighten ----------------------------------------------------------

@pytest.mark.parametrize("direction,old,new,current,expected", [
    ("LONG", "1.12", "1.15", "1.22", PASS),
    ("LONG", "1.12", "1.10", "1.22", FAIL),
    ("LONG", "1.12", "1.25", "1.22", FAIL),
    ("SHORT", "1.40", "1.35", "1.22", PASS),
    ("SHORT", "1.40", "1.45", "1.22", FAIL),
    ("LONG", "1.12", "1.12", "1.22", PASS),
])
def test_rule4_stop_changes(direction, old, new, current, expected):
    p = pos(direction=direction, stop=D(old), current_price=D(current), entry_price=D("1.22"))
    assert risk.check_stop_change(p, D(new)).status == expected


def test_rule4_removing_a_stop_fails():
    assert risk.check_stop_change(pos(), None).status == FAIL


# --- Rule 5: leverage and position count ---------------------------------------------------

def test_rule5_leverage_above_L5(limits):
    assert_veto(run(limits, leverage=D(2)), 5, text="leverage")


def test_rule5_open_positions_at_L4(limits):
    full = tuple(pos(ticket_id=f"T{i}", instrument=f"C{i}", units=D(1), current_price=D(1), entry_price=D(1), stop=D("0.99")) for i in range(3))
    assert_veto(run(limits, acct=account(open_positions=full), related_ticket_ids=()), 5, text="L4")


# --- Rule 6: related positions count as one ----------------------------------------------------

def test_rule6_undeclared_positions_are_all_counted_as_related(tmp_path):
    lim = risk.limits_from_config(cfg_with(tmp_path, MAX_POSITION_PCT=10))
    other = pos(units=D(9), current_price=D(10))  # 90 of the 100 L1 allows
    counted = run(lim, acct=account(open_positions=(other,)))
    declared = run(lim, acct=account(open_positions=(other,)), related_ticket_ids=())
    assert counted.approved_units < declared.approved_units
    assert "counted as related" in counted.checks[6].figure


def test_rule6_declared_related_position_shares_L1(tmp_path):
    lim = risk.limits_from_config(cfg_with(tmp_path, MAX_POSITION_PCT=10))
    other = pos(ticket_id="T-SAME-STORY", units=D(9), current_price=D(10))
    r = run(lim, acct=account(open_positions=(other,)), related_ticket_ids=("T-SAME-STORY",))
    assert r.approved_units * D("1.24") <= D(10)


# --- Rule 7: chase ---------------------------------------------------------------------------

@pytest.mark.parametrize("price", ["1.19", "1.25", "1.31"])
def test_rule7_live_price_outside_the_zone(limits, price):
    assert_veto(run(limits, price=live(price=D(price))), 7, text="outside entry zone")


def test_rule7_inside_zone_but_beyond_the_chase_limit(tmp_path):
    lim = risk.limits_from_config(cfg_with(tmp_path, MAX_CHASE_PCT=1))  # 1.23 is 2.5% from 1.20
    assert_veto(run(lim, price=live(price=D("1.23"))), 7, text="L6")


# --- Rule 8: age ----------------------------------------------------------------------------------

def test_rule8_signal_older_than_L7(limits):
    assert_veto(run(limits, signal=msg("signal_report.msg", DATA_AS_OF="2026-11-23T12:30:00Z")), 8, text="signal")


def test_rule8_assessment_past_valid_until(limits):
    a = msg("setup_assessment.msg", VALID_UNTIL="2026-11-23T13:08:00Z")
    assert_veto(run(limits, assessment=a), 8, text="expired")


# --- Rule 9: crowd stage ------------------------------------------------------------------------------

@pytest.mark.parametrize("stage", ["CROWDED", "EXHAUSTION", "UNCLEAR"])
def test_rule9_long_into_a_late_crowd_is_vetoed(limits, stage):
    r = run(limits, signal=msg("signal_report.msg", CROWD_STAGE=stage), ticket=msg("trade_ticket.msg", CROWD_STAGE=stage))
    assert_veto(r, 9, text=stage)


@pytest.mark.parametrize("stage", ["EARLY", "BUILDING"])
def test_rule9_early_and_building_pass(limits, stage):
    r = run(limits, signal=msg("signal_report.msg", CROWD_STAGE=stage), ticket=msg("trade_ticket.msg", CROWD_STAGE=stage))
    assert status(r, 9) == PASS


# --- Rule 10: authenticity -----------------------------------------------------------------------------

def test_rule10_coordinated_pump_is_always_vetoed(limits):
    r = run(limits, signal=msg("signal_report.msg", AUTHENTICITY="COORDINATED_PUMP"), ticket=msg("trade_ticket.msg", AUTHENTICITY="COORDINATED_PUMP"))
    assert_veto(r, 10)


@pytest.mark.parametrize("auth", ["PROMOTED", "UNKNOWN"])
def test_rule10_promoted_or_unknown_is_noted_for_the_owner(limits, auth):
    r = run(limits, signal=msg("signal_report.msg", AUTHENTICITY=auth), ticket=msg("trade_ticket.msg", AUTHENTICITY=auth))
    assert status(r, 10) == PASS
    assert any(auth in n for n in r.notes)


# --- Rule 11: cooldowns ------------------------------------------------------------------------------

@pytest.mark.parametrize("event", ["EXITED", "MISSED", "DECLINED", "VETOED"])
def test_rule11_reentry_cooldown(limits, event):
    e = InstrumentEvent("TESTCOIN", event, NOW - timedelta(hours=3))  # L15 is 1 day in the test limits
    assert_veto(run(limits, acct=account(events=(e,))), 11, text="L15")


def test_rule11_reentry_cooldown_ends(limits):
    e = InstrumentEvent("TESTCOIN", "EXITED", NOW - timedelta(days=2))
    assert status(run(limits, acct=account(events=(e,))), 11) == PASS


def test_rule11_cooldown_after_any_losing_exit(limits):
    e = InstrumentEvent("OTHERCOIN", "EXITED", NOW - timedelta(minutes=30), losing=True)  # L14 is 2 hours
    assert_veto(run(limits, acct=account(events=(e,))), 11, text="L14")


def test_rule11_winning_exit_elsewhere_does_not_block(limits):
    e = InstrumentEvent("OTHERCOIN", "EXITED", NOW - timedelta(minutes=30), losing=False)
    assert status(run(limits, acct=account(events=(e,))), 11) == PASS


# --- Rule 12: entries per day ---------------------------------------------------------------------

def test_rule12_entries_reached_L13(limits):
    assert_veto(run(limits, acct=account(entries_today=2)), 12)


# --- Rule 13: reward-to-risk, cost, liquidity -----------------------------------------------------

def test_rule13_reward_to_risk_below_L8(tmp_path):
    lim = risk.limits_from_config(cfg_with(tmp_path, MIN_REWARD_RISK=3))
    assert_veto(run(lim), 13, text="reward-to-risk")


def test_rule13_round_trip_cost_above_L9(tmp_path):
    lim = risk.limits_from_config(cfg_with(tmp_path, FEE_MODEL="0.1% per side, minimum 5"))
    assert_veto(run(lim), 13, text="round-trip cost")


def test_rule13_liquidity_below_L10(limits):
    assert_veto(run(limits, price=live(liquidity=D(50_000))), 13, text="liquidity")


# --- Rule 14: the day's loss -----------------------------------------------------------------------

def test_rule14_daily_loss_reached_vetoes_every_entry(limits):
    assert_veto(run(limits, acct=account(day_realised=D(-30), equity=D(970))), 14, text="L11")


def test_rule14_open_losses_count_toward_the_day(limits):
    losing = pos(units=D(10), entry_price=D(10), current_price=D(7), stop=D(6))  # -30 open
    assert_veto(run(limits, acct=account(open_positions=(losing,), equity=D(970)), related_ticket_ids=()), 14)


def test_rule14_size_cut_so_a_stop_out_cannot_pass_L11(limits):
    r = run(limits, acct=account(day_realised=D(-27), equity=D(973)), ticket=msg("trade_ticket.msg", REQUESTED_SIZE="500"))
    assert r.decision == risk.APPROVE_REDUCED
    assert r.max_loss <= D(3)
    assert "binding: L11" in r.checks[1].figure


# --- Rule 15: drawdown and halt ------------------------------------------------------------------------

def test_rule15_drawdown_reached_issues_a_halt(limits):
    r = run(limits, acct=account(equity=D(900), peak_equity=D(1000), day_start_equity=D(900)))
    assert_veto(r, 15, text="HALT")
    assert r.halt


def test_rule15_halt_stays_until_the_owner_re_enables(limits):
    r = run(limits, acct=account(halted=True))  # equity fully recovered, still halted
    assert_veto(r, 15, text="Owner re-enables")


def test_rule15_size_cut_so_a_stop_out_cannot_pass_L12(limits):
    r = run(limits, acct=account(equity=D(905), peak_equity=D(1000), day_start_equity=D(905)), ticket=msg("trade_ticket.msg", REQUESTED_SIZE="500"))
    assert r.decision == risk.APPROVE_REDUCED
    assert r.max_loss <= D(5)


# --- Rule 16: fail closed ----------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["signal", "assessment", "account", "live"])
def test_rule16_missing_input_vetoes(limits, missing):
    req = request()
    req = replace(req, **{missing: None})
    r = risk.evaluate(req, limits)
    assert_veto(r, 16, NA, text="missing")


def test_rule16_stale_live_price(limits):
    assert_veto(run(limits, price=live(as_of=NOW - timedelta(hours=1))), 16, NA, text="stale")


def test_rule16_future_dated_account(limits):
    assert_veto(run(limits, acct=account(as_of=NOW + timedelta(minutes=5))), 16, NA, text="future")


@pytest.mark.parametrize("field", ["liquidity", "expected_slippage_pct", "lot_size"])
def test_rule16_missing_market_figures(limits, field):
    assert_veto(run(limits, price=live(**{field: None})), 16, NA)


def test_rule16_no_equity(limits):
    assert_veto(run(limits, acct=account(equity=D(0), peak_equity=D(0), day_start_equity=D(0))), 16, NA, text="equity")


def test_rule16_ticket_levels_must_match_the_assessment(limits):
    assert_veto(run(limits, ticket=msg("trade_ticket.msg", STOP="1.10, wider")), 16, NA, text="STOP differs")


def test_rule16_reports_must_be_for_the_same_candidate(limits):
    assert_veto(run(limits, signal=msg("signal_report.msg", CANDIDATE_ID="C-OTHER")), 16, NA)


def test_rule16_assessment_must_be_a_valid_setup(limits):
    a = msg("setup_assessment.msg", VERDICT="WAIT", WAIT_CONDITION="a pullback to 1.20")
    assert_veto(run(limits, assessment=a), 16, NA, text="WAIT")


def test_rule16_live_price_for_another_instrument(limits):
    assert_veto(run(limits, price=live(instrument="OTHERCOIN")), 16, NA)


def test_rule16_ticket_must_be_a_draft(limits):
    t = msg("trade_ticket.msg", STATUS="OWNER_APPROVED", APPROVED_SIZE="50", MAX_LOSS_IF_STOPPED="6", RISK_NOTES="NONE")
    assert_veto(run(limits, ticket=t), 16, NA)


# --- empty parameters fail closed ------------------------------------------------------------------

def test_committed_empty_config_vetoes_every_ticket_and_names_the_gaps():
    r = risk.evaluate_with_config(request(), config.load(CONFIG_PATH))
    assert_veto(r, 16, NA, text="limits unset")
    for name in risk.RISK_PARAMS:
        assert name in r.checks[16].figure
    parse(r.to_message_text())


@pytest.mark.parametrize("name", risk.RISK_PARAMS)
def test_any_single_empty_risk_parameter_vetoes(tmp_path, name):
    r = risk.evaluate_with_config(request(), cfg_with(tmp_path, **{name: None}))
    assert_veto(r, 16, NA, text=name)


@pytest.mark.parametrize("name,value", [
    ("FEE_MODEL", "cheap"),
    ("FEE_MODEL", "0.1%"),
    ("TRADING_DAY", "midnight"),
    ("TRADING_DAY", "25:00 UTC"),
    ("TRADING_DAY", "00:00 Mars/Olympus"),
    ("MIN_LIQUIDITY", "plenty"),
])
def test_unreadable_risk_parameter_vetoes(tmp_path, name, value):
    r = risk.evaluate_with_config(request(), cfg_with(tmp_path, **{name: value}))
    assert_veto(r, 16, NA, text=name)


def test_limits_come_only_from_config(tmp_path):
    # The public entry point takes no limits from the caller.
    import inspect
    assert list(inspect.signature(risk.evaluate_with_config).parameters) == ["request", "cfg"]


# --- discipline watch ---------------------------------------------------------------------------------

def test_discipline_urgency_and_thin_thesis(limits):
    r = run(limits, ticket=msg("trade_ticket.msg", THESIS="Last chance, it is going up."))
    assert "urgency wording in the ticket" in r.discipline_flags
    assert any("going up" in f for f in r.discipline_flags)


def test_discipline_resubmitted_veto(limits):
    t = msg("trade_ticket.msg")
    prior = {f: t.raw[f] for f in ("INSTRUMENT", "DIRECTION", "ENTRY_ZONE", "STOP", "TARGETS")}
    r = run(limits, acct=account(vetoed_tickets=(prior,)))
    assert "a vetoed ticket resubmitted with no change in its terms" in r.discipline_flags


def test_trading_day_start():
    td = risk.parse_trading_day("09:30 America/New_York")
    start = td.start_of_day(datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc))  # 08:00 New York
    assert start.hour == 9 and start.day == 22
