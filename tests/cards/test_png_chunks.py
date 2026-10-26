"""tEXt-chunk read/inject round-trip."""

import io

import pytest
from PIL import Image

from aspect.cards import pngtools


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (2, 2)).save(buf, "PNG")
    return buf.getvalue()


def test_read_after_inject_roundtrips():
    png = pngtools.inject_text_chunks(_png(), {"chara": "abc", "ccv3": "def"})
    assert pngtools.read_text_chunks(png) == {"chara": "abc", "ccv3": "def"}


def test_inject_replaces_stale_text_chunks():
    once = pngtools.inject_text_chunks(_png(), {"chara": "old"})
    twice = pngtools.inject_text_chunks(once, {"chara": "new"})
    assert pngtools.read_text_chunks(twice) == {"chara": "new"}


def test_read_rejects_non_png():
    with pytest.raises(ValueError):
        pngtools.read_text_chunks(b"nope")
