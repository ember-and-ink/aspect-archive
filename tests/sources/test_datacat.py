"""datacat import mapping, validated against two real datacat card exports
(tests/fixtures/datacat/*.json -- the base64-decoded embedded card objects):

  * abigail       -- single greeting, no alternates (toraval)
  * alt_greetings -- Aoi, a first_mes plus 5 alternates (AlissaOne)

The browser-capture V2 builder (Phase 3B, see docs/PHASE_3B_PLAN.md) is
validated separately, further down, against a real live capture:

  * raw_api_character_abbie -- GET /api/characters/{id} for JanitorAI's
    "Offer You Can't Refuse | Abbie" (KornyPony), captured live 2026-08-11
    through the archive server's own /api/v1/datacat/dc-proxy -- the same
    card this archive already has via the native /build-jai path
    (data/characters/Abbie_0d162f5f.png), so the mapped fields can be
    checked against that real twin, not just asserted in isolation.
"""

import base64
import io
import json
from pathlib import Path

from PIL import Image

from proxy.sources import datacat as mapper
from proxy.cards import pngtools
from proxy.cards.builder import CardBuilder

FIXTURES = Path(__file__).parent.parent / "fixtures" / "datacat"


def load_obj(name: str) -> dict:
    """The full embedded card object ({spec, spec_version, data, ...mirror})."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def load_data(name: str) -> dict:
    return load_obj(name)["data"]


def png_with_card(obj: dict) -> bytes:
    """A minimal PNG carrying `obj` as a base64(JSON) `ccv3`/`chara` card, the
    way a datacat export does -- for exercising extract_card end to end."""
    buf = io.BytesIO()
    Image.new("RGBA", (1, 1)).save(buf, "PNG")
    payload = base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
    return pngtools.inject_text_chunks(buf.getvalue(), {"chara": payload, "ccv3": payload})


# ---------------------------------------------------------------------------
# extract_card -- pull the embedded card back out of PNG bytes
# ---------------------------------------------------------------------------


def test_extract_card_roundtrips_embedded_card():
    data = mapper.extract_card(png_with_card(load_obj("abigail")))
    assert data is not None
    assert data["name"] == "Abigail"
    assert mapper.datacat_block(data)["id"] == "f901406d-c5bb-48d7-b37a-6815e94ee879"


def test_extract_card_prefers_ccv3_over_chara():
    # ccv3 is authoritative; a divergent chara chunk must not win.
    buf = io.BytesIO()
    Image.new("RGBA", (1, 1)).save(buf, "PNG")
    v3 = base64.b64encode(json.dumps({"data": {"name": "V3"}}).encode()).decode()
    v2 = base64.b64encode(json.dumps({"data": {"name": "V2"}}).encode()).decode()
    png = pngtools.inject_text_chunks(buf.getvalue(), {"chara": v2, "ccv3": v3})
    assert mapper.extract_card(png)["name"] == "V3"


def test_extract_card_returns_none_for_non_png():
    assert mapper.extract_card(b"not a png") is None


def test_extract_card_returns_none_without_card_chunk():
    buf = io.BytesIO()
    Image.new("RGBA", (1, 1)).save(buf, "PNG")
    assert mapper.extract_card(buf.getvalue()) is None


# ---------------------------------------------------------------------------
# to_profile_fields -- datacat puts the whole definition in `description`
# ---------------------------------------------------------------------------


def test_profile_fields_map_definition():
    fields = mapper.to_profile_fields(load_data("abigail"))
    assert fields.name == "Abigail"
    assert fields.creator == "toraval"
    assert fields.description.startswith("Name= Abigail")
    assert "{{user}}" in fields.scenario  # macros are intact
    assert "Royalty" in fields.tags


def test_hash_prefixed_tags_are_normalized_through_to_the_built_card():
    # The real Phase 5 gap: resolve_tag_names strips a leading emoji but not a
    # leading "#" (tests/fixtures/datacat/raw_api_character_abbie.json and
    # great_n_datacat.json both carry #wildwest/#outlaw). CardBuilder is the
    # shared choke point that catches it -- see docs/PHASE_5_TAGS_PLAN.md §2.
    data = load_data("abigail")
    data["tags"] = ["#wildwest", "👤 outlaw", "  slow   burn  ", "Femdom", "femdom"]
    profile = mapper.to_profile_fields(data)
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)
    assert card.tags == ["wildwest", "outlaw", "slow burn", "Femdom"]


def test_creator_notes_are_dehtmled():
    data = load_data("abigail")
    assert "<p>" in (data["creator_notes"])  # raw datacat HTML in the source
    fields = mapper.to_profile_fields(data)
    assert fields.creator_notes  # present
    assert "<p>" not in fields.creator_notes  # ...but converted to markdown


# ---------------------------------------------------------------------------
# greetings -- first_mes first, then alternates
# ---------------------------------------------------------------------------


def test_greetings_single_when_no_alternates():
    greetings = mapper.greetings(load_data("abigail"))
    assert len(greetings) == 1
    assert greetings[0] == load_data("abigail")["first_mes"]


def test_greetings_primary_then_alternates():
    data = load_data("alt_greetings")
    greetings = mapper.greetings(data)
    assert greetings[0] == data["first_mes"]
    assert len(greetings) == 1 + len(data["alternate_greetings"])


# ---------------------------------------------------------------------------
# provenance accessors
# ---------------------------------------------------------------------------


def test_provenance_accessors():
    data = load_data("abigail")
    assert mapper.card_id(data) == "f901406d-c5bb-48d7-b37a-6815e94ee879"
    assert mapper.creator(data) == "toraval"
    assert mapper.page_name(data) == "Abigail"
    assert mapper.source_url(data) == (
        "https://janitorai.com/characters/f901406d-c5bb-48d7-b37a-6815e94ee879"
    )
    assert mapper.is_datacat(data) is True


def test_is_datacat_false_without_block():
    assert mapper.is_datacat({"name": "x"}) is False


# ---------------------------------------------------------------------------
# integration -- the mapped fields build a clean card through CardBuilder
# ---------------------------------------------------------------------------


def test_builds_card_with_macros_intact_and_no_book():
    data = load_data("abigail")
    profile = mapper.to_profile_fields(data)
    card, warnings = CardBuilder().build(
        profile, mapper.greetings(data), capture=None, book=None
    )
    assert card.name == "Abigail"
    assert card.description
    assert card.first_mes
    assert card.character_book is None  # datacat carries no lorebook
    assert "{{user}}" in card.scenario  # sanitizer preserves real macros
    assert "no first_mes" not in " ".join(warnings)


# ---------------------------------------------------------------------------
# Browser-capture V2 builder (Phase 3B): buildV2FromDatacat/buildV2FromDownload
# ports, validated against the real Abbie capture described in the module
# docstring -- and, where the field survives untouched, against the archive's
# own native-JanitorAI twin of the same character.
# ---------------------------------------------------------------------------


def load_raw(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_normalized_source_kind_collapses_datacat_row_kinds():
    # The real trap this fixture caught: a JanitorAI row's own
    # primary_content_source_kind is "janitor_core", not the bare "janitor"
    # extensions.datacat.sourceKind expects everywhere else in this project.
    character = load_raw("raw_api_character_abbie")["character"]
    assert character["primary_content_source_kind"] == "janitor_core"
    assert mapper.normalized_source_kind(character) == "janitor"
    assert mapper.normalized_source_kind({"primary_content_source_kind": "saucepan"}) == "saucepan"
    assert mapper.normalized_source_kind({}) == "janitor"


def test_build_v2_from_character_matches_the_archived_native_twin():
    character = load_raw("raw_api_character_abbie")["character"]

    v2 = mapper.build_v2_from_character(character)
    data = v2["data"]

    # This exact character is also in the archive via the native /build-jai path
    # (data/characters/Abbie_0d162f5f.png, extensions.jai.sourceKind
    # "janitor_core") -- these fields must match it byte for byte.
    assert data["name"] == "Abbie"
    assert data["creator"] == "KornyPony"
    assert data["first_mes"].startswith('"Fuck... Fuck, fuck, fuck!"')
    assert len(data["alternate_greetings"]) == 2
    assert data["scenario"]
    assert len(data["description"]) == 6715  # matches the archived twin exactly
    # No scripts on this row -- no lorebook, same as the archived twin (a
    # native /build-jai retrieval that just happens to carry none either).
    assert data["character_book"] is None

    # The listing title is datacat's `name`; `chat_name` is the character. The
    # native twin recorded "Offer You Can't Refuse | Abbie" as its pageName, so
    # the capture path has to reach the same value rather than falling through
    # to data.name and recording "Abbie" as this card's listing.
    assert character["name"].strip() != character["chat_name"]
    assert data["extensions"]["datacat"]["pageName"] == "Offer You Can't Refuse | Abbie"
    assert mapper.page_name(data) == "Offer You Can't Refuse | Abbie"

    profile = mapper.to_profile_fields(data)
    assert profile.name == "Abbie"
    greetings = mapper.greetings(data)
    assert len(greetings) == 3  # first_mes + 2 alternates


def test_resolve_avatar_url_prefers_the_untouched_janitorai_original():
    # DataCat's own top-level `avatar` is a re-hosted media.datacat.run copy;
    # the embedded V2 json still points at the untouched ella.janitorai.com
    # original -- the same URL the archive's native /build-jai retrieval used for
    # this exact card (verified against the real archived twin).
    character = load_raw("raw_api_character_abbie")["character"]
    assert character["avatar"].startswith("https://media.datacat.run/")
    assert mapper.resolve_avatar_url(character) == (
        "https://ella.janitorai.com/bot-avatars/RlgBeHp4QSVxOoA9Ic1nx.webp"
    )




# ---------------------------------------------------------------------------
# strip_datacat_markers / resolve_tag_names -- hand-rolled scanners where there
# used to be regexes that backtracked quadratically on a stranger's text.
# ---------------------------------------------------------------------------


def test_recovery_markers_are_stripped_from_both_ends():
    body = "##DESCRIPTION START##\nName= Abbie\nAge= 24\n##DESCRIPTION END##\n"
    assert mapper.strip_datacat_markers(body) == "Name= Abbie\nAge= 24"


def test_a_trailing_marker_need_not_be_on_its_own_line():
    assert mapper.strip_datacat_markers("Name= Abbie ##FIRST_MESSAGE END##") == "Name= Abbie"


def test_only_a_real_marker_is_stripped():
    # Lowercase, an unfinished delimiter, and a markdown heading are all content.
    for text in ["##description start##\nhi", "## Backstory\nhi", "hi\n##NOT A MARKER##"]:
        assert mapper.strip_datacat_markers(text) == text


def test_a_marker_in_the_middle_is_left_alone():
    text = "before\n##SCENARIO START##\nafter"
    assert mapper.strip_datacat_markers(text) == text


def test_emoji_prefixed_tag_names_lose_only_the_prefix():
    tags = [{"name": "👤 Male"}, {"name": "🇯🇵Japan"}, {"name": "❤️ Romance"}, {"name": "🔥"}]
    # A name that is nothing but emoji is kept whole rather than emptied.
    assert mapper.resolve_tag_names(tags) == ["Male", "Japan", "Romance", "🔥"]


def test_scripts_book_records_each_scripts_run_so_it_splits_by_id():
    # Two public lorebooks plus a private stub with no content: the book is
    # their entries concatenated, and `jai_sources` says where each one starts
    # -- the same record a /build-jai book carries.
    from proxy.cards import lorebook_split

    def script(sid: str, title: str, *contents: str) -> dict:
        entries = [{"key": [c.lower()], "content": c} for c in contents]
        return {"id": sid, "type": "lorebook", "title": title, "is_public": True, "script": json.dumps(entries)}

    book = mapper.extract_character_book_from_scripts(
        {
            "scripts": [
                script("w", "World", "Town", "River"),
                {"id": "p", "type": "lorebook", "title": "Private", "is_public": False},
                script("c", "Cast", "Mayor"),
            ]
        }
    )
    assert book["extensions"]["jai_sources"] == [
        {"id": "w", "title": "World", "entry_count": 2},
        {"id": "c", "title": "Cast", "entry_count": 1},
    ]
    parts = lorebook_split.split_book(book)
    assert [(p.title, p.refs, len(p.entries)) for p in parts] == [("World", ("jai:w",), 2), ("Cast", ("jai:c",), 1)]
