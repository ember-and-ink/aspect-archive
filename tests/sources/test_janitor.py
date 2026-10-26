import json
from pathlib import Path

import pytest

from aspect.cards.builder import CardBuilder
from aspect.sources import janitor as mapper

FIXTURES = Path(__file__).parent.parent / "fixtures" / "hampter"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# Ground truth from the real captured payloads (see jai_proxy_janitor_api
# memory). `open_lila` keeps its own lorebook closed but the card itself is
# open (showdefinition: true); the `closed_*` cards are genuinely hidden.
OPEN = ["open_nyla", "open_akane_kujo", "open_vaelyra", "open_lila"]
HIDDEN = ["closed_amaya", "closed_selene"]


# ---------------------------------------------------------------------------
# is_hidden classification -- the showdefinition flag drives open/hidden.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", OPEN)
def test_open_cards_are_not_hidden(name):
    assert mapper.is_hidden(load(name)) is False


@pytest.mark.parametrize("name", HIDDEN)
def test_hidden_cards_are_hidden(name):
    assert mapper.is_hidden(load(name)) is True


def test_is_hidden_treats_missing_flag_as_open():
    assert mapper.is_hidden({}) is False


# ---------------------------------------------------------------------------
# to_profile_fields -- open cards carry a full definition.
# ---------------------------------------------------------------------------


def test_open_card_maps_full_definition():
    fields = mapper.to_profile_fields(load("open_nyla"))

    assert fields.name == "Nyla"
    assert fields.creator == "Eclipsed_Honor"
    assert fields.description.startswith("<Nyla>")
    assert "Valeris" in fields.scenario
    assert fields.creator_notes  # HTML blurb converted to markdown
    assert fields.tags


def test_akane_maps_to_trusted_reference_metadata():
    # Pins the trusted mapping for this card (historically cross-checked
    # against the real janitorai-export output): chat_name is the real name,
    # official emoji tags come first (emoji stripped) then the creator's
    # custom_tags.
    fields = mapper.to_profile_fields(load("open_akane_kujo"))

    assert fields.name == "Akane Kujo"
    assert fields.creator == "dezea"
    assert fields.tags == [
        "Female", "Multiple", "AnyPOV", "Angst", "Demi-Human",
        "Fluff", "Horror", "kitsune", "yandere", "TheValentine",
    ]
    assert fields.description.startswith(">Character Information:")
    assert fields.mes_example == ""  # example_dialogs is empty for this card
    assert fields.creator_notes.startswith("You're not dating yet.")


def test_tags_normalized_through_the_shared_intake_pipeline():
    # janitor's own tags() already strips emoji/#; CardBuilder is what also
    # collapses whitespace and dedupes case-insensitively. See
    # docs/PHASE_5_TAGS_PLAN.md §2/§6 step 3.
    character = {
        "tags": [{"name": "#wildwest"}, {"name": "👤 outlaw"}, {"name": "  slow   burn  "}],
        "custom_tags": ["Femdom", "femdom"],
    }
    profile = mapper.to_profile_fields(character)
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)
    assert card.tags == ["wildwest", "outlaw", "slow burn", "Femdom"]


def test_chat_name_is_stripped():
    # open_vaelyra's chat_name is " Vaelyra " (leading/trailing space).
    assert mapper.to_profile_fields(load("open_vaelyra")).name == "Vaelyra"


def test_name_falls_back_to_title_blurb_when_chat_name_absent():
    fields = mapper.to_profile_fields({"name": "Some Title Blurb"})
    assert fields.name == "Some Title Blurb"


# ---------------------------------------------------------------------------
# to_profile_fields -- hidden cards: definition withheld, everything else
# (name/creator/tags/creator_notes) still present in the JSON.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected_name,expected_creator",
    [
        ("closed_amaya", "Amaya", "TimberTambre"),
        ("closed_selene", "Selene", "SydneyFeet"),
    ],
)
def test_hidden_card_maps_metadata_but_not_definition(name, expected_name, expected_creator):
    fields = mapper.to_profile_fields(load(name))

    assert fields.name == expected_name
    assert fields.creator == expected_creator
    # The server withholds the definition for hidden cards -- these keys are
    # absent from the JSON entirely, so they map to empty strings.
    assert fields.description == ""
    assert fields.scenario == ""
    assert fields.mes_example == ""
    # ...but the public metadata is all there.
    assert fields.tags
    assert fields.creator_notes


# ---------------------------------------------------------------------------
# greetings -- authored markdown; nulls and invisible-only entries dropped.
# ---------------------------------------------------------------------------


def test_open_card_greetings_are_all_present():
    # open_nyla has 4 real first_messages, none null/invisible.
    greetings = mapper.greetings(load("open_nyla"))
    assert len(greetings) == 4
    assert greetings[0].startswith("*The great lecture hall")


def test_akane_invisible_separator_greeting_is_dropped():
    # open_akane_kujo has 10 first_messages, one of which is an LRM-only
    # "‎ ‎" separator. Dropping it yields 9 -- matching the reference card's
    # 1 first_mes + 8 alternate_greetings.
    greetings = mapper.greetings(load("open_akane_kujo"))
    assert len(greetings) == 9
    assert all(g.strip() for g in greetings)


@pytest.mark.parametrize("name", HIDDEN)
def test_hidden_card_greetings_are_alternates_only(name):
    character = load(name)
    # first_messages[0] (the primary) is nulled out by the server for hidden
    # cards; greetings() returns only the surviving alternates.
    assert character["first_messages"][0] is None
    greetings = mapper.greetings(character)
    assert greetings
    assert None not in greetings
    # Every returned greeting is a real (long) authored greeting -- the
    # withheld primary comes from the chat capture, not this JSON.
    assert all(len(g) >= 100 for g in greetings)


def test_short_placeholder_greetings_are_dropped():
    # closed_amaya / closed_selene each carry a "."/"..." placeholder;
    # open_lila a 28-char stray. None survive the 100-char floor.
    for name in ("closed_amaya", "closed_selene", "open_lila"):
        greetings = mapper.greetings(load(name))
        assert greetings and all(len(g) >= 100 for g in greetings)


_LONG = "x" * 120


def test_greetings_falls_back_to_first_message_singular():
    assert mapper.greetings({"first_message": _LONG}) == [_LONG]
    # first_messages present but all-junk -> fall through to the singular field.
    assert mapper.greetings({"first_messages": ["."], "first_message": _LONG}) == [_LONG]


def test_greetings_empty_when_nothing_present():
    assert mapper.greetings({}) == []
    assert mapper.greetings({"first_messages": [None, "   "]}) == []
    # A short-but-visible entry is a placeholder, not a greeting.
    assert mapper.greetings({"first_messages": ["...", "a stray line"]}) == []


# ---------------------------------------------------------------------------
# avatar_url -- bare filename -> CDN URL.
# ---------------------------------------------------------------------------


def test_avatar_url_builds_cdn_url():
    akane = load("open_akane_kujo")
    assert mapper.avatar_url(akane) == (
        "https://ella.janitorai.com/bot-avatars/" + akane["avatar"]
    )


def test_avatar_url_none_when_absent():
    assert mapper.avatar_url({}) is None
    assert mapper.avatar_url({"avatar": ""}) is None


# ---------------------------------------------------------------------------
# creator_id -- feeds extensions.datacat.creatorId.
# ---------------------------------------------------------------------------


def test_creator_id_reads_the_json_field():
    akane = load("open_akane_kujo")
    assert mapper.creator_id(akane) == "866c0877-ea3d-4bc6-a906-13c5d9601f9d"


def test_creator_id_empty_when_absent():
    assert mapper.creator_id({}) == ""
