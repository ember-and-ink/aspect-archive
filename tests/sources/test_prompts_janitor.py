from pathlib import Path

import pytest

from aspect.sources.prompts.janitor import SystemPromptParser

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def parser() -> SystemPromptParser:
    return SystemPromptParser()


# ---------------------------------------------------------------------------
# Real captures -- hidden-definition cards. (The parser also tolerates the
# open-card system-prompt format, but open cards now take their definition
# from the JSON API, so the parser's output is only load-bearing for hidden
# cards -- which is all these fixtures cover.)
# ---------------------------------------------------------------------------


def test_lyra_hidden_card(parser):
    raw = _load("system_prompt_hidden_lyra.txt")
    parsed = parser.parse(raw)

    assert parsed.name == "Lyra"
    assert "Full Name: Lyra Amarok" in parsed.personality
    assert parsed.scenario.startswith("Important settings for Roleplay")
    assert "NEVER, under ANY circumstances" in parsed.scenario
    assert parsed.mes_example == ""
    assert parsed.raw == raw


def test_lyra_and_lyra_2_agree_on_meaningful_fields(parser):
    # lyra vs lyra_2 differ only by one trailing whitespace character on a
    # line INSIDE the persona block -- a whitespace-tolerance edge case, not
    # a meaningful diff. That one character legitimately persists in the
    # extracted `personality` text (we only strip block boundaries, not
    # collapse internal whitespace), so compare whitespace-collapsed.
    lyra = parser.parse(_load("system_prompt_hidden_lyra.txt"))
    lyra_2 = parser.parse(_load("system_prompt_hidden_lyra_2.txt"))

    def collapse(s: str) -> str:
        return " ".join(s.split())

    assert lyra.name == lyra_2.name
    assert collapse(lyra.personality) == collapse(lyra_2.personality)
    assert collapse(lyra.scenario) == collapse(lyra_2.scenario)
    assert lyra.mes_example == lyra_2.mes_example
    assert lyra.raw != lyra_2.raw


def test_ari_hidden_card(parser):
    raw = _load("system_prompt_hidden_ari.txt")
    parsed = parser.parse(raw)

    assert parsed.name == "Ari"
    assert parsed.personality.startswith("Location: USA")
    assert "About Ari:" in parsed.personality
    # <Scenario> content plus the trailing "Ravenwood Academy" world-info
    # block (outside any tag) both fold into scenario.
    assert "final-year high school student" in parsed.scenario
    assert "Ravenwood Academy" in parsed.scenario
    assert parsed.mes_example.startswith("*Ari glanced down at her phone")
    assert parsed.raw == raw


def test_aubrey_evans_hidden_card(parser):
    raw = _load("system_prompt_hidden_aubrey_evans.txt")
    parsed = parser.parse(raw)

    assert parsed.name == "Aubrey Evans"
    assert parsed.personality.startswith("Aubrey Evans")
    assert "Her nickname is Ace" in parsed.personality
    assert parsed.scenario.startswith("setting: {The Regional Championship")
    assert parsed.mes_example.startswith("USER: *As her coach")
    assert parsed.raw == raw


# ---------------------------------------------------------------------------
# Graceful degradation on malformed / format-drifted input -- never throws,
# missing fields are just "". No real capture exhibits total tag loss, so
# these are synthetic worst-case fixtures.
# ---------------------------------------------------------------------------


def test_empty_prompt_never_throws(parser):
    parsed = parser.parse("")
    assert parsed.name == ""
    assert parsed.personality == ""
    assert parsed.scenario == ""
    assert parsed.mes_example == ""
    assert parsed.raw == ""


def test_none_prompt_never_throws(parser):
    parsed = parser.parse(None)
    assert parsed.name == ""
    assert parsed.raw == ""


def test_prompt_with_no_recognized_tags_is_untagged_prose(parser):
    raw = "Just some plain prose with no tags at all, JanitorAI format has drifted."
    parsed = parser.parse(raw)

    assert parsed.name == ""
    assert parsed.personality == ""
    assert parsed.scenario == ""
    assert parsed.mes_example == ""
    assert parsed.raw == raw


def test_unclosed_character_tag_takes_content_through_end(parser):
    raw = "<Foo's Persona>bar that never closes, format drifted mid-capture"
    parsed = parser.parse(raw)

    assert parsed.name == "Foo"
    assert parsed.personality == "bar that never closes, format drifted mid-capture"


def test_scenario_nested_inside_persona_is_not_duplicated(parser):
    raw = (
        "<X's Persona>before <Scenario>nested scenario text</Scenario> "
        "after</X's Persona>"
    )
    parsed = parser.parse(raw)

    assert parsed.name == "X"
    assert "nested scenario text" in parsed.personality
    # Not pulled out as a second, top-level scenario block.
    assert parsed.scenario == ""


def test_literal_backslash_n_is_converted_to_real_newlines(parser):
    raw = "<Y's Persona>line one\\nline two</Y's Persona>"
    parsed = parser.parse(raw)

    assert parsed.personality == "line one\nline two"


# ---------------------------------------------------------------------------
# chat_names containing '/' -- e.g. "Celeste // Brightstar". JanitorAI wraps
# the definition in `<{chat_name}'s Persona>...`, so the persona tag name
# itself carries the slash. The parser's name MUST come back equal to the
# chat_name, because CaptureStore keys the record by it and /build-jai resolves
# the capture by chat_name -- a mismatch silently drops the hidden export.
# ---------------------------------------------------------------------------


def test_slash_in_name_is_parsed_from_persona_tag(parser):
    # Mirrors the real "Celeste // Brightstar" capture: the persona tag name
    # holds the slash, and JanitorAI nests an inner self-name tag inside it.
    # The parser must return the full slash name, not latch onto the inner
    # <Celeste> tag (which used to key the capture under "Celeste").
    raw = (
        "\n<Celeste // Brightstar's Persona><Celeste> "
        "Full Name: Celeste Yoon</Celeste>\n\n[AI Guidelines]\n- be explicit "
        "</System></Celeste // Brightstar's Persona>\n"
        "<Scenario>Metro City at dusk.</Scenario>\n"
    )
    parsed = parser.parse(raw)

    assert parsed.name == "Celeste // Brightstar"
    assert "Full Name: Celeste Yoon" in parsed.personality
    # The full persona block is captured, including the guidelines that follow
    # the inner tag (previously dropped when the inner <Celeste> tag was used).
    assert "AI Guidelines" in parsed.personality
    assert parsed.scenario == "Metro City at dusk."


def test_slash_name_key_matches_chat_name_end_to_end(parser):
    # The whole point of the fix: normalize(parsed.name) must equal the key a
    # /build-jai derives from chat_name, or capture_store.get() returns None.
    from aspect.state.captures import normalize

    raw = "<A / B's Persona>body</A / B's Persona>"
    parsed = parser.parse(raw)

    assert parsed.name == "A / B"
    assert normalize(parsed.name) == normalize("A / B")


def test_closing_tag_with_slash_is_not_matched_as_the_name(parser):
    # Guards the `(?!/)` lookahead: allowing '/' inside a name must not let a
    # closing tag be picked up as an opening/name tag.
    raw = "</just a closing tag>\n<Z's Persona>real body</Z's Persona>"
    parsed = parser.parse(raw)

    assert parsed.name == "Z"
    assert parsed.personality == "real body"


def test_whitespace_inside_the_tag_brackets_is_not_part_of_the_name(parser):
    # The name is trimmed by the caller now rather than inside the pattern
    # (which backtracked quadratically on `<` + a long run of spaces).
    parsed = parser.parse("<  Mira's Persona  >She is kind.</Mira's Persona>")

    assert parsed.name == "Mira"
    assert parsed.personality == "She is kind."
