"""`POST /api/v1/characters` -- adopting a card that arrives as a file.

The two behaviours worth pinning are the ones a bundle round-trip depends on: a
card of ours is re-adopted with its pixels untouched (the crop and the quantizer
are one-way, so a second pass would degrade a card we only meant to copy), and
duplicates are decided on the `_<id8>` fragment alone, so a card renamed on disk
is still recognised as itself.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from proxy.cards import pngtools
from tests.conftest import card_png, jai_extensions


def read_card(raw: bytes) -> dict:
    envelope = pngtools.read_envelope(raw)
    assert envelope is not None
    return envelope[1]


def post(client, png: bytes, name: str = "upload.png", **data):
    return client.post(
        "/api/v1/characters",
        files={"file": (name, png, "image/png")},
        data=data,
    )


# --- a foreign card ---------------------------------------------------------


def test_a_plain_png_card_is_adopted_and_stamped(client, archive_dirs):
    resp = post(client, card_png("Nadia", creator="someone"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "png"
    assert body["duplicate"] is False
    assert body["name"] == "Nadia"

    path = archive_dirs["characters"] / body["id"]
    assert path.exists()
    jai = read_card(path.read_bytes())["extensions"]["jai"]
    assert jai["sourceKind"] == "png_import"
    # Every card in the archive carries a fragment -- it is the dedupe key, and
    # a card without one can never be recognised again.
    assert body["id"].endswith(f"_{jai['id'][:8]}.png")


def test_the_same_file_twice_is_one_card(client):
    png = card_png("Nadia", creator="someone")
    first = post(client, png).json()
    second = post(client, png).json()
    assert second["duplicate"] is True
    assert second["id"] == first["id"]


def test_a_chub_card_keeps_its_id_and_is_stamped_chub(client, archive_dirs):
    png = card_png(
        "Rook",
        extensions={"chub": {"id": 5655941, "full_path": "alice/rook", "pageName": "Rook"}},
    )
    body = post(client, png).json()
    assert body["source"] == "chub"
    assert body["id"] == "Rook_5655941.png"
    jai = read_card((archive_dirs["characters"] / body["id"]).read_bytes())["extensions"]["jai"]
    assert jai["sourceKind"] == "chub_import"
    assert jai["source_url"] == "https://chub.ai/characters/alice/rook"


def test_macros_are_sanitized_on_the_way_in(client, archive_dirs):
    """A foreign card gets intake's full text treatment -- the same repairs
    (broken brackets, JanitorAI pronoun macros) the build routes apply, with the
    same unresolved-macro warnings reported back."""
    body = post(
        client, card_png("Mac", description="Hi {char}, {{poss}} friend. {{nonsense}}")
    ).json()
    data = read_card((archive_dirs["characters"] / body["id"]).read_bytes())
    assert data["description"] == "Hi {{char}}, {{user}} friend. {{nonsense}}"
    assert body["warnings"] == ["unresolved macro: {{nonsense}}"]


def test_a_file_with_no_card_is_refused(client):
    buffer = io.BytesIO()
    Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(buffer, "PNG")
    resp = post(client, buffer.getvalue())
    assert resp.status_code == 422
    assert "no character card" in resp.json()["detail"]


def test_a_nameless_card_is_refused(client):
    resp = post(client, card_png(""))
    assert resp.status_code == 422


# --- a card of ours ---------------------------------------------------------


def test_re_adopting_our_own_card_touches_neither_text_nor_pixels(client, archive_dirs):
    png = card_png(
        "Zara",
        creator="KornyPony",
        description="Hi {{char}}",  # already sanitized on the way in the first time
        extensions=jai_extensions("aaaa1111-0000-0000-0000-000000000000"),
    )
    body = post(client, png).json()
    assert body["source"] == "archive"
    written = (archive_dirs["characters"] / body["id"]).read_bytes()

    # Pixels byte-for-byte: no re-crop, no second quantization pass.
    assert pngtools.non_text_chunks(written) == pngtools.non_text_chunks(png)
    # And the definition carried over verbatim, macros and all -- a card of ours
    # was cleaned when it was built, and a second clean is a second opinion.
    assert read_card(written)["description"] == "Hi {{char}}"


def test_a_duplicate_is_matched_on_the_fragment_not_the_name(client, populated_archive):
    """Abbie is already in the archive; the same card renamed on disk and with a
    different name in its fields is still the same card."""
    png = card_png("Renamed Entirely", extensions=jai_extensions())
    body = post(client, png).json()
    assert body["duplicate"] is True
    assert body["id"] == "Abbie_0d162f5f.png"


def test_overwrite_replaces_the_card_under_the_name_on_disk(client, populated_archive):
    characters: Path = populated_archive["characters"]
    png = card_png("Abbie", description="rewritten", extensions=jai_extensions())
    body = post(client, png, on_duplicate="overwrite").json()

    assert body["overwritten"] is True
    assert body["id"] == "Abbie_0d162f5f.png"
    assert read_card((characters / body["id"]).read_bytes())["description"] == "rewritten"
    # One file, not two: writing the derived name alongside the on-disk one is
    # how an archive ends up with two cards sharing a fragment.
    assert len(list(characters.glob("*_0d162f5f.png"))) == 1


# --- POST /characters/have --------------------------------------------------
# The `/api/v1` peer of `POST /existing` -- Discover's "hide cards I have" and
# its pre-import duplicate guard (docs/UI_REWRITE_PLAN.md §3.8).


def test_have_reports_ids_already_on_disk_by_fragment(client, populated_archive):
    resp = client.post(
        "/api/v1/characters/have",
        json={"ids": ["0d162f5f-86ab-4fdd-a2c2-3912adf24960", "no-such-card-id"]},
    )
    assert resp.status_code == 200
    assert resp.json()["have"] == ["0d162f5f-86ab-4fdd-a2c2-3912adf24960"]


def test_have_is_empty_for_an_empty_archive(client):
    resp = client.post("/api/v1/characters/have", json={"ids": []})
    assert resp.status_code == 200
    assert resp.json()["have"] == []


def test_have_fragments_lists_every_on_disk_fragment(client, populated_archive):
    resp = client.get("/api/v1/characters/have-fragments")
    assert resp.status_code == 200
    assert "0d162f5f" in resp.json()["fragments"]


def test_an_imported_card_is_visible_immediately(client):
    body = post(client, card_png("Fresh", creator="someone")).json()
    listed = client.get("/api/v1/characters?limit=0").json()
    assert body["id"] in [c["id"] for c in listed["items"]]


def test_an_adopted_card_queues_its_images_and_a_duplicate_does_not(client, queued_downloads):
    png = card_png("Nadia", creator="someone")
    first = post(client, png).json()
    post(client, png)
    assert queued_downloads == [first["id"]]
