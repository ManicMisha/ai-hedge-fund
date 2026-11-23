"""Configuration: empty means refuse, wrong shape means refuse, nothing is invented."""

from datetime import timedelta

import pytest
import yaml

from desk import CONFIG_PATH, config
from desk.params import AGENTS, PARAM_BY_NAME, PARAM_NAMES

# Placeholder test values. They exist only to exercise the code and are never
# written to config/desk.yaml.
SAMPLE = {
    "ASSET_CLASS": "test asset",
    "VENUE": "test venue",
    "INSTRUMENT_UNIVERSE": ["AAA", "BBB"],
    "TIMEFRAME": "test timeframe",
    "ALLOWED_DIRECTIONS": "LONG_ONLY",
    "TRADING_DAY": "test day",
    "SOCIAL_SOURCES": "test source",
    "TRACKED_TRADER_SOURCES": "NONE",
    "MAX_SIGNAL_AGE": "15 minutes",
    "MIN_LIQUIDITY": "test liquidity",
    "MIN_REWARD_RISK": 2,
    "FEE_MODEL": "test fees",
    "ACCOUNT_EQUITY": "test equity",
    "MAX_POSITION_PCT": 10,
    "MAX_RISK_PER_TRADE_PCT": 1,
    "MAX_TOTAL_EXPOSURE_PCT": 30,
    "MAX_OPEN_POSITIONS": 3,
    "MAX_LEVERAGE": 1,
    "MAX_CHASE_PCT": 2.5,
    "MAX_COST_PCT": 1,
    "MAX_DAILY_LOSS_PCT": 3,
    "MAX_DRAWDOWN_PCT": 10,
    "MAX_TRADES_PER_DAY": 2,
    "COOLDOWN_AFTER_LOSS": "2 hours",
    "REENTRY_COOLDOWN": "1 day",
    "LIMIT_CHANGE_COOLDOWN": "24 hours",
    "APPROVAL_MODE": "MANUAL",
    "RISK_EXIT_MODE": "ASK_OWNER",
    "TICKET_EXPIRY": "30 minutes",
    "MAX_SLIPPAGE_PCT": 0.5,
    "OWNER_CHANNEL": "test channel",
    "SUMMARY_CADENCE": "test cadence",
}


def write(tmp_path, data):
    p = tmp_path / "desk.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_sample_covers_every_parameter():
    assert set(SAMPLE) == set(PARAM_NAMES)


def test_committed_config_lists_all_32_and_every_one_is_empty():
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert len(raw) == 32 and set(raw) == set(PARAM_NAMES)  # grouped for reading, so not in doc order
    assert all(v is None for v in raw.values())
    cfg = config.load(CONFIG_PATH)
    assert cfg.filled_count == 0 and cfg.missing == list(PARAM_NAMES)


def test_empty_config_refuses_to_render_and_names_all_32(tmp_path):
    cfg = config.load(write(tmp_path, {n: None for n in PARAM_NAMES}))
    with pytest.raises(config.MissingConfig) as exc:
        config.render_all(cfg)
    assert exc.value.missing == list(PARAM_NAMES)
    for name in PARAM_NAMES:
        assert name in str(exc.value)


def test_missing_keys_count_as_empty(tmp_path):
    cfg = config.load(write(tmp_path, {}))
    assert cfg.missing == list(PARAM_NAMES)


@pytest.mark.parametrize("name", PARAM_NAMES)
def test_one_empty_value_blocks_the_whole_render_and_is_named(tmp_path, name):
    data = dict(SAMPLE, **{name: ""})
    cfg = config.load(write(tmp_path, data))
    with pytest.raises(config.MissingConfig) as exc:
        config.render_all(cfg)
    assert exc.value.missing == [name]


@pytest.mark.parametrize("name", PARAM_NAMES)
def test_agent_render_needs_only_its_own_values(tmp_path, name):
    cfg = config.load(write(tmp_path, dict(SAMPLE, **{name: None})))
    for agent in AGENTS:
        if agent.key in PARAM_BY_NAME[name].used_by:
            with pytest.raises(config.MissingConfig):
                config.render(agent.key, cfg)
        else:
            assert "[" + name + "]" not in config.render(agent.key, cfg)


def test_full_config_renders_every_prompt_with_no_placeholder_left(tmp_path):
    cfg = config.load(write(tmp_path, SAMPLE))
    assert cfg.complete
    out = config.render_all(cfg)
    assert set(out) == {a.key for a in AGENTS}
    for text in out.values():
        assert config.placeholders(text) == []
    assert "Tradable instruments: AAA, BBB" in out["desk-lead"]
    assert "L6  Chase limit: 2.5 percent" in out["risk-officer"]


@pytest.mark.parametrize(
    "name,bad,why",
    [
        ("MAX_POSITION_PCT", "10%", "% sign"),
        ("MAX_POSITION_PCT", "ten", "plain number"),
        ("MAX_POSITION_PCT", -1, "at least 0"),
        ("MAX_POSITION_PCT", True, "plain number"),
        ("MAX_OPEN_POSITIONS", 2.5, "whole number"),
        ("MAX_LEVERAGE", 0.5, "at least 1"),
        ("MAX_SIGNAL_AGE", "15", "unit"),
        ("MAX_SIGNAL_AGE", 15, "unit"),
        ("TICKET_EXPIRY", "soon", "unit"),
        ("RISK_EXIT_MODE", "YES", "one of"),
        ("ALLOWED_DIRECTIONS", "long", "one of"),
        ("VENUE", "[VENUE]", "placeholder"),
        ("ASSET_CLASS", {"a": 1}, "words"),
    ],
)
def test_invalid_values_are_reported_not_corrected(tmp_path, name, bad, why):
    cfg = config.load(write(tmp_path, dict(SAMPLE, **{name: bad})))
    state = cfg.states[name]
    assert state.status == "invalid"
    assert why in state.problem
    with pytest.raises(config.MissingConfig) as exc:
        config.render_all(cfg)
    assert name in exc.value.invalid


def test_durations_parse():
    assert config.parse_duration("15 minutes") == timedelta(minutes=15)
    assert config.parse_duration("2h") == timedelta(hours=2)
    assert config.parse_duration("1 week") == timedelta(weeks=1)


def test_unknown_name_is_an_error_not_ignored(tmp_path):
    with pytest.raises(config.ConfigError, match="MAX_POSITON_PCT"):
        config.load(write(tmp_path, {"MAX_POSITON_PCT": 5}))


def test_missing_file_and_bad_yaml_are_errors(tmp_path):
    with pytest.raises(config.ConfigError):
        config.load(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("A: [unclosed", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(bad)
