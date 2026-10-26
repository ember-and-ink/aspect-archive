import base64
import io
import json
import random
from pathlib import Path

import pytest
from PIL import Image

from aspect.sources import janitor
from aspect.cards import pngtools
from aspect.cards.builder import CardBuilder, PngWriter
from aspect.cards.naming import id_fragment, safe_filename
from aspect.config import settings
from aspect.text.macros import MacroSanitizer
from aspect.cards.models import CaptureRecord, CharacterBook, LoreEntry, ProfileFields

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_character(name: str) -> dict:
    return json.loads((FIXTURES / "hampter" / f"{name}.json").read_text(encoding="utf-8"))


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), (5, 5, 5, 255)).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CardBuilder -- real Akane Kujo character JSON (public card: the mapped
# profile fields win outright since there's no capture).
# ---------------------------------------------------------------------------


def test_build_public_card_from_real_profile_fixture():
    profile = janitor.to_profile_fields(_load_character("open_akane_kujo"))
    card, warnings = CardBuilder().build(profile, greetings=[], capture=None, book=None)

    assert card.name == "Akane Kujo"
    assert card.creator == "dezea"
    assert card.description.startswith(">Character Information:")
    assert card.scenario.startswith("[System Instructions for Roleplay]")
    assert card.personality == ""
    assert "no first_mes / greetings found" in warnings


def test_build_maps_greetings_to_first_mes_and_alternates():
    # CardBuilder consumes authored greeting markdown straight from the JSON;
    # it only sanitizes macros here.
    profile = ProfileFields(name="Test", description="d", scenario="s")
    card, _ = CardBuilder().build(
        profile, greetings=["Hello **there**", "Second greeting"], capture=None, book=None
    )
    assert card.first_mes == "Hello **there**"
    assert card.alternate_greetings == ["Second greeting"]


# ---------------------------------------------------------------------------
# Hidden-card precedence: empty DOM def + capture -> capture fills the gap.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tag normalization -- CardBuilder is the single choke point for the four
# sources that build a ProfileFields (janitor, datacat, saucepan, jannyai);
# see aspect/text/tags.py and docs/PHASE_5_TAGS_PLAN.md §2/§6 step 3. Chub
# never goes through ProfileFields, so it gets its own test in test_chub.py.
# ---------------------------------------------------------------------------


def test_build_normalizes_tags_through_the_shared_pipeline():
    profile = ProfileFields(
        name="Test",
        description="d",
        tags=["#wildwest", "👤 outlaw", "  slow   burn  ", "Femdom", "femdom"],
    )
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)
    assert card.tags == ["wildwest", "outlaw", "slow burn", "Femdom"]


def test_hidden_capture_fills_gap_when_dom_definition_empty():
    profile = ProfileFields(name="Lyra", description="", scenario="", mes_example="")
    capture = CaptureRecord(
        name="Lyra",
        personality="hidden personality text",
        scenario="hidden scenario text",
        mes_example="hidden mes example text",
        raw_system_prompt="<Lyra's Persona>hidden personality text</Lyra's Persona>",
    )
    card, warnings = CardBuilder().build(profile, greetings=[], capture=capture, book=None)

    assert card.description == "hidden personality text"
    assert card.scenario == "hidden scenario text"
    assert card.mes_example == "hidden mes example text"
    assert "no description/scenario/example dialogs found" not in warnings


def test_visible_dom_value_wins_over_capture_when_both_present():
    profile = ProfileFields(name="Lyra", description="visible wins")
    capture = CaptureRecord(name="Lyra", personality="hidden loses")
    card, _ = CardBuilder().build(profile, greetings=[], capture=capture, book=None)
    assert card.description == "visible wins"


def test_capture_name_used_when_profile_name_missing():
    profile = ProfileFields(name="")
    capture = CaptureRecord(name="Captured Name")
    card, _ = CardBuilder().build(profile, greetings=[], capture=capture, book=None)
    assert card.name == "Captured Name"


# ---------------------------------------------------------------------------
# Macro sanitization + warnings
# ---------------------------------------------------------------------------


def test_unresolved_macro_surfaces_as_warning():
    profile = ProfileFields(name="X", description="hello {{waifu}} friend", scenario="s")
    card, warnings = CardBuilder().build(profile, greetings=[], capture=None, book=None)
    assert card.description == "hello {{waifu}} friend"
    assert "unresolved macro: {{waifu}}" in warnings


def test_known_pronoun_macro_folds_without_warning():
    profile = ProfileFields(name="X", description="hi {obj}, love {{poss}}", scenario="s")
    card, warnings = CardBuilder().build(profile, greetings=[], capture=None, book=None)
    assert card.description == "hi {{user}}, love {{user}}"
    assert not any("unresolved macro" in w for w in warnings)


# ---------------------------------------------------------------------------
# character_book passthrough
# ---------------------------------------------------------------------------


def test_character_book_attached_when_provided():
    profile = ProfileFields(name="X", description="d", scenario="s")
    book = CharacterBook(name="lore", entries=[LoreEntry(keys=["k"], content="c")])
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=book)
    assert card.character_book is book


# ---------------------------------------------------------------------------
# creator_notes gets a leading avatar reference, echoing the JanitorAI
# sidebar (full avatar, then text) and preserving a clean pointer to the
# original image before the embedded avatar gets cropped/resized.
# ---------------------------------------------------------------------------


def test_build_prepends_avatar_reference_to_creator_notes():
    profile = ProfileFields(name="Ari", creator_notes="Some creator notes.")
    card, _ = CardBuilder().build(
        profile,
        greetings=[],
        capture=None,
        book=None,
        avatar_url="https://ella.janitorai.com/bot-avatars/ari.webp",
    )
    assert card.creator_notes == (
        "![Ari](https://ella.janitorai.com/bot-avatars/ari.webp)\n\nSome creator notes."
    )


def test_build_creator_notes_unchanged_without_avatar_url():
    profile = ProfileFields(name="Ari", creator_notes="Some creator notes.")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)
    assert card.creator_notes == "Some creator notes."


# ---------------------------------------------------------------------------
# safe_filename
# ---------------------------------------------------------------------------


def test_safe_filename_strips_unsafe_characters():
    assert safe_filename("Rival Mafia Heiress - Vivienne Laurent") == "Rival_Mafia_Heiress_-_Vivienne_Laurent"
    assert safe_filename("   ") == "unnamed"
    assert safe_filename("Akane Kujo") == "Akane_Kujo"


def test_id_fragment_takes_first_uuid_segment():
    assert id_fragment("bffaaf71-6c33-4e82-8cf2-3699f2ce4d92") == "bffaaf71"
    # Non-UUID / shorter ids pass through (capped at 8 chars).
    assert id_fragment("abc123") == "abc123"
    assert id_fragment("0123456789") == "01234567"
    # Nothing usable -> empty, so the filename degrades to just the name.
    assert id_fragment(None) == ""
    assert id_fragment("   ") == ""


# ---------------------------------------------------------------------------
# PngWriter -- round trip: write -> reopen -> read chunks back -> JSON equal.
# ---------------------------------------------------------------------------


def _decode(path: Path) -> dict:
    """The card envelope embedded in a written PNG."""
    with Image.open(path) as reopened:
        return json.loads(base64.b64decode(reopened.text["ccv3"]))


def _pop_gallery_id(envelope: dict) -> str:
    """Remove the write-time gallery_id from a decoded envelope -- from the
    `data` object and its V2 top-level mirror both -- and return it, so what's
    left can be compared against the card as it was before the write."""
    gid = envelope["data"]["extensions"].pop("gallery_id")
    envelope["extensions"].pop("gallery_id")
    return gid


def test_png_writer_round_trips_card_json(tmp_path):
    akane = _load_character("open_akane_kujo")
    profile = janitor.to_profile_fields(akane)
    greetings = janitor.greetings(akane)
    card, _ = CardBuilder().build(profile, greetings=greetings, capture=None, book=None)

    writer = PngWriter(output_dir=tmp_path, layout="flat")
    path = writer.write(card, _png_bytes(), card_id="bffaaf71-6c33-4e82-8cf2-3699f2ce4d92")

    # Flat by default (SillyTavern doesn't recurse), name suffixed with the
    # 8-char id fragment that keeps it unique across creators.
    assert path.parent == tmp_path
    assert path.name == "Akane_Kujo_bffaaf71.png"
    assert path.exists()

    reopened = Image.open(path)
    assert reopened.text["chara"] == reopened.text["ccv3"]
    decoded = json.loads(base64.b64decode(reopened.text["ccv3"]))
    # The gallery_id is minted by the write itself; everything else round-trips.
    assert len(_pop_gallery_id(decoded)) == 12
    assert decoded == card.to_dict()
    assert decoded["data"]["name"] == "Akane Kujo"
    assert decoded["data"]["first_mes"].startswith("**Scenario: Welcome to Kamii University!**")


def test_png_writer_converts_non_png_avatar_source(tmp_path):
    # Simulate a webp avatar (JanitorAI avatars are typically .webp) --
    # PngWriter must still write a valid PNG with the chunks readable.
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 100, 50)).save(buf, "WEBP")
    webp_bytes = buf.getvalue()

    profile = ProfileFields(name="Webp Test", description="d")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)

    path = PngWriter(output_dir=tmp_path).write(card, webp_bytes)
    reopened = Image.open(path)
    assert reopened.format == "PNG"
    assert json.loads(base64.b64decode(reopened.text["chara"]))["data"]["name"] == "Webp Test"


def _stacked_avatar_png(panel: int = 300) -> bytes:
    """A 3-image stack (height == 3x width) -- the shape the archive detects and
    crops down to the top panel before embedding."""
    buf = io.BytesIO()
    Image.new("RGBA", (panel, panel * 3), (5, 5, 5, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_png_writer_crops_detected_stack_avatar_to_top_third(tmp_path):
    profile = ProfileFields(name="Stacked")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)

    path = PngWriter(output_dir=tmp_path, compress=False).write(card, _stacked_avatar_png(300))
    with Image.open(path) as embedded:
        assert embedded.size == (300, 300)


def test_png_writer_downscales_oversized_avatar(tmp_path):
    buf = io.BytesIO()
    Image.new("RGBA", (4000, 2000), (5, 5, 5, 255)).save(buf, "PNG")

    profile = ProfileFields(name="Big")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)

    path = PngWriter(output_dir=tmp_path, compress=False).write(card, buf.getvalue())
    with Image.open(path) as embedded:
        assert embedded.size == (1920, 960)


def test_png_writer_creates_output_dir_if_missing(tmp_path):
    out_dir = tmp_path / "deep" / "cards"
    profile = ProfileFields(name="Nested")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)

    path = PngWriter(output_dir=out_dir, layout="flat").write(card, _png_bytes())
    assert path.exists()
    assert path.parent == out_dir


def test_png_writer_nested_layout_folders_by_creator(tmp_path):
    profile = ProfileFields(name="Akane Kujo", creator="dezea")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)

    writer = PngWriter(output_dir=tmp_path, layout="nested")
    path = writer.write(card, _png_bytes(), card_id="bffaaf71-6c33-4e82-8cf2-3699f2ce4d92")

    assert path.parent == tmp_path / "dezea"
    assert path.name == "Akane_Kujo_bffaaf71.png"


def test_png_writer_nested_layout_falls_back_to_unknown_creator(tmp_path):
    profile = ProfileFields(name="Orphan")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)

    path = PngWriter(output_dir=tmp_path, layout="nested").write(card, _png_bytes())
    assert path.parent == tmp_path / "unknown_creator"


def test_existing_and_find_match_across_both_layouts(tmp_path):
    """The lookups a bulk export skips on -- `existing` and `find_by_id`, both
    keyed on the id fragment -- glob recursively, so a folder holding cards
    written under either layout (an archive migrated from nested to flat) still
    resolves both."""
    cid = "bffaaf71-6c33-4e82-8cf2-3699f2ce4d92"
    profile = ProfileFields(name="Akane Kujo", creator="dezea")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)
    PngWriter(output_dir=tmp_path, layout="nested").write(card, _png_bytes(), card_id=cid)

    flat = PngWriter(output_dir=tmp_path, layout="flat")
    assert flat.existing([cid]) == {cid}
    assert [p.name for p in flat.find_by_id(cid)] == ["Akane_Kujo_bffaaf71.png"]


# ---------------------------------------------------------------------------
# gallery_id -- stamped at write time (the one choke point every save path,
# native and import, funnels through) so a card drops into its own gallery
# folder in SillyTavern-CharacterLibrary. See aspect/cards/gallery.py.
# ---------------------------------------------------------------------------


def test_png_writer_stamps_gallery_id_into_data_and_top_level_mirror(tmp_path):
    profile = ProfileFields(name="Gallery", creator="dezea")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)

    decoded = _decode(PngWriter(output_dir=tmp_path).write(card, _png_bytes()))
    gid = decoded["data"]["extensions"]["gallery_id"]

    assert len(gid) == 12 and gid.isalnum()
    assert decoded["extensions"]["gallery_id"] == gid  # V2 mirror stays in step


def test_png_writer_gives_two_cards_different_gallery_ids(tmp_path):
    writer = PngWriter(output_dir=tmp_path)
    ids = set()
    for name in ("One", "Two"):
        card, _ = CardBuilder().build(ProfileFields(name=name), [], capture=None, book=None)
        ids.add(_decode(writer.write(card, _png_bytes()))["data"]["extensions"]["gallery_id"])
    assert len(ids) == 2


def test_png_writer_keeps_a_gallery_id_the_payload_already_carries(tmp_path):
    # The import path for a Chub card: its whole extensions block is passed
    # through as a raw dict, gallery_id included. That id is the link to an
    # existing gallery -- the write must not replace it.
    data = {"name": "Tsuko", "extensions": {"gallery_id": "f1AMBFO5oPUr", "chub": {"id": 1}}}
    payload = {"spec": "chara_card_v3", "spec_version": "3.0", "data": data}
    payload.update(data)

    path = PngWriter(output_dir=tmp_path).write_payload(
        payload, _png_bytes(), creator="dezea", name="Tsuko"
    )
    assert _decode(path)["data"]["extensions"]["gallery_id"] == "f1AMBFO5oPUr"


def test_png_writer_reuses_the_gallery_id_of_the_card_it_overwrites(tmp_path):
    # Re-exporting a character overwrites its card; a fresh id there would
    # orphan the gallery folder CharacterLibrary already keyed to the old one.
    writer = PngWriter(output_dir=tmp_path)
    profile = ProfileFields(name="Akane Kujo", creator="dezea")
    cid = "bffaaf71-6c33-4e82-8cf2-3699f2ce4d92"

    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)
    first = writer.write(card, _png_bytes(), card_id=cid)
    original = _decode(first)["data"]["extensions"]["gallery_id"]

    card2, _ = CardBuilder().build(profile, greetings=["hi"], capture=None, book=None)
    second = writer.write(card2, _png_bytes(), card_id=cid)

    assert second == first  # same character -> same path, overwritten
    assert _decode(second)["data"]["extensions"]["gallery_id"] == original


def test_png_writer_mints_a_new_gallery_id_when_the_target_is_unreadable(tmp_path):
    # A stray non-card PNG sitting at the target path must not break the write.
    profile = ProfileFields(name="Clobber", creator="dezea")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)
    stray = tmp_path / "dezea" / "Clobber.png"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(_png_bytes())

    decoded = _decode(PngWriter(output_dir=tmp_path).write(card, _png_bytes()))
    assert len(decoded["data"]["extensions"]["gallery_id"]) == 12


# ---------------------------------------------------------------------------
# pngquant compression + raw tEXt (re)injection.
# ---------------------------------------------------------------------------


def _noisy_png(side: int = 96) -> bytes:
    """A many-colour RGB image whose lossless PNG is meaningfully larger than a
    256-colour quantized version -- so pngquant has something to shrink. Solid
    fills (like _png_bytes) are already tiny and get skipped by
    --skip-if-larger."""
    rng = random.Random(0)
    data = bytes(rng.randrange(256) for _ in range(side * side * 3))
    buf = io.BytesIO()
    Image.frombytes("RGB", (side, side), data).save(buf, "PNG")
    return buf.getvalue()


def test_inject_text_chunks_replaces_rather_than_duplicates():
    injected = pngtools.inject_text_chunks(_png_bytes(), {"chara": "AAA", "ccv3": "AAA"})
    reinjected = pngtools.inject_text_chunks(injected, {"chara": "BBB", "ccv3": "BBB"})

    reopened = Image.open(io.BytesIO(reinjected))
    assert reopened.text["chara"] == "BBB"
    assert reopened.text["ccv3"] == "BBB"


def test_pngtools_quantize_shrinks_and_stays_valid_png():
    original = _noisy_png()
    out = pngtools.quantize(original, settings.pngquant_bin)
    if out is None:
        pytest.skip("pngquant binary not runnable on this platform")
    assert out.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(out) < len(original)


def test_png_writer_card_survives_quantization(tmp_path):
    profile = ProfileFields(name="Noisy", description="d" * 50, creator="c")
    card, _ = CardBuilder().build(profile, greetings=["g" * 200], capture=None, book=None)

    path = PngWriter(output_dir=tmp_path, compress=True).write(card, _noisy_png())

    reopened = Image.open(path)
    assert reopened.format == "PNG"
    assert reopened.text["chara"] == reopened.text["ccv3"]
    decoded = json.loads(base64.b64decode(reopened.text["ccv3"]))
    _pop_gallery_id(decoded)  # minted by the write, so not on the pre-write card
    assert decoded == card.to_dict()


def test_png_writer_compression_shrinks_output_file(tmp_path):
    profile = ProfileFields(name="Big", creator="c")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)
    avatar = _noisy_png()
    if pngtools.quantize(avatar, settings.pngquant_bin) is None:
        pytest.skip("pngquant binary not runnable on this platform")

    small = PngWriter(output_dir=tmp_path / "on", compress=True).write(card, avatar)
    big = PngWriter(output_dir=tmp_path / "off", compress=False).write(card, avatar)
    assert small.stat().st_size < big.stat().st_size


def test_png_writer_falls_back_when_binary_missing(tmp_path):
    profile = ProfileFields(name="Fallback", creator="c")
    card, _ = CardBuilder().build(profile, greetings=[], capture=None, book=None)

    # A bogus binary path with no pngquant on PATH -> compression disabled, but
    # a valid, card-bearing PNG is still written.
    writer = PngWriter(output_dir=tmp_path, compress=True, pngquant_bin=tmp_path / "nope")
    path = writer.write(card, _noisy_png())

    reopened = Image.open(path)
    assert reopened.format == "PNG"
    assert json.loads(base64.b64decode(reopened.text["chara"]))["data"]["name"] == "Fallback"


# ---------------------------------------------------------------------------
# M7: persona-name reverse-substitution -- applied to capture-sourced
# definition fields and to all greetings, NOT to profile-DOM fields.
# ---------------------------------------------------------------------------


def test_reverse_sub_applied_to_capture_definition_not_profile_dom():
    profile = ProfileFields(name="Ari", description="", scenario="visible USER text", mes_example="")
    capture = CaptureRecord(
        name="Ari",
        personality="Ari looked at USER",
        scenario="hidden scenario about USER",
        mes_example="USER: hello",
    )
    card, _ = CardBuilder(sanitizer=MacroSanitizer(user_names=["USER"])).build(
        profile, greetings=[], capture=capture, book=None
    )

    assert card.description == "Ari looked at {{user}}"
    assert card.mes_example == "{{user}}: hello"
    # profile.scenario is non-empty (visible DOM value) so it wins outright
    # and must NOT be reverse-substituted.
    assert card.scenario == "visible USER text"


def test_reverse_sub_applied_to_all_greetings():
    profile = ProfileFields(name="Ari")
    card, _ = CardBuilder(sanitizer=MacroSanitizer(user_names=["USER"])).build(
        profile, greetings=["Hi USER", "Bye USER"], capture=None, book=None
    )

    assert card.first_mes == "Hi {{user}}"
    assert card.alternate_greetings == ["Bye {{user}}"]


def test_hidden_style_build_produces_greetings_and_no_literal_persona_name():
    profile = ProfileFields(name="Ari", description="", scenario="", mes_example="")
    capture = CaptureRecord(
        name="Ari",
        personality="Ari's persona mentions USER often",
        scenario="USER and Ari's scenario",
        mes_example="USER: hi\nAri: hello",
    )
    card, warnings = CardBuilder(sanitizer=MacroSanitizer(user_names=["USER"])).build(
        profile,
        greetings=["Hello USER, welcome", "Second greeting for USER"],
        capture=capture,
        book=None,
    )

    assert card.first_mes == "Hello {{user}}, welcome"
    assert card.alternate_greetings == ["Second greeting for {{user}}"]
    for field in (card.description, card.scenario, card.mes_example, card.first_mes, *card.alternate_greetings):
        assert "USER" not in field
    assert "no description/scenario/example dialogs found" not in warnings
    assert "no first_mes / greetings found" not in warnings
