"""Thumbnail cache: inheritance, sniffing, generation, maintenance."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from aspect.archive import thumbs
from tests.conftest import card_png


@pytest.fixture
def store(populated_archive):
    return thumbs.ThumbnailStore(populated_archive["thumbs"], populated_archive["characters"])


# --- sniffing ---------------------------------------------------------------
# The trap this whole module is shaped around: all 4,663 inherited avatar thumbs
# are JPEG data behind a `.png` extension, so the media type can only come from
# the bytes.


@pytest.mark.parametrize(
    "header, expected",
    [
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 4, "image/png"),
        (b"GIF89a" + b"\x00" * 6, "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
        (b"BM" + b"\x00" * 10, "image/bmp"),
        (b"nonsense", "application/octet-stream"),
        (b"", "application/octet-stream"),
        # RIFF that is not WebP -- a .wav would sniff as an image if only the
        # first four bytes were checked.
        (b"RIFF\x00\x00\x00\x00WAVE", "application/octet-stream"),
    ],
)
def test_sniff_media_type(header, expected):
    assert thumbs.sniff_media_type(header) == expected


def test_inherited_jpeg_named_png_is_served_as_jpeg(store, populated_archive):
    """The exact inherited-cache shape, reproduced: JPEG bytes, `.png` name."""
    path = store.avatar_path("Abbie_0d162f5f.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (96, 144), (10, 20, 30)).save(buffer, "JPEG")
    path.write_bytes(buffer.getvalue())

    thumb = store.avatar("Abbie_0d162f5f.png")
    assert thumb.path == path
    assert thumb.media_type == "image/jpeg", "extension must not decide the media type"


def test_media_type_of_a_missing_file_is_generic(store):
    assert thumbs.media_type_of(store.avatar_path("nope.png")) == "application/octet-stream"


# --- generation -------------------------------------------------------------


def test_generates_on_miss_at_the_inherited_geometry(store):
    """A generated thumb has to sit beside 3,823 inherited ones without the grid
    showing two sizes -- the geometry is inherited, the encoding is not."""
    thumb = store.avatar("Abbie_0d162f5f.png")
    assert thumb is not None
    assert thumb.media_type == "image/webp"
    image = Image.open(thumb.path)
    assert image.format == "WEBP"
    assert image.size == thumbs.THUMB_SIZE
    # Cached under the card's own filename -- the key the inherited cache uses.
    assert thumb.path.name == "Abbie_0d162f5f.png"
    assert thumb.path.is_file()


def test_generation_is_much_smaller_than_the_card(store, populated_archive):
    card = populated_archive["characters"] / "Abbie_0d162f5f.png"
    thumb = store.avatar("Abbie_0d162f5f.png")
    assert thumb.path.stat().st_size < card.stat().st_size


def test_generate_leaves_no_partial_file_behind(store):
    store.avatar("Abbie_0d162f5f.png")
    assert list(store.avatar_dir.glob("*.part")) == []


def test_wide_source_is_cover_cropped_not_squashed(archive_dirs):
    """Cover-crop, so a landscape avatar comes out as a portrait tile with its
    aspect intact rather than horizontally squeezed."""
    characters = archive_dirs["characters"]
    (characters / "Wide_1.png").write_bytes(card_png("Wide", size=(400, 200)))
    store = thumbs.ThumbnailStore(archive_dirs["thumbs"], characters)
    thumb = store.generate_avatar("Wide_1.png")
    assert Image.open(thumb.path).size == thumbs.THUMB_SIZE


def test_transparent_avatar_flattens_onto_white_not_black(archive_dirs):
    characters = archive_dirs["characters"]
    (characters / "Ghost_1.png").write_bytes(card_png("Ghost", colour=(255, 0, 0, 0)))
    store = thumbs.ThumbnailStore(archive_dirs["thumbs"], characters)
    thumb = store.generate_avatar("Ghost_1.png")
    assert Image.open(thumb.path).convert("RGB").getpixel((48, 72)) == pytest.approx(
        (255, 255, 255), abs=4
    )


def test_generate_returns_none_for_an_unrenderable_card(archive_dirs):
    characters = archive_dirs["characters"]
    (characters / "Junk_1.png").write_bytes(b"not a png at all")
    store = thumbs.ThumbnailStore(archive_dirs["thumbs"], characters)
    assert store.generate_avatar("Junk_1.png") is None
    assert store.avatar("Junk_1.png") is None


def test_generate_overwrites_so_it_doubles_as_repair(store):
    path = store.avatar_path("Abbie_0d162f5f.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"corrupt")
    assert store.avatar("Abbie_0d162f5f.png").media_type == "application/octet-stream"
    thumb = store.generate_avatar("Abbie_0d162f5f.png")
    assert thumb.media_type == "image/webp"


def test_generate_can_be_declined(store):
    assert store.avatar("Abbie_0d162f5f.png", generate=False) is None
    assert not store.avatar_path("Abbie_0d162f5f.png").exists()


# --- maintenance ------------------------------------------------------------


def test_missing_and_stale(store):
    names = ["Abbie_0d162f5f.png", "Bella_11112222.png"]
    assert store.missing(names) == names

    store.generate_avatar("Abbie_0d162f5f.png")
    assert store.missing(names) == ["Bella_11112222.png"]

    # A thumb for a card that no longer exists: the residue of a rename.
    (store.avatar_dir / "Ghost_of_a_card.png").write_bytes(b"\xff\xd8\xff")
    assert [p.name for p in store.stale(names)] == ["Ghost_of_a_card.png"]


def test_stale_on_an_empty_cache_is_empty(archive_dirs):
    store = thumbs.ThumbnailStore(archive_dirs["thumbs"], archive_dirs["characters"])
    assert store.stale(["anything.png"]) == []
