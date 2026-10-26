"""Spec for aspect/sources.saucepan against real captured saucepan exports.

Fixtures are the thin JSON the (refactored) saucepan userscript posts to
/build-saucepan -- {id, definition, companion, lorebooks} -- captured live from
saucepan's API. Four representative fully-open cards:

    Eve   (desslok)  -- Advanced Prompt + Example Dialogue, 2 lorebooks
    Taryn (Theodrax) -- Response Formatting only, no Example Dialogue, 1 lorebook
    Akane (dezea)    -- the JanitorAI-mirror card; 9 greetings, 110 lore chapters
    JJ    (Theodrax) -- Response Formatting + Example Dialogue, "Blank" greeting
"""

import json
from pathlib import Path

from aspect.sources import saucepan as m
from aspect.cards.builder import CardBuilder

FIX = Path(__file__).parent.parent / "fixtures" / "saucepan"

EVE = "04a0c1ac"
TARYN = "1155a61e"
AKANE = "7aef6bad"
JJ = "ff6eb375"
CLOSED = "closed_83831943"  # hidden companion: open_definition:false


def load(id_fragment: str) -> dict:
    path = next(FIX.glob(f"saucepan_{id_fragment}*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Eve -- the fully-structured baseline (Advanced Prompt, Example Dialogue, books)
# ---------------------------------------------------------------------------


def test_eve_profile_fields():
    pf = m.to_profile_fields(load(EVE))
    assert pf.name == "Eve"
    assert pf.creator == "desslok"
    assert len(pf.tags) == 23
    assert "enhanced" in pf.tags
    assert pf.description.startswith("NAME: Eve Ackerman")
    assert "Prototype Android MX-07" in pf.description
    # Advanced Prompt leads scenario, raw; Eve has no other non-core section.
    assert pf.scenario.startswith("{{char}} is an android struggling to learn emotion")
    assert "--- " not in pf.scenario
    assert pf.mes_example.startswith("<START> {{char}}:")
    # blurb leads, then one markdown image per portrait (11 for Eve)
    assert pf.creator_notes.startswith("What if the machine is more humane than the human?")
    assert pf.creator_notes.count("![") == 11
    assert "![The Three Bullies](https://saucepan.ai/cdn/370f5b18-a180-4259-b9f8-17e46113cd00/highres)" in pf.creator_notes


def test_eve_greetings_drop_blank_placeholder():
    gr = m.greetings(load(EVE))
    # 5 starting scenarios, but "Choose Your Own Adventure!" is an all-decoy blank.
    assert len(gr) == 4
    assert all(g.strip() for g in gr)
    assert gr[0].startswith("Throughout her first week at Crestfall High")


def test_eve_character_book_merges_both_lorebooks():
    book = m.character_book(load(EVE), "Eve")
    assert book is not None
    assert len(book.entries) == 19  # 6 + 13 chapters across two lorebooks
    e0 = book.entries[0]
    assert e0.comment == "Eve's Father"
    assert e0.name == "Eve's Father"
    assert e0.keys == ["eve's father"]
    assert e0.insertion_order == 10
    # ties the mapper to the pinned chapter0 deobfuscation
    assert e0.content.startswith("NAME: Doctor Charles Ackerman")
    # ids/orders run sequentially across the merged books
    assert [e.id for e in book.entries] == list(range(1, 20))
    assert [e.insertion_order for e in book.entries] == [n * 10 for n in range(1, 20)]


def test_eve_avatar_and_meta():
    raw = load(EVE)
    assert m.avatar_url(raw) == "https://saucepan.ai/cdn/3a7962f6-1640-49dc-e70d-4ec6b7022900/card"
    assert m.page_name(raw) == "Eve | I Did Nothing Wrong"
    assert m.companion_id(raw) == "04a0c1ac-187b-4aa0-8f5b-885533be748d"
    assert m.is_open(raw) is True
    assert m.creator_id(raw) == "cba8693b-3a04-42fe-883d-27df186ca711"


def test_eve_portrait_gallery():
    raw = load(EVE)
    gallery = m.portrait_urls(raw)
    assert len(gallery) == 11
    assert gallery[0] == (
        "The Three Bullies",
        "https://saucepan.ai/cdn/370f5b18-a180-4259-b9f8-17e46113cd00/highres",
    )
    # avatar and gallery images are distinct ids -- no accidental collision
    assert m.avatar_url(raw) not in [url for _, url in gallery]


def test_portrait_gallery_empty_when_locked():
    raw = load(EVE)
    raw["companion"]["companion"]["unlocked_portraits"] = False
    assert m.portrait_urls(raw) == []
    pf = m.to_profile_fields(raw)
    assert pf.creator_notes == "What if the machine is more humane than the human?"


def test_portrait_gallery_empty_when_no_portraits():
    raw = load(TARYN)
    raw["companion"]["companion"]["portraits"] = []
    assert m.portrait_urls(raw) == []


# ---------------------------------------------------------------------------
# Taryn / JJ -- "Response Formatting Instructions" (no Advanced Prompt) -> scenario
# ---------------------------------------------------------------------------


def test_taryn_response_formatting_becomes_labeled_scenario():
    pf = m.to_profile_fields(load(TARYN))
    assert pf.name == "Taryn"
    assert pf.creator == "Theodrax"
    assert pf.scenario.startswith("--- Response Formatting Instructions ---\n")
    assert "Put inner thoughts inside asterisks" in pf.scenario
    assert pf.mes_example == ""  # Taryn has no Example Dialogue section


def test_taryn_greetings_drop_blank():
    gr = m.greetings(load(TARYN))
    assert len(gr) == 2  # "Going Out", "The Club"; "Blank" dropped


def test_jj_has_example_dialogue_and_formatting_scenario():
    raw = load(JJ)
    pf = m.to_profile_fields(raw)
    assert pf.name == "JJ"
    assert pf.scenario.startswith("--- Response Formatting Instructions ---")
    assert pf.mes_example != ""  # JJ does have Example Dialogue
    assert len(m.greetings(raw)) == 3  # one "Blank" dropped from four


# ---------------------------------------------------------------------------
# Akane -- the JanitorAI mirror; macro fidelity + heavy lorebook
# ---------------------------------------------------------------------------


def test_akane_mirror_card():
    raw = load(AKANE)
    pf = m.to_profile_fields(raw)
    assert pf.name == "Akane Kujo"
    assert pf.creator == "dezea"
    assert pf.scenario.startswith("[System Instructions for Roleplay]")
    # macros come back intact from the definition API -- NOT account-substituted.
    assert pf.description.count("{{user}}") > 0
    assert len(m.greetings(raw)) == 9
    book = m.character_book(raw, "Akane Kujo")
    assert len(book.entries) == 110  # 20 + 90 chapters merged


# ---------------------------------------------------------------------------
# scenario assembly rule (no single fixture exercises Advanced + extra together)
# ---------------------------------------------------------------------------


def test_build_scenario_advanced_leads_then_labeled_extras():
    sections = [
        ("Companion Core", "core text"),
        ("Advanced Prompt", "adv text"),
        ("Response Formatting Instructions", "fmt text"),
        ("Example Dialogue", "ex text"),
        ("Mystery Section", "mystery text"),
    ]
    assert m._build_scenario(sections) == (
        "adv text\n\n"
        "--- Response Formatting Instructions ---\nfmt text\n\n"
        "--- Mystery Section ---\nmystery text"
    )


def test_build_scenario_empty_when_only_core_and_example():
    sections = [("Companion Core", "c"), ("Example Dialogue", "e")]
    assert m._build_scenario(sections) == ""


# ---------------------------------------------------------------------------
# Hidden/locked companion -- open_definition:false. The definition API returns a
# decoy error, so only the v2 public fields survive (name, blurb, greetings,
# avatar); the definition sections (scenario/example) come back empty. This is
# the flag the userscript pill reads to show `hidden ✗`.
# ---------------------------------------------------------------------------


def test_hidden_companion_is_not_open():
    raw = load(CLOSED)
    assert m.is_open(raw) is False
    # the definition endpoint returned a decoy error, not real sections
    assert list((raw.get("definition") or {}).keys()) == ["error"]


def test_hidden_companion_falls_back_to_public_fields():
    raw = load(CLOSED)
    pf = m.to_profile_fields(raw)
    assert pf.name == "Maddie, Alice, Laila, Veronica, Sadie"
    assert pf.creator == "GreatN"
    assert m.page_name(raw) == "Roommate Hates Your New Girlfriend"
    # description falls back to the v2 public blurb...
    assert pf.description.startswith("You have been living the dream")
    # ...but the definition sections (scenario/example dialogue) are gone.
    assert pf.scenario == ""
    assert pf.mes_example == ""
    # starting scenarios live in the v2 companion payload, so greetings survive.
    assert len(m.greetings(raw)) == 9


# ---------------------------------------------------------------------------
# The mapper output drops straight into the shared CardBuilder (janitor peer).
# ---------------------------------------------------------------------------


def test_mapper_output_feeds_cardbuilder():
    raw = load(EVE)
    pf = m.to_profile_fields(raw)
    card, warnings = CardBuilder().build(
        pf,
        m.greetings(raw),
        capture=None,
        book=m.character_book(raw, pf.name),
        avatar_url=m.avatar_url(raw),
    )
    assert card.name == "Eve"
    assert card.creator == "desslok"
    assert card.first_mes.startswith("Throughout her first week")
    assert len(card.alternate_greetings) == 3
    assert card.scenario.startswith("{{char}} is an android")
    assert card.character_book is not None and len(card.character_book.entries) == 19
    # avatar markdown leads creator_notes, as on the JanitorAI path
    assert card.creator_notes.startswith("![Eve](https://saucepan.ai/cdn/")


def test_tags_normalized_through_the_shared_intake_pipeline():
    # Saucepan's own to_profile_fields does no tag cleaning at all -- CardBuilder
    # is the shared choke point that catches #/emoji/whitespace/case-dupes for
    # this source. See docs/PHASE_5_TAGS_PLAN.md §2/§6 step 3.
    raw = load(EVE)
    raw["companion"]["companion"]["tags"] = ["#wildwest", "👤 outlaw", "  slow   burn  ", "Femdom", "femdom"]
    pf = m.to_profile_fields(raw)
    card, _ = CardBuilder().build(pf, greetings=[], capture=None, book=None)
    assert card.tags == ["wildwest", "outlaw", "slow burn", "Femdom"]
