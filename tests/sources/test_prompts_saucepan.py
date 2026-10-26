"""Spec for aspect/saucepan_prompt_parser against a real captured saucepan chat
system prompt -- the plaintext definition saucepan's Custom API Provider sends
to a chat endpoint.

The fixture (tests/fixtures/saucepan/saucepan_chat_finley_60bdd321.json) was
captured via the "Rosetta stone" method described in
docs/SAUCEPAN_HIDDEN_CAPTURE_PLAN.md §4: export the OPEN card "Finley Brite"
(Theodrax) normally -- ground truth embedded in cards/Theodrax/Finley_60bdd321.png
-- then capture that same card's chat system prompt via the custom provider and
diff. After {{user}} reversal, personality/scenario match the ground-truth
description/scenario to the byte.
"""

import json
from pathlib import Path

import pytest

from aspect.sources.prompts.saucepan import SaucepanPromptParser, is_saucepan_prompt

FIXTURES = Path(__file__).parent.parent / "fixtures" / "saucepan"

# Ground truth, extracted from cards/Theodrax/Finley_60bdd321.png's embedded
# card JSON (spec chara_card_v3).
FINLEY_DESCRIPTION_LEN = 4491
FINLEY_SCENARIO = (
    "Finley has very bad luck. Things that can go wrong tend to go wrong for "
    "her. She is also very clumsy and disaster prone."
)


def _load_system_prompt() -> str:
    data = json.loads(
        (FIXTURES / "saucepan_chat_finley_60bdd321.json").read_text(encoding="utf-8")
    )
    for message in data["messages"]:
        if message["role"] == "system":
            return message["content"]
    raise AssertionError("fixture has no system message")


@pytest.fixture
def parser() -> SaucepanPromptParser:
    return SaucepanPromptParser()


def test_finley_hidden_definition_matches_ground_truth_after_user_reversal(parser):
    raw = _load_system_prompt()
    parsed = parser.parse(raw)

    assert parsed.name == "Finley"

    assert parsed.personality.startswith("Name: Finley Brite")
    assert parsed.personality.endswith("does not want violence to define her future.")
    assert len(parsed.personality) == FINLEY_DESCRIPTION_LEN

    assert parsed.scenario == FINLEY_SCENARIO

    assert parsed.mes_example == ""
    assert parsed.first_mes == ""
    assert parsed.raw == raw


def test_user_handle_is_reversed_to_the_user_macro(parser):
    raw = _load_system_prompt()
    parsed = parser.parse(raw)

    # The captured prompt has the real account handle substituted in twice
    # inside the Background section; both must come back as {{user}}.
    assert "EnchantedRobot" not in parsed.personality
    assert "EnchantedRobot" not in parsed.scenario
    assert parsed.personality.count("{{user}}") == 2


def test_preamble_and_user_description_are_dropped(parser):
    raw = _load_system_prompt()
    parsed = parser.parse(raw)

    # Preamble scaffolding ("You are skilled writer...") and the account's own
    # persona ([ User Description ]) are not creator content -- must not leak
    # into either field.
    assert "skilled writer" not in parsed.personality
    assert "skilled writer" not in parsed.scenario
    assert "secretive internet avatar" not in parsed.personality
    assert "secretive internet avatar" not in parsed.scenario


def test_is_saucepan_prompt_detects_the_fixture():
    assert is_saucepan_prompt(_load_system_prompt())


def test_is_saucepan_prompt_false_for_janitor_style_prompt():
    janitor_style = "<Finley's Persona>\nSome persona text\n</Finley's Persona>"
    assert not is_saucepan_prompt(janitor_style)
    assert not is_saucepan_prompt("")
    assert not is_saucepan_prompt(None)


def test_never_raises_on_missing_or_empty_input():
    parser = SaucepanPromptParser()

    empty = parser.parse("")
    assert empty.name == ""
    assert empty.personality == ""
    assert empty.scenario == ""
    assert empty.raw == ""

    none_result = parser.parse(None)
    assert none_result.name == ""
    assert none_result.raw == ""


def test_no_headers_at_all_returns_empty_fields_not_a_crash(parser):
    parsed = parser.parse("just some plain prose with no bracketed sections at all")
    assert parsed.name == ""
    assert parsed.personality == ""
    assert parsed.scenario == ""
