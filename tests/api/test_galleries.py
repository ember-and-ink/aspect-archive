"""The gallery half of `/api/v1`: folders, files, images and their thumbnails.

The browse UI reaches these with folder and file names taken straight off a
card, so the tests here care about two things the character endpoints never had
to: names that survive a URL round trip, and names that are trying to escape the
gallery root.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from aspect.archive import thumbs
from aspect.config import settings
from aspect.media import manifest as media_manifest


def real_jpeg(size: tuple[int, int] = (800, 600)) -> bytes:
    """An image Pillow can actually open. The `populated_archive` fixture's
    gallery files are three magic bytes and padding -- enough to sniff a media
    type from, not enough to resize -- so a thumbnail test has to bring its
    own."""
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 90)).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def gallery_dir(populated_archive: dict[str, Path]) -> Path:
    return populated_archive["galleries"] / "Abbie_kzbYR2QbpncC"


def test_folders_name_the_card_that_claims_them(client):
    folders = client.get("/api/v1/galleries").json()
    assert folders == [{"folder": "Abbie_kzbYR2QbpncC", "card_ids": ["Abbie_0d162f5f.png"]}]


def test_a_folder_no_card_computes_is_reported_as_an_orphan(client, populated_archive):
    """A rename leaves the folder behind under the old name, and nothing looks
    for it there. That is the whole of `scripts/repair_galleries.py`, and it
    starts with being able to see them."""
    (populated_archive["galleries"] / "Renamed_zzzzzzzzzzzz").mkdir()

    folders = client.get("/api/v1/galleries").json()
    orphans = [f["folder"] for f in folders if not f["card_ids"]]
    assert orphans == ["Renamed_zzzzzzzzzzzz"]


def test_files_carry_their_kind_and_a_usable_url(client):
    listing = client.get("/api/v1/galleries/Abbie_kzbYR2QbpncC").json()

    assert listing["total"] == 2
    assert listing["bytes"] == 103 + 203
    names = [f["name"] for f in listing["items"]]
    assert names == ["one.jpg", "two.jpg"]
    first = listing["items"][0]
    assert first["kind"] == "image"
    # The client must never build a gallery path itself.
    assert client.get(first["url"]).status_code == 200


def test_video_and_audio_are_listed_but_offered_no_thumbnail(client, gallery_dir):
    """A gallery holds more than images. They belong in the listing -- they are
    the character's media -- but promising a thumbnail for an mp4 means the
    client requests one for every video and gets an error for every video."""
    (gallery_dir / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (gallery_dir / "voice.mp3").write_bytes(b"ID3\x03\x00")
    (gallery_dir / "notes.txt").write_text("not media")

    items = {f["name"]: f for f in client.get("/api/v1/galleries/Abbie_kzbYR2QbpncC").json()["items"]}

    assert items["clip.mp4"]["kind"] == "video"
    assert items["voice.mp3"]["kind"] == "audio"
    assert items["notes.txt"]["kind"] == "other"
    assert items["clip.mp4"]["thumb_url"] is None
    assert items["one.jpg"]["thumb_url"] is not None


def test_a_missing_folder_is_a_404_and_is_not_created(client, populated_archive):
    """SillyTavern's /api/images/list mkdir'd the folder it was asked about, so
    a client could never tell an empty gallery from a missing one -- and every
    probe littered the images directory. This 404s and leaves the disk alone."""
    assert client.get("/api/v1/galleries/Nobody_xxxxxxxxxxxx").status_code == 404
    assert not (populated_archive["galleries"] / "Nobody_xxxxxxxxxxxx").exists()


def test_a_folder_name_with_spaces_and_punctuation_round_trips(client, populated_archive):
    """Real folder names include `A Mother's Claim, A Daughter's Hunger_7urd...`
    -- the gallery convention sanitizes only Windows' reserved set, so commas,
    apostrophes and spaces all survive into the URL."""
    folder = "A Mother's Claim, A Daughter's Hunger_7urduYOFlyNU"
    (populated_archive["galleries"] / folder).mkdir()
    (populated_archive["galleries"] / folder / "a b.jpg").write_bytes(real_jpeg())

    listing = client.get(f"/api/v1/galleries/{folder}").json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert client.get(item["url"]).status_code == 200
    assert client.get(item["thumb_url"]).status_code == 200


def test_an_image_is_served_with_the_media_type_its_bytes_claim(client, gallery_dir):
    (gallery_dir / "shot.png").write_bytes(real_jpeg())  # JPEG bytes, .png name

    resp = client.get("/api/v1/galleries/Abbie_kzbYR2QbpncC/files/shot.png")
    assert resp.status_code == 200
    # Sniffed, never derived from the extension: the inherited caches are full
    # of exactly this mismatch.
    assert resp.headers["content-type"] == "image/jpeg"


def test_a_thumbnail_is_generated_on_miss_and_cached(client, gallery_dir, populated_archive):
    (gallery_dir / "big.jpg").write_bytes(real_jpeg((1600, 1200)))

    url = "/api/v1/galleries/Abbie_kzbYR2QbpncC/files/big.jpg/thumb"
    resp = client.get(url)
    assert resp.status_code == 200
    assert len(resp.content) < len(real_jpeg((1600, 1200)))

    cached = populated_archive["thumbs"] / "gallery" / "Abbie_kzbYR2QbpncC" / "big.jpg_288.webp"
    assert cached.is_file(), "CharacterLibrary's cache layout, with WebP's own extension"
    assert client.get(url).content == resp.content


def test_a_gallery_thumbnail_is_cover_cropped_to_the_square_tile(client, gallery_dir):
    """The tile it lands in is `aspect-ratio: 1` with `object-fit: cover`, so a
    fitted thumb would just be cropped again by the browser -- shipping an edge
    that is decoded and thrown away, and sizing the image off its long edge when
    the tile renders from its short one."""
    (gallery_dir / "wide.jpg").write_bytes(real_jpeg((1600, 400)))

    resp = client.get("/api/v1/galleries/Abbie_kzbYR2QbpncC/files/wide.jpg/thumb?size=384")
    image = Image.open(io.BytesIO(resp.content))
    assert image.size == (384, 384)
    assert image.format == "WEBP"


def test_an_image_smaller_than_the_box_is_not_upscaled(client, gallery_dir):
    """No upscaling, even though the box is a square now: a 100x60 sprite gets a
    100x60 thumb rather than a blurry 384x384 one larger than the image it
    stands in for. The tile's own `object-fit: cover` finishes the job."""
    (gallery_dir / "tiny.jpg").write_bytes(real_jpeg((100, 60)))

    resp = client.get("/api/v1/galleries/Abbie_kzbYR2QbpncC/files/tiny.jpg/thumb?size=384")
    assert Image.open(io.BytesIO(resp.content)).size == (100, 60)


def test_a_file_that_cannot_be_rendered_is_refused_not_replaced_by_its_original(client, gallery_dir):
    """A gallery page asks for a hundred thumbnails at once. Falling back to the
    source is how one unrenderable file serves 4 MB where 20 KB was asked for,
    a hundred times over."""
    (gallery_dir / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 500)

    resp = client.get("/api/v1/galleries/Abbie_kzbYR2QbpncC/files/clip.mp4/thumb")
    assert resp.status_code == 415


def test_gallery_paths_cannot_escape_the_gallery_root(client, tmp_path):
    """Folder and file names arrive from the client -- they are how the frontend
    addresses images -- so every one of them is a traversal until proven
    otherwise."""
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours")

    for attempt in ("..", "../..", "%2e%2e"):
        assert client.get(f"/api/v1/galleries/{attempt}").status_code in (400, 404)

    resp = client.get("/api/v1/galleries/Abbie_kzbYR2QbpncC/files/..%2F..%2Fsecret.txt")
    assert resp.status_code in (400, 404)
    assert "not yours" not in resp.text


def test_a_gallery_image_revalidates_instead_of_resending(client, gallery_dir):
    (gallery_dir / "big.jpg").write_bytes(real_jpeg())
    url = "/api/v1/galleries/Abbie_kzbYR2QbpncC/files/big.jpg"

    first = client.get(url)
    again = client.get(url, headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.content == b""


# --- bulk delete -------------------------------------------------------------


def test_bulk_delete_bins_the_named_files(client, gallery_dir, populated_archive, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.post(
        "/api/v1/galleries/Abbie_kzbYR2QbpncC/files/bulk-delete",
        json={"names": ["one.jpg", "two.jpg"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(body["deleted"]) == ["one.jpg", "two.jpg"]
    assert body["failed"] == {}
    assert not (gallery_dir / "one.jpg").exists()
    assert not (gallery_dir / "two.jpg").exists()


def test_bulk_delete_reports_a_missing_file_without_failing_the_rest(client, gallery_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.post(
        "/api/v1/galleries/Abbie_kzbYR2QbpncC/files/bulk-delete",
        json={"names": ["one.jpg", "nope.jpg"]},
    )
    body = resp.json()
    assert body["deleted"] == ["one.jpg"]
    assert "nope.jpg" in body["failed"]
    assert not (gallery_dir / "one.jpg").exists()


def test_bulk_delete_cannot_escape_the_gallery_root(client, gallery_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    resp = client.post(
        "/api/v1/galleries/Abbie_kzbYR2QbpncC/files/bulk-delete",
        json={"names": ["../secret.txt", "one.jpg"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == ["one.jpg"]
    assert "../secret.txt" in body["failed"]


def test_bulk_delete_leaves_the_manifest_alone(client, gallery_dir, tmp_path, monkeypatch):
    """The point of the whole route: a card localize run reads `.media.json`
    to decide a card is already complete without checking the manifest's
    file entries against disk (`media_status.card_status_map`), so binning a
    file here must not touch the manifest -- that is what keeps a plain
    "Localize media" pass from re-fetching it."""
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    manifest = media_manifest.load_manifest(gallery_dir)
    media_manifest.record_saved(manifest, "https://example.com/one.jpg", "one.jpg", "deadbeef")
    media_manifest.append_run(manifest, {"at": "2026-08-30T00:00:00Z", "errors": 0})
    media_manifest.save_manifest(gallery_dir, manifest)
    before = media_manifest.load_manifest(gallery_dir)

    client.post(
        "/api/v1/galleries/Abbie_kzbYR2QbpncC/files/bulk-delete",
        json={"names": ["one.jpg"]},
    )

    after = media_manifest.load_manifest(gallery_dir)
    assert after["files"] == before["files"]
    assert after["runs"] == before["runs"]


# --- sized avatar thumbnails ------------------------------------------------


def test_the_default_avatar_thumb_is_the_inherited_geometry(client, populated_archive):
    resp = client.get("/api/v1/characters/Abbie_0d162f5f.png/thumb")
    assert resp.status_code == 200
    assert Image.open(io.BytesIO(resp.content)).size == thumbs.THUMB_SIZE
    assert (populated_archive["thumbs"] / "avatar" / "Abbie_0d162f5f.png").is_file()


def test_a_larger_avatar_thumb_keeps_the_grid_aspect_and_its_own_cache(client, populated_archive):
    """The grid tile is 2:3 whatever its size, so one number is enough to ask
    for a bigger thumb. It lands in its own directory rather than beside the
    3,839 inherited files, where a mixed-size cache would be unreadable."""
    resp = client.get("/api/v1/characters/Abbie_0d162f5f.png/thumb?size=512")
    assert resp.status_code == 200
    assert Image.open(io.BytesIO(resp.content)).size == (341, 512)

    assert (populated_archive["thumbs"] / "avatar_512" / "Abbie_0d162f5f.png").is_file()
    assert (populated_archive["thumbs"] / "avatar" / "Abbie_0d162f5f.png").exists() is False, (
        "asking for a large thumb must not populate the inherited cache with it"
    )


def test_an_absurd_thumb_size_is_rejected_rather_than_rendered(client):
    """The size is a cache key as well as a geometry: an unbounded one is an
    unbounded number of directories, filled by anyone who can type a URL."""
    assert client.get("/api/v1/characters/Abbie_0d162f5f.png/thumb?size=99999").status_code == 422
    assert client.get("/api/v1/characters/Abbie_0d162f5f.png/thumb?size=1").status_code == 422
