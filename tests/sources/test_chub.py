"""Chub.ai import mapping, validated against a real Chub card export
(tests/fixtures/chub/sakura_makoto.json -- the base64-decoded embedded card
`data`, wrapped as {"data": ...}):

  * sakura_makoto -- Sancay's "Sakura, Makoto" (5 lorebook entries, a styled
    creator_notes blurb with a <style> CSS block, a depth_prompt extension).

The whole point of the Chub path is a *light-touch passthrough*: the card is
already a chara_card_v3, so the mapper cleans the text fields and leaves the
lorebook + extensions exactly as Chub wrote them. The tests below assert both
halves -- what gets cleaned, and what must survive untouched.
"""

import base64
import io
import json
from pathlib import Path

from PIL import Image

from proxy.sources import chub as mapper
from proxy.cards import pngtools
from proxy.text.macros import MacroSanitizer

FIXTURES = Path(__file__).parent.parent / "fixtures" / "chub"


def load_data(name: str) -> dict:
    obj = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return obj["data"]


def png_with_card(obj: dict) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (1, 1)).save(buf, "PNG")
    payload = base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
    return pngtools.inject_text_chunks(buf.getvalue(), {"chara": payload, "ccv3": payload})


def clean(name: str = "sakura_makoto"):
    return mapper.clean_card(load_data(name), MacroSanitizer(user_names=["USER"]))


# ---------------------------------------------------------------------------
# identification + provenance accessors
# ---------------------------------------------------------------------------


def test_is_chub_true_for_chub_card():
    assert mapper.is_chub(load_data("sakura_makoto")) is True


def test_is_chub_false_without_block():
    assert mapper.is_chub({"name": "x"}) is False
    assert mapper.is_chub({"extensions": {"datacat": {"id": "y"}}}) is False


def test_provenance_accessors():
    data = load_data("sakura_makoto")
    assert mapper.card_id(data) == "5655941"  # numeric id, stringified
    assert mapper.name(data) == "Sakura, Makoto"
    assert mapper.creator(data) == "Sancay"
    assert mapper.page_name(data) == "Sakura - Neglected Roommate"
    assert mapper.source_url(data) == (
        "https://chub.ai/characters/Sancay/sakura-neglected-roommate-cec5866ac66c"
    )


# ---------------------------------------------------------------------------
# clean_card -- what gets cleaned
# ---------------------------------------------------------------------------


def test_creator_notes_keep_layout_but_lose_css():
    """A Chub blurb carries real layout, so it is tamed in place rather than
    flattened -- the markup survives, the stylesheet does not."""
    data = load_data("sakura_makoto")
    assert "<style" in data["creator_notes"]  # raw Chub HTML in the source
    cleaned, _ = clean()
    notes = cleaned["creator_notes"]
    assert notes  # present

    # The <style> block is gone -- this is what SillyTavern prompts about, and
    # it only ever inspects <style> elements.
    assert "<style" not in notes
    assert ".sakura-img" not in notes  # a CSS selector that used to leak in
    assert "@keyframes" not in notes

    # ...but the structure it described is still here.
    assert "<div" in notes
    assert "<img" in notes

    # Nothing that could escape the creator-notes drawer or fight the theme.
    for banned in ("position:", "z-index", "animation", "color:", "background"):
        assert banned not in notes


def test_macros_preserved_in_definition():
    cleaned, _ = clean()
    assert "{{user}}" in cleaned["first_mes"]
    assert "{{user}}" in cleaned["description"]


def test_tags_cleaned():
    cleaned, _ = clean()
    # Real tags are already plain words here; cleaning must not corrupt them or
    # drop entries.
    assert "NSFW" in cleaned["tags"]
    assert len(cleaned["tags"]) == len(load_data("sakura_makoto")["tags"])
    assert all(t == t.strip() for t in cleaned["tags"])


def test_tags_normalized_through_the_shared_intake_pipeline():
    # A tag list carrying #, emoji, stray whitespace and a case-dupe -- the
    # regression that would have caught the DataCat gap (Phase 5 plan §2/§6
    # step 3). Chub is the one source that never builds a ProfileFields, so it
    # needs its own coverage of proxy.text.tags.normalize_tags.
    data = load_data("sakura_makoto")
    data["tags"] = ["#wildwest", "👤 outlaw", "  slow   burn  ", "Femdom", "femdom"]
    cleaned, _ = mapper.clean_card(data, MacroSanitizer(user_names=["USER"]))
    assert cleaned["tags"] == ["wildwest", "outlaw", "slow burn", "Femdom"]


def test_post_history_original_macro_not_warned():
    # {{original}} is a real ST macro that Chub cards carry in
    # post_history_instructions; it must not surface as an unresolved-macro
    # warning.
    cleaned, warnings = clean()
    assert "{{original}}" in cleaned["post_history_instructions"]
    assert not any("original" in w for w in warnings)


# ---------------------------------------------------------------------------
# clean_card -- what must survive untouched (the passthrough guarantee)
# ---------------------------------------------------------------------------


def test_extensions_preserved_verbatim():
    data = load_data("sakura_makoto")
    cleaned, _ = clean()
    # chub block, depth_prompt injection, and fav flag all carried through as-is.
    assert cleaned["extensions"] == data["extensions"]
    assert cleaned["extensions"]["depth_prompt"]["prompt"] == (
        data["extensions"]["depth_prompt"]["prompt"]
    )


def test_lorebook_entries_and_extras_preserved():
    data = load_data("sakura_makoto")
    cleaned, _ = clean()
    src_entries = data["character_book"]["entries"]
    out_entries = cleaned["character_book"]["entries"]
    assert len(out_entries) == len(src_entries)
    for src, out in zip(src_entries, out_entries):
        # Fields our LoreEntry model doesn't carry must not be dropped.
        for extra in ("priority", "probability", "selectiveLogic"):
            assert out[extra] == src[extra]
        # Keys / metadata untouched; only content is macro-sanitized.
        assert out["keys"] == src["keys"]
        assert out["position"] == src["position"]
    # book-level meta preserved
    assert cleaned["character_book"]["scan_depth"] == data["character_book"]["scan_depth"]
    assert cleaned["character_book"]["token_budget"] == data["character_book"]["token_budget"]


def test_int_position_passes_through_uncoerced():
    # Chub's `position` is int-or-string; a raw-dict passthrough must keep an int
    # an int (a pydantic round-trip through our str-typed LoreEntry would coerce
    # or reject it). Guards against reintroducing such a round-trip.
    data = {
        "name": "X",
        "creator": "c",
        "extensions": {"chub": {"id": 1}},
        "character_book": {
            "entries": [{"content": "{{user}} hi", "position": 4, "probability": 50}]
        },
    }
    cleaned, _ = mapper.clean_card(data, MacroSanitizer())
    entry = cleaned["character_book"]["entries"][0]
    assert entry["position"] == 4
    assert isinstance(entry["position"], int)
    assert entry["probability"] == 50


def test_clean_card_does_not_mutate_source():
    data = load_data("sakura_makoto")
    before = json.dumps(data, sort_keys=True)
    mapper.clean_card(data, MacroSanitizer(user_names=["USER"]))
    assert json.dumps(data, sort_keys=True) == before  # deep-copied, not mutated


# ---------------------------------------------------------------------------
# to_payload + PngWriter round-trip -- the embedded card reads back intact
# ---------------------------------------------------------------------------


def test_to_payload_wraps_v3_envelope():
    cleaned, _ = clean()
    payload = mapper.to_payload(cleaned)
    assert payload["spec"] == "chara_card_v3"
    assert payload["spec_version"] == "3.0"
    assert payload["data"] is cleaned
    assert payload["name"] == cleaned["name"]  # V2 top-level mirror


def test_payload_survives_png_roundtrip():
    cleaned, _ = clean()
    payload = mapper.to_payload(cleaned)
    png = png_with_card(payload)
    back = pngtools.extract_embedded_card(png)
    assert back["name"] == "Sakura, Makoto"
    assert back["extensions"]["chub"]["id"] == 5655941
    assert len(back["character_book"]["entries"]) == 5
    assert back["character_book"]["entries"][0]["probability"] == (
        cleaned["character_book"]["entries"][0]["probability"]
    )


# ---------------------------------------------------------------------------
# build_v2_from_chub -- the browser-capture path (Phase 3B). Ported from
# chub-api.js:buildCharacterCardFromChub; validated against a real captured
# GET /api/characters/{fullPath}?full=true node (RelicGuy's "Your Bully Wants
# To Be Your Sex Slave?!") plus its real linked-lorebook git-API response,
# both captured live 2026-08-11 -- not hand-written fixtures.
# ---------------------------------------------------------------------------


def load_raw(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_build_v2_from_chub_maps_fields_from_real_node():
    node = load_raw("raw_api_full_your_bully")
    linked = load_raw("raw_linked_lorebook_your_bully")

    data = mapper.build_v2_from_chub(node, linked)

    assert data["name"] == "Autumn"
    assert data["creator"] == "RelicGuy"
    assert data["first_mes"]
    assert len(data["alternate_greetings"]) == 11
    assert "Smut" in data["tags"]
    # Chub bakes its own id/full_path into definition.extensions.chub; the
    # port must preserve them (not just apiData.id/fullPath separately).
    assert data["extensions"]["chub"]["id"] == 7547962
    assert data["extensions"]["chub"]["full_path"] == node["fullPath"]
    assert data["extensions"]["chub"]["tagline"] == node["tagline"]
    # The listing title lives only on the node -- Chub's own export block has
    # no pageName -- so the port has to stamp it, or it is gone. Note it is not
    # `definition.name`: this page is titled "Your Bully Wants To Be Your Sex
    # Slave?!" and holds a character called "Autumn".
    assert data["extensions"]["chub"]["pageName"] == node["name"]
    assert data["extensions"]["chub"]["pageName"] != data["name"]
    # Linked lorebook resolved via the git API, not the (absent) embedded one.
    assert len(data["character_book"]["entries"]) == 4


def test_build_v2_from_chub_survives_clean_card_as_raw_dict_passthrough():
    """The trap: a Chub character_book entry carries extra fields a pydantic
    LoreEntry round-trip would drop (priority/probability/selectiveLogic) and
    a position that isn't always an int. clean_card() must leave them as-is."""
    node = load_raw("raw_api_full_your_bully")
    linked = load_raw("raw_linked_lorebook_your_bully")

    data = mapper.build_v2_from_chub(node, linked)
    cleaned, warnings = mapper.clean_card(data, MacroSanitizer())

    entry = cleaned["character_book"]["entries"][0]
    assert "priority" in entry
    assert "probability" in entry
    assert "selectiveLogic" in entry
    assert isinstance(entry["position"], str)  # Chub sent "", not an int
    assert warnings == []

    assert mapper.card_id(cleaned) == "7547962"
    assert mapper.page_name(cleaned) == "Your Bully Wants To Be Your Sex Slave?!"
    assert mapper.source_url(cleaned) == (
        "https://chub.ai/characters/RelicGuy/your-bully-wants-to-be-your-sex-slave-6d84429ab4b8"
    )


def test_build_v2_from_chub_falls_back_to_embedded_lorebook_without_linked():
    node = load_raw("raw_api_full_your_bully")
    data = mapper.build_v2_from_chub(node, linked_lorebook=None)
    # No linked lorebook passed in -- falls back to definition.embedded_lorebook.
    assert data["character_book"] is node["definition"]["embedded_lorebook"]


def test_avatar_url_prefers_full_res_over_the_200px_thumbnail():
    base = "https://avatars.charhub.io/avatars/a/b"
    node = {
        "fullPath": "a/b",
        "avatar_url": f"{base}/avatar.webp",
        "max_res_url": f"{base}/chara_card_v2.png",
    }
    assert mapper.avatar_url(node) == f"{base}/chara_card_v2.png"
    # No max_res_url: build the original's URL, never fall to avatar.webp.
    del node["max_res_url"]
    assert mapper.avatar_url(node) == f"{base}/chara_card_v2.png"
    assert mapper.avatar_url({"avatar_url": f"{base}/avatar.webp"}) == f"{base}/avatar.webp"
    assert mapper.avatar_url({}) is None
