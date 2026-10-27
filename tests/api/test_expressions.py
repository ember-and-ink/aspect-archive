"""`/api/v1/expressions` -- the same list/get/thumb/upload/delete routes
`/api/v1/galleries` serves, mounted a second time over `data/expressions/`
(`aspect.api.v1.galleries.register_folder_routes`).

`tests/api/test_galleries.py` already proves the shared route logic works --
listing, file serving, thumbnailing, path-traversal rejection, revalidation.
This file only needs to prove three things specific to having *two* mounts:
the expressions mount actually works, it never reads or writes the gallery
root (or vice versa) even though a character can have both under the same
folder name, and the two expressions-only routes -- the detail view's
`expressions` field and the zip exports.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from aspect.config import settings


def sprite_bytes(fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    mode = "RGB" if fmt == "JPEG" else "RGBA"
    Image.new(mode, (24, 24), (200, 30, 30)).save(buffer, fmt)
    return buffer.getvalue()


@pytest.fixture
def expressions_dir(populated_archive: dict[str, Path]) -> Path:
    return populated_archive["expressions"] / "Abbie_kzbYR2QbpncC"


def test_folders_are_claimed_the_same_way_as_galleries(client):
    folders = client.get("/api/v1/expressions").json()
    assert folders == [{"folder": "Abbie_kzbYR2QbpncC", "card_ids": ["Abbie_0d162f5f.png"]}]


def test_files_are_listed_independently_of_the_gallery(client):
    listing = client.get("/api/v1/expressions/Abbie_kzbYR2QbpncC").json()
    names = sorted(f["name"] for f in listing["items"])
    assert names == ["joy-_00001_.webp", "neutral-_00001_.webp"]
    # The gallery folder of the same name carries different files -- the two
    # roots must never bleed into each other.
    gallery_listing = client.get("/api/v1/galleries/Abbie_kzbYR2QbpncC").json()
    assert sorted(f["name"] for f in gallery_listing["items"]) == ["one.jpg", "two.jpg"]


def test_a_missing_expressions_folder_is_a_404_and_is_not_created(client, populated_archive):
    assert client.get("/api/v1/expressions/Nobody_xxxxxxxxxxxx").status_code == 404
    assert not (populated_archive["expressions"] / "Nobody_xxxxxxxxxxxx").exists()


def test_upload_and_delete_round_trip_without_touching_the_gallery_cache(
    client, expressions_dir, populated_archive, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.post(
        "/api/v1/expressions/Abbie_kzbYR2QbpncC/files",
        files={"file": ("admiration-_00001_.webp", sprite_bytes("WEBP"), "image/webp")},
    )
    assert resp.status_code == 201
    assert (expressions_dir / "admiration-_00001_.webp").is_file()

    resp = client.delete("/api/v1/expressions/Abbie_kzbYR2QbpncC/files/admiration-_00001_.webp")
    assert resp.status_code == 204
    assert not (expressions_dir / "admiration-_00001_.webp").is_file()
    # Never binned into the gallery's trash path or thumb cache.
    assert (populated_archive["galleries"] / "Abbie_kzbYR2QbpncC" / "one.jpg").is_file()


def test_expression_thumbnails_cache_under_their_own_root(client, expressions_dir, populated_archive):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (400, 400), (10, 20, 30)).save(buffer, "JPEG")
    (expressions_dir / "big.jpg").write_bytes(buffer.getvalue())

    resp = client.get("/api/v1/expressions/Abbie_kzbYR2QbpncC/files/big.jpg/thumb")
    assert resp.status_code == 200
    cached = populated_archive["thumbs"] / "expression" / "Abbie_kzbYR2QbpncC" / "big.jpg_288.webp"
    assert cached.is_file()
    assert not (populated_archive["thumbs"] / "gallery" / "Abbie_kzbYR2QbpncC" / "big.jpg_288.webp").exists()


def test_expression_paths_cannot_escape_the_expressions_root(client):
    for attempt in ("..", "../..", "%2e%2e"):
        assert client.get(f"/api/v1/expressions/{attempt}").status_code in (400, 404)


# --- the detail view ---------------------------------------------------------


def test_card_detail_reports_the_expressions_folder_alongside_the_gallery(client):
    detail = client.get("/api/v1/characters/Abbie_0d162f5f.png").json()
    assert detail["expressions"]["exists"] is True
    assert detail["expressions"]["folder"] == "Abbie_kzbYR2QbpncC"
    assert detail["expressions"]["images"] == 2
    assert detail["gallery"]["exists"] is True
    # The client must never build this path itself -- see test_galleries.py.
    assert detail["expressions_zip_url"] == "/api/v1/characters/Abbie_0d162f5f.png/expressions.zip"
    assert client.get(detail["expressions_zip_url"]).status_code == 200


def test_card_detail_reports_no_expressions_folder_for_a_card_without_one(client):
    detail = client.get("/api/v1/characters/Bella_11112222.png").json()
    assert detail["expressions"]["exists"] is False
    assert detail["expressions"]["images"] == 0
    assert detail["expressions_zip_url"] is None


# --- zip export ---------------------------------------------------------------


def test_single_character_zip_is_flat_at_the_root(client):
    import io
    import zipfile

    resp = client.get("/api/v1/characters/Abbie_0d162f5f.png/expressions.zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    names = sorted(zipfile.ZipFile(io.BytesIO(resp.content)).namelist())
    assert names == ["joy-_00001_.webp", "neutral-_00001_.webp"]


def test_single_character_zip_404s_for_a_card_with_no_expressions(client):
    resp = client.get("/api/v1/characters/Bella_11112222.png/expressions.zip")
    assert resp.status_code == 404


def test_many_character_zip_nests_under_the_on_disk_folder_name(client, populated_archive):
    import io
    import zipfile

    other = populated_archive["expressions"] / "Cleo_CCCCCCCCCCCC"
    other.mkdir()
    (other / "neutral-_00001_.webp").write_bytes(b"\xff\xd8\xff" + b"2" * 40)

    resp = client.get(
        "/api/v1/expressions/export.zip",
        params=[("id", "Abbie_0d162f5f.png"), ("id", "Cleo_33334444.png")],
    )
    assert resp.status_code == 200
    names = sorted(zipfile.ZipFile(io.BytesIO(resp.content)).namelist())
    assert names == [
        "Abbie_kzbYR2QbpncC/joy-_00001_.webp",
        "Abbie_kzbYR2QbpncC/neutral-_00001_.webp",
        "Cleo_CCCCCCCCCCCC/neutral-_00001_.webp",
    ]


def test_many_character_zip_skips_ids_with_no_expressions_folder(client):
    """Bella has no expressions folder at all -- she is silently left out
    rather than failing the whole export, the same "broken-not-skipped"
    posture the archive index takes elsewhere, applied to a request that asks
    for several characters at once and gets what exists."""
    import io
    import zipfile

    resp = client.get(
        "/api/v1/expressions/export.zip",
        params=[("id", "Abbie_0d162f5f.png"), ("id", "Bella_11112222.png")],
    )
    assert resp.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert all(n.startswith("Abbie_kzbYR2QbpncC/") for n in names)


def test_many_character_zip_404s_when_nothing_has_expressions(client):
    resp = client.get("/api/v1/expressions/export.zip", params=[("id", "Bella_11112222.png")])
    assert resp.status_code == 404


def test_many_character_zip_requires_at_least_one_id(client):
    assert client.get("/api/v1/expressions/export.zip").status_code == 422


# --- ingest (docs/FORKS_AND_EXTRAS_PLAN.md §9) -------------------------------


def zip_of(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def test_a_sprite_is_stored_as_webp_under_its_original_stem(client, expressions_dir):
    """The stem is the label (ST parses it out of the filename), so only the
    extension may change on the way in."""
    resp = client.post(
        "/api/v1/expressions/Abbie_kzbYR2QbpncC/files",
        files={"file": ("Grief-_00007_.png", sprite_bytes(), "image/png")},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "Grief-_00007_.webp"
    assert (expressions_dir / "Grief-_00007_.webp").read_bytes()[8:12] == b"WEBP"


def test_a_sprite_whose_label_is_not_one_of_the_28_is_refused(client, expressions_dir):
    resp = client.post(
        "/api/v1/expressions/Abbie_kzbYR2QbpncC/files",
        files={"file": ("smugness.png", sprite_bytes(), "image/png")},
    )
    assert resp.status_code == 422
    assert "smugness" in resp.json()["detail"]
    assert not (expressions_dir / "smugness.webp").exists()


def test_the_same_label_rule_does_not_apply_to_galleries(client, populated_archive):
    resp = client.post(
        "/api/v1/galleries/Abbie_kzbYR2QbpncC/files",
        files={"file": ("smugness.png", sprite_bytes(), "image/png")},
    )
    assert resp.status_code == 201
    assert (populated_archive["galleries"] / "Abbie_kzbYR2QbpncC" / "smugness.webp").is_file()


def test_a_refused_upload_does_not_bring_a_folder_into_existence(client, populated_archive):
    resp = client.post(
        "/api/v1/expressions/Cleo_CCCCCCCCCCCC/files",
        files={"file": ("smugness.png", sprite_bytes(), "image/png")},
    )
    assert resp.status_code == 422
    assert not (populated_archive["expressions"] / "Cleo_CCCCCCCCCCCC").exists()


def test_a_zip_writes_what_it_can_and_names_what_it_skipped(client, expressions_dir):
    body = zip_of(
        {
            "anger-_00001_.png": sprite_bytes(),
            "sprites/relief.jpg": sprite_bytes("JPEG"),
            "smugness.png": sprite_bytes(),
            "notes.txt": b"not an image at all",
        }
    )
    resp = client.post(
        "/api/v1/expressions/Abbie_kzbYR2QbpncC/zip",
        files={"file": ("pack.zip", body, "application/zip")},
    )
    assert resp.status_code == 201
    result = resp.json()
    assert sorted(w["name"] for w in result["written"]) == ["anger-_00001_.webp", "relief.webp"]
    assert sorted(s["name"] for s in result["skipped"]) == ["notes.txt", "smugness.png"]
    # Nested entries flatten to their basename, exactly as ST's importer does.
    assert (expressions_dir / "relief.webp").is_file()
    assert not (expressions_dir / "sprites").exists()


def test_a_zip_round_trips_through_the_single_character_export(client, expressions_dir):
    """What the export writes, the import takes back -- flat basenames, every
    label already valid, so the second pass is a pure overwrite."""
    for existing in expressions_dir.iterdir():
        existing.write_bytes(sprite_bytes("WEBP"))
    exported = client.get("/api/v1/characters/Abbie_0d162f5f.png/expressions.zip").content
    resp = client.post(
        "/api/v1/expressions/Abbie_kzbYR2QbpncC/zip",
        files={"file": ("expressions.zip", exported, "application/zip")},
    )
    assert resp.status_code == 201
    result = resp.json()
    assert result["skipped"] == []
    assert all(w["replaced"] for w in result["written"])


def test_a_zip_that_is_not_a_zip_is_a_422(client):
    resp = client.post(
        "/api/v1/expressions/Abbie_kzbYR2QbpncC/zip",
        files={"file": ("pack.zip", b"PK not really", "application/zip")},
    )
    assert resp.status_code == 422
