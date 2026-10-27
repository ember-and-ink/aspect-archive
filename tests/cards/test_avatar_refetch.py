"""Re-fetching a card's original image.

The promise under test is the guard, not the happy path: a refetch replaces
the card's pixels only when what came back is clearly better, and in every
other case the file is byte-identical afterwards.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from aspect.cards import avatar_refetch, pngtools
from tests.conftest import card_png, jai_extensions

FULL_PATH = "Drivai/mizuki-sayama-6f5ec0abafe8"
CHUB_URL = f"https://avatars.charhub.io/avatars/{FULL_PATH}/chara_card_v2.png"


def image_bytes(size: tuple[int, int], fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 40, 90)).save(buffer, fmt)
    return buffer.getvalue()


def chub_extensions() -> dict:
    extensions = jai_extensions("7980518", source_kind="chub_core")
    extensions["chub"] = {"id": 7980518, "full_path": FULL_PATH}
    return extensions


@pytest.fixture
def thumb_card(tmp_path: Path) -> Path:
    path = tmp_path / "Mizuki_7980518.png"
    path.write_bytes(card_png("Mizuki", size=(200, 200), extensions=chub_extensions()))
    return path


def data_of(path: Path) -> dict:
    envelope = pngtools.read_envelope(path.read_bytes())
    assert envelope is not None
    return envelope[1]


def test_origin_and_url_per_source():
    chub = {"extensions": chub_extensions()}
    datacat = {"extensions": {"datacat": {"id": "b8e0b075"}, **jai_extensions()}}
    assert avatar_refetch.origin(chub) == "chub"
    assert avatar_refetch.resolve_url(chub) == CHUB_URL
    assert avatar_refetch.origin(datacat) == "datacat"
    assert avatar_refetch.origin({"extensions": jai_extensions()}) is None
    assert avatar_refetch.resolve_url({"extensions": jai_extensions()}) is None


def test_is_low_res_needs_both_sides_small():
    assert avatar_refetch.is_low_res(200, 200)
    assert not avatar_refetch.is_low_res(200, 300)
    assert not avatar_refetch.is_low_res(0, 0), "an unreadable size is not a thumbnail"


def test_refetch_replaces_the_pixels_and_keeps_the_card(thumb_card):
    before_data = data_of(thumb_card)
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return image_bytes((560, 700), "WEBP")

    result = avatar_refetch.refetch(thumb_card, fetch=fetch)

    assert fetched == [CHUB_URL]
    assert result.before == (200, 200)
    assert result.after == (560, 700)
    assert pngtools.image_size(thumb_card.read_bytes()) == (560, 700)
    assert data_of(thumb_card) == before_data


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (None, "could not download"),
        (b"<html>not found</html>", "is not an image"),
        (image_bytes((200, 200)), "no better than a thumbnail"),
    ],
)
def test_a_bad_download_leaves_the_card_untouched(thumb_card, payload, reason):
    before = thumb_card.read_bytes()
    with pytest.raises(avatar_refetch.RefetchError, match=reason):
        avatar_refetch.refetch(thumb_card, fetch=lambda url: payload)
    assert thumb_card.read_bytes() == before


def test_a_source_no_larger_than_the_card_is_refused(tmp_path):
    path = tmp_path / "Big_7980518.png"
    path.write_bytes(card_png("Big", size=(800, 1000), extensions=chub_extensions()))
    before = path.read_bytes()
    with pytest.raises(avatar_refetch.RefetchError, match="no larger"):
        avatar_refetch.refetch(path, fetch=lambda url: image_bytes((640, 800)))
    assert path.read_bytes() == before


def test_a_card_with_no_known_source_is_refused(tmp_path):
    path = tmp_path / "Nobody_12345678.png"
    path.write_bytes(card_png("Nobody", size=(200, 200), extensions=jai_extensions()))
    with pytest.raises(avatar_refetch.RefetchError, match="no original image"):
        avatar_refetch.refetch(path, fetch=lambda url: pytest.fail("nothing to fetch"))
