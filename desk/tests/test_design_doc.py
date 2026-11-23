"""The code must agree with docs/team-design.md, word for word where it copies it."""

import re

from desk import DESIGN_DOC, PROMPTS_DIR
from desk.config import placeholders
from desk.params import AGENT_BY_SHORT, AGENTS, PARAM_BY_NAME, PARAMS

DOC = DESIGN_DOC.read_text(encoding="utf-8")


def _doc_param_rows():
    rows = {}
    for m in re.finditer(r"^\| `\[([A-Z_]+)\]` \| (.*?) \| (.*?) \|$", DOC, flags=re.M):
        rows[m.group(1)] = (m.group(2), m.group(3))
    return rows


def _doc_prompts():
    return re.findall(r"```text\n(.*?)\n```", DOC, flags=re.S)


def test_doc_defines_32_parameters_and_code_has_the_same_32_in_order():
    rows = _doc_param_rows()
    assert len(rows) == 32
    assert list(rows) == [p.name for p in PARAMS]


def test_parameter_descriptions_match_the_doc_word_for_word():
    for name, (sets, _) in _doc_param_rows().items():
        assert PARAM_BY_NAME[name].sets == sets, name


def test_used_by_matches_the_doc():
    for name, (_, used) in _doc_param_rows().items():
        expected = {a.key for a in AGENTS} if used == "All five" else {AGENT_BY_SHORT[s.strip()].key for s in used.split(",")}
        assert set(PARAM_BY_NAME[name].used_by) == expected, name


def test_prompt_files_are_the_doc_prompts_word_for_word():
    blocks = _doc_prompts()
    assert len(blocks) == 5
    for agent, block in zip(AGENTS, blocks):
        text = (PROMPTS_DIR / f"{agent.key}.md").read_text(encoding="utf-8")
        assert text == block + "\n", agent.key
        assert text.startswith(f"You are the {agent.name} of a five-agent trading desk"), agent.key


def test_each_prompt_uses_exactly_the_parameters_the_doc_says_it_uses():
    for agent in AGENTS:
        used = set(placeholders((PROMPTS_DIR / f"{agent.key}.md").read_text(encoding="utf-8")))
        expected = {p.name for p in PARAMS if agent.key in p.used_by}
        assert used == expected, agent.key


def test_limit_numbers_match_the_risk_officer_prompt():
    risk = (PROMPTS_DIR / "risk-officer.md").read_text(encoding="utf-8")
    found = {}
    for m in re.finditer(r"^(L\d+)\s+.*$", risk, flags=re.M):
        for name in placeholders(m.group(0)):
            found[name] = m.group(1)
    assert sorted(set(found.values()), key=lambda s: int(s[1:])) == [f"L{i}" for i in range(1, 16)]
    assert found == {p.name: p.limit for p in PARAMS if p.limit}


def test_no_prompt_mentions_a_placeholder_the_doc_does_not_define():
    for agent in AGENTS:
        text = (PROMPTS_DIR / f"{agent.key}.md").read_text(encoding="utf-8")
        assert all(n in PARAM_BY_NAME for n in placeholders(text))
