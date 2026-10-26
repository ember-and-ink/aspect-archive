"""The write half of `/api/v1`.

The one test in here that matters more than the others is
`test_editing_a_card_leaves_every_pixel_chunk_alone`. Everything else checks
behaviour; that one checks the promise the whole design rests on -- that a card
edit rewrites the tEXt chunks and nothing else, so a pngquant-compressed avatar
survives being edited. Break it and cards silently inflate and lose their
compression on every save, which nobody would notice until the archive had
doubled in size.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from aspect.cards import pngtools
from aspect.config import settings
from tests.conftest import card_png, jai_extensions


def read_card(path: Path) -> dict:
    envelope = pngtools.read_envelope(path.read_bytes())
    assert envelope is not None
    return envelope[1]


def image_bytes(size=(40, 60), colour=(10, 200, 30, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


# --- editing a card ---------------------------------------------------------


def test_put_replaces_the_card_and_reads_back(client, populated_archive):
    card = client.get("/api/v1/characters/Abbie_0d162f5f.png").json()["card"]
    card["description"] = "rewritten"
    card["tags"] = ["Female", "Rewritten"]

    resp = client.put("/api/v1/characters/Abbie_0d162f5f.png", json={"card": card})
    assert resp.status_code == 200
    assert resp.json()["card"]["description"] == "rewritten"

    on_disk = read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")
    assert on_disk["description"] == "rewritten"
    assert on_disk["tags"] == ["Female", "Rewritten"]


def test_editing_a_card_leaves_every_pixel_chunk_alone(client, populated_archive):
    """The load-bearing one. A field edit must not re-encode the image."""
    path = populated_archive["characters"] / "Abbie_0d162f5f.png"
    before = pngtools.non_text_chunks(path.read_bytes())

    card = client.get("/api/v1/characters/Abbie_0d162f5f.png").json()["card"]
    card["description"] = "x" * 5000  # big enough to move every offset in the file
    client.put("/api/v1/characters/Abbie_0d162f5f.png", json={"card": card})

    assert pngtools.non_text_chunks(path.read_bytes()) == before


def test_edit_keeps_the_v3_envelope_and_the_v2_mirror(client, populated_archive):
    card = client.get("/api/v1/characters/Abbie_0d162f5f.png").json()["card"]
    card["scenario"] = "a new scenario"
    client.put("/api/v1/characters/Abbie_0d162f5f.png", json={"card": card})

    chunks = pngtools.read_text_chunks(
        (populated_archive["characters"] / "Abbie_0d162f5f.png").read_bytes()
    )
    # Both spec keys, and both carrying the same payload -- the shape every
    # importer downstream reads.
    assert chunks["chara"] == chunks["ccv3"]
    envelope = json.loads(__import__("base64").b64decode(chunks["ccv3"]))
    assert envelope["spec"] == "chara_card_v3"
    assert envelope["data"]["scenario"] == "a new scenario"
    assert envelope["scenario"] == "a new scenario", "the V2 top-level mirror went missing"


def test_a_rename_does_not_move_the_file(client, populated_archive):
    """The filename is a derived label the whole app keys on, not the name."""
    card = client.get("/api/v1/characters/Abbie_0d162f5f.png").json()["card"]
    card["name"] = "Abigail"
    resp = client.put("/api/v1/characters/Abbie_0d162f5f.png", json={"card": card})

    assert resp.status_code == 200
    assert resp.json()["id"] == "Abbie_0d162f5f.png"
    assert (populated_archive["characters"] / "Abbie_0d162f5f.png").is_file()
    assert read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")["name"] == "Abigail"


def test_a_renamed_card_still_finds_its_gallery(client, populated_archive):
    """Resolution is by gallery id, so the folder keeps its old name and works."""
    card = client.get("/api/v1/characters/Abbie_0d162f5f.png").json()["card"]
    card["name"] = "Abigail"
    client.put("/api/v1/characters/Abbie_0d162f5f.png", json={"card": card})

    detail = client.get("/api/v1/characters/Abbie_0d162f5f.png").json()
    assert detail["gallery"]["exists"] is True
    assert detail["gallery"]["images"] == 2
    # The folder on disk is untouched; the card just knows where to look.
    assert detail["gallery"]["folder"] == "Abbie_kzbYR2QbpncC"
    assert (populated_archive["galleries"] / "Abbie_kzbYR2QbpncC").is_dir()

    # And the frontend, which computes the folder from the *new* name, still
    # reaches the files.
    listing = client.get("/api/v1/galleries/Abigail_kzbYR2QbpncC")
    assert listing.status_code == 200
    assert listing.json()["total"] == 2


def test_gallery_id_and_provenance_survive_a_payload_that_omits_them(client, populated_archive):
    """Losing gallery_id orphans the images with no way back; losing `jai` loses
    the source id the dedupe key is built from. Neither may go by omission."""
    resp = client.put(
        "/api/v1/characters/Abbie_0d162f5f.png",
        json={"card": {"name": "Abbie", "description": "sparse payload", "extensions": {}}},
    )
    assert resp.status_code == 200

    extensions = read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")["extensions"]
    assert extensions["gallery_id"] == "kzbYR2QbpncC"
    assert extensions["jai"]["id"] == "0d162f5f-86ab-4fdd-a2c2-3912adf24960"


def test_other_extension_keys_are_the_payloads_business(client, populated_archive):
    card = read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")
    card["extensions"] = {"chub": {"id": 7}}
    client.put("/api/v1/characters/Abbie_0d162f5f.png", json={"card": card})

    extensions = read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")["extensions"]
    assert extensions["chub"] == {"id": 7}
    assert "gallery_id" in extensions


def test_clearing_a_field_works(client, populated_archive):
    """A whole-document replace has to be able to empty something."""
    card = read_card(populated_archive["characters"] / "Bella_11112222.png")
    card["creator_notes"] = ""
    client.put("/api/v1/characters/Bella_11112222.png", json={"card": card})
    assert read_card(populated_archive["characters"] / "Bella_11112222.png")["creator_notes"] == ""


@pytest.mark.parametrize("name", ["", "   ", None, 42])
def test_a_card_must_keep_a_name(client, name):
    resp = client.put("/api/v1/characters/Abbie_0d162f5f.png", json={"card": {"name": name}})
    assert resp.status_code == 422


def test_put_to_an_unknown_card_is_404(client):
    resp = client.put("/api/v1/characters/Nobody_00000000.png", json={"card": {"name": "x"}})
    assert resp.status_code == 404


# --- optimistic concurrency -------------------------------------------------


def test_if_match_lets_a_stale_write_fail_loudly(client, populated_archive):
    stale = client.get("/api/v1/characters/Abbie_0d162f5f.png/png").headers["etag"]

    card = read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")
    card["description"] = "first writer"
    assert client.put("/api/v1/characters/Abbie_0d162f5f.png", json={"card": card}).status_code == 200

    card["description"] = "second writer, working from a stale read"
    resp = client.put(
        "/api/v1/characters/Abbie_0d162f5f.png",
        json={"card": card},
        headers={"If-Match": stale},
    )
    assert resp.status_code == 412
    assert read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")["description"] == "first writer"


def test_detail_read_carries_the_etag_a_write_checks(client, populated_archive):
    # The editor takes its `If-Match` token from the detail read, not a second
    # request -- the GET must hand out the same validator `/png` does.
    detail = client.get("/api/v1/characters/Abbie_0d162f5f.png")
    assert detail.status_code == 200
    etag = detail.headers["etag"]
    assert etag == client.get("/api/v1/characters/Abbie_0d162f5f.png/png").headers["etag"]

    card = read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")
    card["description"] = "written against the detail read's etag"
    assert (
        client.put(
            "/api/v1/characters/Abbie_0d162f5f.png",
            json={"card": card},
            headers={"If-Match": etag},
        ).status_code
        == 200
    )


def test_if_match_with_a_current_etag_goes_through(client, populated_archive):
    card = read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")
    etag = client.get("/api/v1/characters/Abbie_0d162f5f.png/png").headers["etag"]
    card["description"] = "fresh"
    resp = client.put(
        "/api/v1/characters/Abbie_0d162f5f.png",
        json={"card": card},
        headers={"If-Match": etag},
    )
    assert resp.status_code == 200


# --- favourites -------------------------------------------------------------


def test_favorite_toggle_writes_into_the_card(client, populated_archive):
    """The star goes into the PNG, not into this server's settings, so it comes
    back with the card wherever the card is exported to."""
    path = populated_archive["characters"] / "Abbie_0d162f5f.png"

    resp = client.post("/api/v1/characters/Abbie_0d162f5f.png/favorite", json={"value": True})

    assert resp.status_code == 200
    assert resp.json() == {"id": "Abbie_0d162f5f.png", "favorite": True}
    assert read_card(path)["extensions"]["fav"] is True
    assert client.get("/api/v1/characters/Abbie_0d162f5f.png").json()["favorite"] is True


def test_unstarring_writes_false_rather_than_dropping_the_key(client, populated_archive):
    """`fav: false` and no `fav` at all mean the same thing here, but a card that
    passed through SillyTavern may be starred at the envelope root too -- and
    that root copy does not survive a re-embed. Writing the explicit false keeps
    the answer in the one place that does survive."""
    client.post("/api/v1/characters/Abbie_0d162f5f.png/favorite", json={"value": True})
    resp = client.post("/api/v1/characters/Abbie_0d162f5f.png/favorite", json={"value": False})

    assert resp.json()["favorite"] is False
    card = read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")
    assert card["extensions"]["fav"] is False


def test_starring_a_card_leaves_its_pixels_alone(client, populated_archive):
    """A star is one boolean and must cost nothing. If it re-encoded, a click in
    the grid would quietly undo pngquant on the card it starred."""
    path = populated_archive["characters"] / "Abbie_0d162f5f.png"
    before = pngtools.non_text_chunks(path.read_bytes())

    client.post("/api/v1/characters/Abbie_0d162f5f.png/favorite", json={"value": True})

    assert pngtools.non_text_chunks(path.read_bytes()) == before


def test_starring_keeps_the_rest_of_the_card_intact(client, populated_archive):
    """A targeted write is only safe if it is genuinely targeted -- provenance,
    gallery link and prose all have to come through untouched."""
    path = populated_archive["characters"] / "Abbie_0d162f5f.png"
    before = read_card(path)

    client.post("/api/v1/characters/Abbie_0d162f5f.png/favorite", json={"value": True})

    after = read_card(path)
    assert after["description"] == before["description"]
    assert after["character_book"] == before["character_book"]
    assert after["extensions"]["jai"] == before["extensions"]["jai"]
    assert after["extensions"]["gallery_id"] == before["extensions"]["gallery_id"]


def test_starring_an_unknown_card_is_404(client):
    resp = client.post("/api/v1/characters/Nobody_00000000.png/favorite", json={"value": True})

    assert resp.status_code == 404


# --- deleting ---------------------------------------------------------------


def test_delete_bins_the_card_rather_than_unlinking_it(client, populated_archive, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.delete("/api/v1/characters/Cleo_33334444.png")

    assert resp.status_code == 200
    assert not (populated_archive["characters"] / "Cleo_33334444.png").exists()
    binned = Path(resp.json()["card"])
    assert binned.is_file()
    assert read_card(binned)["name"] == "Cleo"
    assert client.get("/api/v1/characters/Cleo_33334444.png").status_code == 404


def test_delete_keeps_the_gallery_by_default(client, populated_archive, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.delete("/api/v1/characters/Abbie_0d162f5f.png")
    assert resp.json()["gallery"] is None
    assert (populated_archive["galleries"] / "Abbie_kzbYR2QbpncC").is_dir()


def test_delete_can_take_the_gallery_too(client, populated_archive, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.delete("/api/v1/characters/Abbie_0d162f5f.png?gallery=delete")

    assert not (populated_archive["galleries"] / "Abbie_kzbYR2QbpncC").exists()
    binned = Path(resp.json()["gallery"])
    assert binned.is_dir()
    assert sorted(p.name for p in binned.iterdir()) == ["one.jpg", "two.jpg"]


def test_delete_takes_the_stale_thumbnail_with_it(client, populated_archive, tmp_path, monkeypatch):
    """A freed filename can be reused, and the avatar cache has no staleness
    check -- a thumb left behind would show the dead card's face on its
    replacement."""
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    assert client.get("/api/v1/characters/Cleo_33334444.png/thumb").status_code == 200
    thumb = populated_archive["thumbs"] / "avatar" / "Cleo_33334444.png"
    assert thumb.is_file()

    client.delete("/api/v1/characters/Cleo_33334444.png")
    assert not thumb.exists()


def test_binning_two_cards_of_the_same_name_keeps_both(client, archive_dirs, tmp_path, monkeypatch):
    """The bin may not be lossy -- that is the entire reason it exists."""
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    characters = archive_dirs["characters"]
    for card_id in ("Dup_11111111.png", "Dup_22222222.png"):
        (characters / card_id).write_bytes(card_png("Dup", extensions=jai_extensions()))
    client.post("/api/v1/refresh")

    first = client.delete("/api/v1/characters/Dup_11111111.png").json()["card"]
    second = client.delete("/api/v1/characters/Dup_22222222.png").json()["card"]

    assert first != second
    assert Path(first).is_file() and Path(second).is_file()


# --- bulk delete -------------------------------------------------------------


def test_bulk_delete_bins_selected_cards(client, populated_archive, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.post(
        "/api/v1/characters/bulk-delete",
        json={"ids": ["Bella_11112222.png", "Cleo_33334444.png"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(body["deleted"]) == ["Bella_11112222.png", "Cleo_33334444.png"]
    assert body["failed"] == {}

    assert not (populated_archive["characters"] / "Bella_11112222.png").exists()
    assert not (populated_archive["characters"] / "Cleo_33334444.png").exists()
    assert client.get("/api/v1/characters/Bella_11112222.png").status_code == 404


def test_bulk_delete_keeps_galleries_by_default(client, populated_archive, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.post("/api/v1/characters/bulk-delete", json={"ids": ["Abbie_0d162f5f.png"]})
    assert resp.json()["deleted"] == ["Abbie_0d162f5f.png"]
    assert (populated_archive["galleries"] / "Abbie_kzbYR2QbpncC").is_dir()


def test_bulk_delete_can_take_galleries_too(client, populated_archive, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.post(
        "/api/v1/characters/bulk-delete",
        json={"ids": ["Abbie_0d162f5f.png", "Bella_11112222.png"], "gallery": "delete"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(body["deleted"]) == ["Abbie_0d162f5f.png", "Bella_11112222.png"]
    assert not (populated_archive["galleries"] / "Abbie_kzbYR2QbpncC").exists()


def test_bulk_delete_reports_partial_failure_without_undoing_the_rest(client, populated_archive, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.post(
        "/api/v1/characters/bulk-delete",
        json={"ids": ["Abbie_0d162f5f.png", "Ghost_99999999.png"]},
    )
    body = resp.json()
    assert body["deleted"] == ["Abbie_0d162f5f.png"]
    assert "Ghost_99999999.png" in body["failed"]
    assert not (populated_archive["characters"] / "Abbie_0d162f5f.png").exists()


def test_bulk_delete_takes_stale_thumbnails_with_it(client, populated_archive, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    assert client.get("/api/v1/characters/Cleo_33334444.png/thumb").status_code == 200
    thumb = populated_archive["thumbs"] / "avatar" / "Cleo_33334444.png"
    assert thumb.is_file()

    client.post("/api/v1/characters/bulk-delete", json={"ids": ["Cleo_33334444.png"]})
    assert not thumb.exists()


# --- replacing the avatar ---------------------------------------------------


def test_avatar_replacement_changes_pixels_and_keeps_the_card(client, populated_archive):
    path = populated_archive["characters"] / "Abbie_0d162f5f.png"
    before = pngtools.non_text_chunks(path.read_bytes())

    resp = client.put(
        "/api/v1/characters/Abbie_0d162f5f.png/avatar",
        files={"image": ("new.png", image_bytes(), "image/png")},
    )
    assert resp.status_code == 200

    after_card = read_card(path)
    assert after_card["name"] == "Abbie"
    assert after_card["description"] == "Abbie is a test character."
    assert after_card["extensions"]["gallery_id"] == "kzbYR2QbpncC"
    assert pngtools.non_text_chunks(path.read_bytes()) != before, "the image did not change"


def test_avatar_replacement_drops_the_cached_thumbnail(client, populated_archive):
    """Same filename, different pixels: the cache has to be told."""
    client.get("/api/v1/characters/Abbie_0d162f5f.png/thumb")
    thumb = populated_archive["thumbs"] / "avatar" / "Abbie_0d162f5f.png"
    assert thumb.is_file()

    client.put(
        "/api/v1/characters/Abbie_0d162f5f.png/avatar",
        files={"image": ("new.png", image_bytes(), "image/png")},
    )
    assert not thumb.exists()


def test_avatar_replacement_rejects_something_that_is_not_an_image(client, populated_archive):
    path = populated_archive["characters"] / "Abbie_0d162f5f.png"
    before = path.read_bytes()
    resp = client.put(
        "/api/v1/characters/Abbie_0d162f5f.png/avatar",
        files={"image": ("not.png", b"this is not an image at all", "image/png")},
    )
    assert resp.status_code == 422
    assert path.read_bytes() == before, "a rejected upload must not touch the card"


# --- bulk tags --------------------------------------------------------------


def test_bulk_add_and_remove(client, populated_archive):
    resp = client.post(
        "/api/v1/characters/tags",
        json={
            "ids": ["Abbie_0d162f5f.png", "Bella_11112222.png"],
            "add": ["Fantasy"],
            "remove": ["Male"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["changed"] == 2

    assert read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")["tags"] == [
        "Female", "Vampire", "Fantasy",
    ]
    assert read_card(populated_archive["characters"] / "Bella_11112222.png")["tags"] == ["Fantasy"]


def test_bulk_remove_is_case_insensitive(client, populated_archive):
    """Collapsing `Female`/`female` pairs is most of what bulk tag cleanup is."""
    client.post(
        "/api/v1/characters/tags",
        json={"ids": ["Abbie_0d162f5f.png"], "remove": ["fEmAlE"]},
    )
    assert read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")["tags"] == ["Vampire"]


def test_bulk_does_not_rewrite_a_card_that_would_not_change(client, populated_archive):
    path = populated_archive["characters"] / "Abbie_0d162f5f.png"
    before = path.stat().st_mtime_ns

    resp = client.post(
        "/api/v1/characters/tags",
        json={"ids": ["Abbie_0d162f5f.png"], "add": ["Female"]},
    )
    assert resp.json() == {"changed": 0, "unchanged": 1, "failed": {}}
    assert path.stat().st_mtime_ns == before


def test_bulk_reports_partial_failure_without_undoing_the_rest(client, populated_archive):
    resp = client.post(
        "/api/v1/characters/tags",
        json={"ids": ["Abbie_0d162f5f.png", "Ghost_99999999.png"], "add": ["Tagged"]},
    )
    body = resp.json()
    assert body["changed"] == 1
    assert "Ghost_99999999.png" in body["failed"]
    assert "Tagged" in read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")["tags"]


def test_bulk_with_nothing_to_do_is_rejected(client):
    resp = client.post("/api/v1/characters/tags", json={"ids": ["Abbie_0d162f5f.png"]})
    assert resp.status_code == 422


# --- tag plan apply (corpus-wide) -------------------------------------------
#
# Different route from bulk tags above: a literal {rename, remove} plan is
# applied over every card in the archive in one pass, not a selection. Cards:
# Abbie=[Female, Vampire], Bella=[Male], Cleo=[Female].


def test_apply_renames_across_every_card_with_the_tag(client, populated_archive):
    resp = client.post("/api/v1/tags/apply", json={"rename": {"Female": "Vampiress"}})
    assert resp.status_code == 200
    assert resp.json()["changed"] == 2  # Abbie and Cleo, not Bella

    assert read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")["tags"] == [
        "Vampiress", "Vampire",
    ]
    assert read_card(populated_archive["characters"] / "Cleo_33334444.png")["tags"] == ["Vampiress"]
    assert read_card(populated_archive["characters"] / "Bella_11112222.png")["tags"] == ["Male"]


def test_apply_removes_a_tag(client, populated_archive):
    resp = client.post("/api/v1/tags/apply", json={"remove": ["Vampire"]})
    assert resp.status_code == 200
    assert resp.json()["changed"] == 1
    assert read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")["tags"] == ["Female"]


def test_apply_matches_literally_not_case_insensitively(client, populated_archive):
    """Unlike bulk remove, apply is a resolved plan -- literal string equality,
    no casing normalization of its own. See docs/PHASE_5_TAGS_PLAN.md §5."""
    resp = client.post("/api/v1/tags/apply", json={"remove": ["female"]})
    assert resp.status_code == 200
    assert resp.json() == {"changed": 0, "unchanged": 3, "failed": {}}


def test_apply_dedupes_two_tags_renaming_onto_the_same_canonical(client, populated_archive):
    """Abbie carries [Female, Vampire]; renaming both onto "Undead" must
    collapse to a single tag, not two identical ones."""
    resp = client.post(
        "/api/v1/tags/apply",
        json={"rename": {"Female": "Undead", "Vampire": "Undead"}},
    )
    assert resp.status_code == 200
    assert read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")["tags"] == ["Undead"]


def test_apply_dedupes_a_rename_landing_on_a_tag_already_present(client, populated_archive):
    """Abbie carries [Female, Vampire]; renaming Female -> Vampire must not
    produce a duplicate."""
    resp = client.post("/api/v1/tags/apply", json={"rename": {"Female": "Vampire"}})
    assert resp.status_code == 200
    assert read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")["tags"] == ["Vampire"]


def test_apply_does_not_rewrite_cards_the_plan_does_not_touch(client, populated_archive):
    path = populated_archive["characters"] / "Bella_11112222.png"
    before = path.stat().st_mtime_ns

    resp = client.post("/api/v1/tags/apply", json={"remove": ["Female"]})
    assert resp.json()["unchanged"] == 1  # Bella, untouched
    assert path.stat().st_mtime_ns == before


def test_apply_is_idempotent(client, populated_archive):
    plan = {"rename": {"Female": "Vampiress"}}
    first = client.post("/api/v1/tags/apply", json=plan).json()
    assert first["changed"] == 2

    second = client.post("/api/v1/tags/apply", json=plan).json()
    assert second == {"changed": 0, "unchanged": 3, "failed": {}}


def test_apply_reports_partial_failure_without_undoing_the_rest(client, populated_archive, monkeypatch):
    from aspect.cards import edit

    real_patch = edit.patch_card

    def flaky(path, data):
        if "Bella" in path.name:
            raise edit.WriteError("simulated failure")
        return real_patch(path, data)

    monkeypatch.setattr(edit, "patch_card", flaky)
    resp = client.post("/api/v1/tags/apply", json={"rename": {"Male": "Werewolf"}})
    body = resp.json()
    assert body["changed"] == 0
    assert "Bella_11112222.png" in body["failed"]


def test_apply_with_nothing_to_do_is_rejected(client):
    resp = client.post("/api/v1/tags/apply", json={})
    assert resp.status_code == 422


# --- gallery writes ---------------------------------------------------------


def test_upload_lands_in_the_folder_as_webp_and_is_listed(client, populated_archive):
    """Uploads are re-encoded on the way in (docs/FORKS_AND_EXTRAS_PLAN.md §9),
    so the stem survives and the extension does not."""
    resp = client.post(
        "/api/v1/galleries/Abbie_kzbYR2QbpncC/files",
        files={"file": ("three.png", image_bytes(), "image/png")},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "three.webp"
    assert resp.json()["replaced"] is False
    assert resp.json()["path"] == "user/images/Abbie_kzbYR2QbpncC/three.webp"
    written = populated_archive["galleries"] / "Abbie_kzbYR2QbpncC" / "three.webp"
    assert written.read_bytes()[8:12] == b"WEBP"

    names = [f["name"] for f in client.get("/api/v1/galleries/Abbie_kzbYR2QbpncC").json()["items"]]
    assert names == ["one.jpg", "three.webp", "two.jpg"]


def test_the_url_an_upload_returns_actually_fetches_the_file(client):
    """It is a URL in the contract, so it has to resolve -- the `/files/`
    segment is easy to leave out and yields a 404 nothing would notice."""
    written = client.post(
        "/api/v1/galleries/Abbie_kzbYR2QbpncC/files",
        files={"file": ("three.png", image_bytes(), "image/png")},
    ).json()
    fetched = client.get(written["url"])
    assert fetched.status_code == 200
    assert fetched.content[8:12] == b"WEBP"


def test_upload_creates_a_gallery_that_did_not_exist(client, populated_archive):
    resp = client.post(
        "/api/v1/galleries/Cleo_CCCCCCCCCCCC/files",
        files={"file": ("first.png", image_bytes(), "image/png")},
    )
    assert resp.status_code == 201
    assert (populated_archive["galleries"] / "Cleo_CCCCCCCCCCCC" / "first.webp").is_file()


def test_upload_after_a_rename_goes_to_the_existing_folder(client, populated_archive):
    """Not a second folder under the new name -- that is how galleries split."""
    card = read_card(populated_archive["characters"] / "Abbie_0d162f5f.png")
    card["name"] = "Abigail"
    client.put("/api/v1/characters/Abbie_0d162f5f.png", json={"card": card})

    resp = client.post(
        "/api/v1/galleries/Abigail_kzbYR2QbpncC/files",
        files={"file": ("three.png", image_bytes(), "image/png")},
    )
    assert resp.json()["folder"] == "Abbie_kzbYR2QbpncC"
    assert not (populated_archive["galleries"] / "Abigail_kzbYR2QbpncC").exists()
    assert (populated_archive["galleries"] / "Abbie_kzbYR2QbpncC" / "three.webp").is_file()


def test_deleting_a_gallery_file_bins_it(client, populated_archive, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.delete("/api/v1/galleries/Abbie_kzbYR2QbpncC/files/one.jpg")

    assert resp.status_code == 204
    assert not (populated_archive["galleries"] / "Abbie_kzbYR2QbpncC" / "one.jpg").exists()
    assert list((tmp_path / "trash").rglob("one.jpg"))


def test_deleting_a_gallery_file_that_is_not_there_is_404(client):
    assert client.delete("/api/v1/galleries/Abbie_kzbYR2QbpncC/files/nope.jpg").status_code == 404


@pytest.mark.parametrize("evil", ["../escape.png", "..", "a/b", "%2e%2e"])
def test_a_gallery_write_can_never_name_a_path(client, evil):
    """Folder names come from the client, so every one is a traversal attempt
    until proven otherwise -- and they are write targets now, not just reads.

    Anything but 2xx is a pass here: a separator or a `..` either fails to match
    the route at all (405/404, the request never reaches a handler) or is
    rejected by `_safe_child` (400). What must not happen is a write landing.
    """
    assert not client.delete(f"/api/v1/galleries/{evil}/files/one.jpg").is_success
    assert not client.post(
        f"/api/v1/galleries/{evil}/files",
        files={"file": ("x.png", image_bytes(), "image/png")},
    ).is_success


def test_uploading_cannot_write_outside_its_folder(client, populated_archive, tmp_path):
    """A filename with a path in it must be reduced to its last component."""
    client.post(
        "/api/v1/galleries/Abbie_kzbYR2QbpncC/files",
        files={"file": ("../../escaped.png", image_bytes(), "image/png")},
    )
    assert not (populated_archive["galleries"].parent / "escaped.webp").exists()
    assert not (settings.archive_dir / "escaped.webp").exists()


# --- re-fetching the original avatar -----------------------------------------


@pytest.fixture
def thumb_card(client, populated_archive):
    """A Chub card saved with the 200px thumbnail, as Discover did until 2026-10-16."""
    extensions = jai_extensions("7980518", gallery_id="MMMMMMMMMMMM", source_kind="chub_core")
    extensions["chub"] = {"id": 7980518, "full_path": "Drivai/mizuki-6f5ec0abafe8"}
    path = populated_archive["characters"] / "Mizuki_7980518.png"
    path.write_bytes(card_png("Mizuki", size=(200, 200), extensions=extensions))
    client.post("/api/v1/refresh")
    return path


def test_low_res_filter_finds_only_thumbnails(client, thumb_card):
    listed = client.get("/api/v1/characters", params={"low_res": True}).json()
    assert [c["id"] for c in listed["items"]] == ["Mizuki_7980518.png"]
    assert listed["items"][0]["avatar_width"] == 200


def test_refetch_replaces_a_thumbnail(client, thumb_card, monkeypatch):
    from aspect.cards import avatar_refetch

    urls: list[str] = []
    monkeypatch.setattr(avatar_refetch, "download", lambda url: urls.append(url) or image_bytes((600, 900)))
    detail = client.get("/api/v1/characters/Mizuki_7980518.png").json()
    assert detail["avatar_origin"] == "chub"

    resp = client.post("/api/v1/characters/Mizuki_7980518.png/avatar/refetch")
    assert resp.status_code == 200
    assert (resp.json()["avatar_width"], resp.json()["avatar_height"]) == (600, 900)
    assert urls == ["https://avatars.charhub.io/avatars/Drivai/mizuki-6f5ec0abafe8/chara_card_v2.png"]
    assert read_card(thumb_card)["name"] == "Mizuki"
    assert client.get("/api/v1/characters", params={"low_res": True}).json()["items"] == []


def test_refetch_refuses_a_thumbnail_sized_source(client, thumb_card, monkeypatch):
    from aspect.cards import avatar_refetch

    monkeypatch.setattr(avatar_refetch, "download", lambda url: image_bytes((200, 200)))
    before = thumb_card.read_bytes()
    resp = client.post("/api/v1/characters/Mizuki_7980518.png/avatar/refetch")
    assert resp.status_code == 422
    assert "thumbnail" in resp.json()["detail"]
    assert thumb_card.read_bytes() == before
