"""JannyAI import mapping, validated against three real jannyai.com card exports
(tests/fixtures/jannyai/*.json -- the base64-decoded embedded card objects):

  * abby       -- @MathiDoos, single greeting, no lorebook
  * airi       -- @MathiDoos, dialogue-style first_mes
  * alexandra  -- @Kasper376, parenthesised name, slug with hyphens

JannyAI is a structural twin of datacat (definition-only, macros intact, HTML
creator_notes), so the mapping mirrors sources.datacat's; the tests mirror
test_datacat_mapper's.
"""

import json
from pathlib import Path

from aspect.sources import jannyai as mapper
from aspect.cards.builder import CardBuilder

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jannyai"


def load_data(name: str) -> dict:
    """The embedded card's `data` object."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["data"]


# ---------------------------------------------------------------------------
# to_profile_fields -- JannyAI puts the whole definition in `description`
# ---------------------------------------------------------------------------


def test_profile_fields_map_definition():
    fields = mapper.to_profile_fields(load_data("abby"))
    assert fields.name == "Abby"
    assert fields.creator == "MathiDoos"  # "@" sigil stripped
    assert fields.description
    assert "{{" in fields.scenario  # macros are intact
    assert "Female" in fields.tags


def test_tags_normalized_through_the_shared_intake_pipeline():
    # JannyAI's own to_profile_fields does no tag cleaning at all -- CardBuilder
    # is the shared choke point that catches #/emoji/whitespace/case-dupes for
    # this source. See docs/PHASE_5_TAGS_PLAN.md §2/§6 step 3.
    data = load_data("abby")
    data["tags"] = ["#wildwest", "👤 outlaw", "  slow   burn  ", "Femdom", "femdom"]
    profile = mapper.to_profile_fields(data)
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)
    assert card.tags == ["wildwest", "outlaw", "slow burn", "Femdom"]


def test_creator_notes_are_dehtmled():
    data = load_data("abby")
    assert "<" in data["creator_notes"]  # raw JannyAI HTML in the source
    fields = mapper.to_profile_fields(data)
    assert fields.creator_notes  # present
    assert "<p>" not in fields.creator_notes  # ...but converted to markdown


# ---------------------------------------------------------------------------
# greetings -- first_mes only (no alternates observed)
# ---------------------------------------------------------------------------


def test_greetings_single_greeting():
    greetings = mapper.greetings(load_data("airi"))
    assert len(greetings) == 1
    assert greetings[0] == load_data("airi")["first_mes"]


# ---------------------------------------------------------------------------
# provenance accessors
# ---------------------------------------------------------------------------


def test_provenance_accessors():
    data = load_data("abby")
    assert mapper.card_id(data) == "c9630bdc-c851-4d90-a6e2-fc96a19b3e1a"
    assert mapper.creator(data) == "MathiDoos"
    assert mapper.page_name(data) == "Abby"
    assert mapper.source_url(data) == (
        "https://jannyai.com/characters/"
        "c9630bdc-c851-4d90-a6e2-fc96a19b3e1a_character-abby"
    )
    assert mapper.is_jannyai(data) is True


def test_source_url_carries_hyphenated_slug():
    assert mapper.source_url(load_data("alexandra")).endswith(
        "_character-alexandra-defamed-cheerleader"
    )


def test_is_jannyai_false_without_block():
    assert mapper.is_jannyai({"name": "x"}) is False
    # a datacat card must not be misread as JannyAI
    assert mapper.is_jannyai({"extensions": {"datacat": {"id": "x"}}}) is False


def test_source_url_none_without_id():
    assert mapper.source_url({"extensions": {"jannyai": {}}}) is None


# ---------------------------------------------------------------------------
# integration -- the mapped fields build a clean card through CardBuilder
# ---------------------------------------------------------------------------


def test_builds_card_with_macros_intact_and_no_book():
    data = load_data("abby")
    profile = mapper.to_profile_fields(data)
    card, warnings = CardBuilder().build(
        profile, mapper.greetings(data), capture=None, book=None
    )
    assert card.name == "Abby"
    assert card.description
    assert card.first_mes
    assert card.character_book is None  # JannyAI carries no lorebook
    assert "{{" in card.description  # sanitizer preserves real macros
    assert "no first_mes" not in " ".join(warnings)
