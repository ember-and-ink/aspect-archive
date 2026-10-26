"""proxy/media/writer.py: the server-side download pipeline for one media URL
-- guard, name-index skip, dead-ledger skip, fetch, sniff, WebP normalize,
content-hash dedupe, write, thumbnail, manifest record
(docs/PHASE_3C_PLAN.md §3, step 4).

`sniff_media` is a verbatim port of `validateMediaContent`
(web/library-sections/30-media-localization-feature.js:622-725); the magic
bytes here are exactly what that function checks, not re-derived by hand.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import struct
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from proxy.archive import thumbs
from proxy.media import manifest as media_manifest, names as media_names, writer as media_writer

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_bytes(size=(4, 4), color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _jpeg_bytes(size=(4, 4), color=(0, 255, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _animated_gif_bytes() -> bytes:
    frames = [Image.new("RGB", (4, 4), c) for c in [(255, 0, 0), (0, 255, 0)]]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buf.getvalue()


def _still_gif_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (0, 0, 255)).save(buf, "GIF")
    return buf.getvalue()


def _mp4_atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _mp4_with_handler(handler_type: bytes) -> bytes:
    hdlr_payload = b"\x00" * 8 + handler_type + b"\x00" * 4
    hdlr = _mp4_atom(b"hdlr", hdlr_payload)
    ftyp = _mp4_atom(b"ftyp", b"isom" + b"\x00" * 8)
    # Trailing padding so the scan's `pos < len - 24` boundary check (the
    # same one the JS it's ported from uses) still has room to read the
    # hdlr atom sitting right at the end of the buffer.
    return ftyp + hdlr + b"\x00" * 24


# --------------------------------------------------------------------------
# sniff_media
# --------------------------------------------------------------------------


def test_sniff_png():
    r = media_writer.sniff_media(_png_bytes(), None)
    assert r.valid and r.media_type == "image/png"


def test_sniff_jpeg():
    r = media_writer.sniff_media(_jpeg_bytes(), None)
    assert r.valid and r.media_type == "image/jpeg"


def test_sniff_gif():
    r = media_writer.sniff_media(_still_gif_bytes(), None)
    assert r.valid and r.media_type == "image/gif"


def test_sniff_webp():
    data = b"RIFF" + struct.pack("<I", 20) + b"WEBPVP8 " + b"\x00" * 12
    r = media_writer.sniff_media(data, None)
    assert r.valid and r.media_type == "image/webp"


def test_sniff_bmp():
    r = media_writer.sniff_media(b"BM" + b"\x00" * 20, None)
    assert r.valid and r.media_type == "image/bmp"


def test_sniff_mp4_audio_brand():
    data = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00" + b"\x00" * 20
    r = media_writer.sniff_media(data, None)
    assert r.valid and r.media_type == "audio/mp4"


def test_sniff_mp4_with_video_handler_is_video():
    data = _mp4_with_handler(b"vide")
    r = media_writer.sniff_media(data, None)
    assert r.valid and r.media_type == "video/mp4"


def test_sniff_mp4_with_sound_handler_is_audio():
    data = _mp4_with_handler(b"soun")
    r = media_writer.sniff_media(data, None)
    assert r.valid and r.media_type == "audio/mp4"


def test_sniff_webm():
    r = media_writer.sniff_media(b"\x1a\x45\xdf\xa3" + b"\x00" * 8, None)
    assert r.valid and r.media_type == "video/webm"


def test_sniff_mp3_id3():
    r = media_writer.sniff_media(b"ID3" + b"\x00" * 8, None)
    assert r.valid and r.media_type == "audio/mpeg"


def test_sniff_mp3_sync_word():
    r = media_writer.sniff_media(bytes([0xFF, 0xFB]) + b"\x00" * 8, None)
    assert r.valid and r.media_type == "audio/mpeg"


def test_sniff_ogg():
    r = media_writer.sniff_media(b"OggS" + b"\x00" * 8, None)
    assert r.valid and r.media_type == "audio/ogg"


def test_sniff_wav():
    data = b"RIFF" + struct.pack("<I", 20) + b"WAVEfmt " + b"\x00" * 8
    r = media_writer.sniff_media(data, None)
    assert r.valid and r.media_type == "audio/wav"


def test_sniff_flac():
    r = media_writer.sniff_media(b"fLaC" + b"\x00" * 8, None)
    assert r.valid and r.media_type == "audio/flac"


def test_sniff_svg():
    r = media_writer.sniff_media(b'<?xml version="1.0"?><svg></svg>', None)
    assert r.valid and r.media_type == "image/svg+xml"


def test_sniff_html_error_page_is_invalid():
    r = media_writer.sniff_media(b"<!DOCTYPE html><html><body>404</body></html>", None)
    assert not r.valid
    assert r.media_type == "text/html"


def test_sniff_unknown_bytes_with_media_content_type_trusts_header():
    r = media_writer.sniff_media(b"\x01\x02\x03\x04\x05\x06\x07\x08", "image/avif")
    assert r.valid and r.media_type == "image/avif"


def test_sniff_unknown_bytes_no_content_type_is_invalid():
    r = media_writer.sniff_media(b"\x01\x02\x03\x04\x05\x06\x07\x08", None)
    assert not r.valid


def test_sniff_too_small_is_invalid():
    r = media_writer.sniff_media(b"\x89PNG", None)
    assert not r.valid


# --------------------------------------------------------------------------
# extension_for / classify_failure
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "media_type,expected",
    [
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("audio/mp4", "m4a"),
        ("video/mp4", "mp4"),
    ],
)
def test_extension_for_known_types(media_type, expected):
    assert media_writer.extension_for(media_type, "https://x/y") == expected


def test_extension_for_unknown_audio_subtype_uses_subtype():
    assert media_writer.extension_for("audio/opus", "https://x/y") == "opus"


def test_extension_for_unknown_type_falls_back_to_url_suffix():
    assert media_writer.extension_for("application/octet-stream", "https://x/y/pic.bmp") == "bmp"


def test_extension_for_totally_unknown_defaults_png():
    assert media_writer.extension_for("application/octet-stream", "https://x/y/pic") == "png"


def test_classify_failure_blocked_is_permanent():
    assert media_writer.classify_failure(blocked=True, status=None, message="") is True


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 410, 451])
def test_classify_failure_permanent_statuses(status):
    assert media_writer.classify_failure(blocked=False, status=status, message="") is True


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_classify_failure_transient_statuses(status):
    assert media_writer.classify_failure(blocked=False, status=status, message="") is False


def test_classify_failure_network_error_is_transient():
    assert media_writer.classify_failure(blocked=False, status=None, message="timeout") is False


# --------------------------------------------------------------------------
# normalize_to_webp
# --------------------------------------------------------------------------


def test_normalize_still_png_becomes_webp():
    data, media_type = media_writer.normalize_to_webp(_png_bytes(), "image/png")
    assert media_type == "image/webp"
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def test_normalize_still_jpeg_becomes_webp():
    data, media_type = media_writer.normalize_to_webp(_jpeg_bytes(), "image/jpeg")
    assert media_type == "image/webp"


def test_normalize_still_gif_becomes_webp():
    data, media_type = media_writer.normalize_to_webp(_still_gif_bytes(), "image/gif")
    assert media_type == "image/webp"


def test_normalize_animated_gif_passes_through():
    original = _animated_gif_bytes()
    data, media_type = media_writer.normalize_to_webp(original, "image/gif")
    assert media_type == "image/gif"
    assert data == original


def test_normalize_audio_passes_through_untouched():
    original = b"ID3" + b"\x00" * 20
    data, media_type = media_writer.normalize_to_webp(original, "audio/mpeg")
    assert data == original and media_type == "audio/mpeg"


def test_normalize_svg_passes_through_untouched():
    original = b"<svg></svg>"
    data, media_type = media_writer.normalize_to_webp(original, "image/svg+xml")
    assert data == original and media_type == "image/svg+xml"


# --------------------------------------------------------------------------
# GalleryIndex
# --------------------------------------------------------------------------


def test_gallery_index_builds_name_and_hash_keys(tmp_path: Path):
    payload = _png_bytes()
    (tmp_path / "localized_media_1_forest_scene.webp").write_bytes(payload)
    idx = media_writer.GalleryIndex.build(tmp_path)
    assert idx.find_by_name("https://cdn.example.com/x/forest_scene.png", None, "localized_media") == (
        "localized_media_1_forest_scene.webp"
    )
    import hashlib

    assert idx.find_by_hash(hashlib.sha256(payload).hexdigest()) == "localized_media_1_forest_scene.webp"


def test_gallery_index_never_downgrades_a_higher_priority_prefix(tmp_path: Path):
    (tmp_path / "localized_media_1_thing.webp").write_bytes(_png_bytes())
    idx = media_writer.GalleryIndex.build(tmp_path)
    # A lorebook_media write for the same name must not treat the
    # higher-priority localized_media file as "not yet under my prefix" --
    # it should still report the match (never re-download), matching the
    # JS's wouldDowngrade rule.
    match = idx.find_by_name("https://cdn.example.com/x/thing.png", None, "lorebook_media")
    assert match == "localized_media_1_thing.webp"


def test_gallery_index_does_not_read_files_until_a_hash_is_needed(tmp_path: Path, monkeypatch):
    """The name index costs a scandir; the hash index costs a full read of
    every file in the folder. A run whose items all skip on the name index
    must never pay the second one."""
    (tmp_path / "extgallery_1_vF9hgQCD.webp").write_bytes(_png_bytes())

    reads = 0
    original = Path.read_bytes

    def counting_read(self):
        nonlocal reads
        reads += 1
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read)
    media_writer._digest_cache.clear()

    idx = media_writer.GalleryIndex.build(tmp_path)
    assert idx.find_by_name("mega://folder/vF9hgQCD", None, "extgallery") == "extgallery_1_vF9hgQCD.webp"
    assert reads == 0

    assert idx.by_hash  # materializing it is what reads
    assert reads == 1


def test_gallery_index_memoizes_digests_across_builds(tmp_path: Path, monkeypatch):
    payload = _png_bytes()
    (tmp_path / "extgallery_1_thing.webp").write_bytes(payload)
    media_writer._digest_cache.clear()

    digest = hashlib.sha256(payload).hexdigest()
    assert media_writer.GalleryIndex.build(tmp_path).find_by_hash(digest) == "extgallery_1_thing.webp"

    reads = 0
    original = Path.read_bytes

    def counting_read(self):
        nonlocal reads
        reads += 1
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read)
    # A second index over the same unchanged folder -- the browser-fetch door
    # builds one per item -- reuses the digests instead of re-reading.
    assert media_writer.GalleryIndex.build(tmp_path).find_by_hash(digest) == "extgallery_1_thing.webp"
    assert reads == 0


def test_gallery_index_digest_for_one_file_does_not_hash_the_folder(tmp_path: Path, monkeypatch):
    payload = _png_bytes()
    (tmp_path / "extgallery_1_wanted.webp").write_bytes(payload)
    for i in range(5):
        (tmp_path / f"extgallery_{i + 2}_other{i}.webp").write_bytes(_png_bytes() + bytes([i]))
    media_writer._digest_cache.clear()

    reads = 0
    original = Path.read_bytes

    def counting_read(self):
        nonlocal reads
        reads += 1
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read)
    idx = media_writer.GalleryIndex.build(tmp_path)
    assert idx.digest_for("extgallery_1_wanted.webp") == hashlib.sha256(payload).hexdigest()
    assert reads == 1


def test_gallery_index_re_hashes_a_file_that_changed(tmp_path: Path):
    """The digest cache keys on (path, size, mtime), so a rewritten file is
    not served from a stale digest."""
    target = tmp_path / "extgallery_1_thing.webp"
    target.write_bytes(_png_bytes())
    media_writer._digest_cache.clear()
    first = media_writer.GalleryIndex.build(tmp_path).digest_for(target.name)

    replacement = _png_bytes() + b"different"
    target.write_bytes(replacement)
    os.utime(target, (0, 0))

    assert media_writer.GalleryIndex.build(tmp_path).digest_for(target.name) == hashlib.sha256(
        replacement
    ).hexdigest()
    assert first != hashlib.sha256(replacement).hexdigest()


def test_gallery_index_ignores_non_media_files(tmp_path: Path):
    (tmp_path / media_manifest.MANIFEST_NAME).write_text("{}")
    (tmp_path / "notes.txt").write_text("hi")
    idx = media_writer.GalleryIndex.build(tmp_path)
    assert idx.by_key == {}
    assert idx.by_hash == {}


# --------------------------------------------------------------------------
# download_item -- end to end against a mocked transport
# --------------------------------------------------------------------------


@pytest.fixture
def gallery_dir(tmp_path: Path) -> Path:
    d = tmp_path / "gallery"
    d.mkdir()
    return d


@pytest.fixture
def thumbnail_store(tmp_path: Path) -> thumbs.ThumbnailStore:
    return thumbs.ThumbnailStore(root=tmp_path / "thumbs", archive_dir=tmp_path)


def _client_for(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


@pytest.mark.asyncio
async def test_download_item_saves_a_new_image(gallery_dir, thumbnail_store, monkeypatch):
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    body = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        outcome = await media_writer.download_item(
            client,
            gallery_dir,
            "gallery",
            url="https://cdn.example.com/x/photo.png",
            filename_hint=None,
            prefix="localized_media",
            index=1,
            index_state=index_state,
            manifest=manifest,
            ledger=ledger,
            thumbnail_store=thumbnail_store,
        )

    assert outcome.status == "saved"
    assert outcome.file.startswith("localized_media_1_photo")
    assert outcome.file.endswith(".webp")
    written = gallery_dir / outcome.file
    assert written.is_file()
    assert written.read_bytes()[:4] == b"RIFF"
    assert "https://cdn.example.com/x/photo.png" in manifest["files"]
    # Thumbnail generated immediately, per step 9 of the plan.
    thumb = thumbnail_store.gallery_path("gallery", outcome.file)
    assert thumb.is_file()


@pytest.mark.asyncio
async def test_download_item_404_is_permanent_and_recorded_in_manifest_and_ledger(gallery_dir, thumbnail_store, monkeypatch):
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        outcome = await media_writer.download_item(
            client,
            gallery_dir,
            "gallery",
            url="https://cdn.example.com/x/gone.png",
            filename_hint=None,
            prefix="localized_media",
            index=1,
            index_state=index_state,
            manifest=manifest,
            ledger=ledger,
            thumbnail_store=thumbnail_store,
        )

    assert outcome.status == "skipped"
    assert outcome.permanent is True
    assert "https://cdn.example.com/x/gone.png" in manifest["dead"]
    assert media_manifest.is_dead(ledger, "https://cdn.example.com/x/gone.png")


@pytest.mark.asyncio
async def test_download_item_blocked_url_never_touches_the_network(gallery_dir, thumbnail_store):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"should never be requested")

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        outcome = await media_writer.download_item(
            client,
            gallery_dir,
            "gallery",
            url="http://127.0.0.1:8000/secrets",
            filename_hint=None,
            prefix="localized_media",
            index=1,
            index_state=index_state,
            manifest=manifest,
            ledger=ledger,
            thumbnail_store=thumbnail_store,
        )

    assert outcome.status == "skipped"
    assert outcome.permanent is True
    assert calls == []
    assert "http://127.0.0.1:8000/secrets" in manifest["dead"]


@pytest.mark.asyncio
async def test_download_item_second_call_skips_by_name_index(gallery_dir, thumbnail_store, monkeypatch):
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    body = _png_bytes()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        first = await media_writer.download_item(
            client, gallery_dir, "gallery",
            url="https://cdn.example.com/x/photo.png", filename_hint=None,
            prefix="localized_media", index=1, index_state=index_state,
            manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        )
        assert first.status == "saved"

        second = await media_writer.download_item(
            client, gallery_dir, "gallery",
            url="https://other-cdn.example.com/y/photo.png", filename_hint=None,
            prefix="localized_media", index=2, index_state=index_state,
            manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        )

    assert second.status == "skipped"
    assert second.reason == "filename match"
    assert len(calls) == 1  # the second URL was never fetched

    # The skip still records the url -> file mapping, exactly as the hash-dedupe
    # branch does. Without it a card whose media all predates this pipeline
    # reports `files: 0` from /media/status forever.
    entry = manifest["files"]["https://other-cdn.example.com/y/photo.png"]
    assert entry["file"] == second.file
    assert entry["sha256"] == manifest["files"]["https://cdn.example.com/x/photo.png"]["sha256"]
    assert entry["size"] == (gallery_dir / second.file).stat().st_size


@pytest.mark.asyncio
async def test_download_item_cross_character_dead_ledger_skips_without_fetch(gallery_dir, thumbnail_store):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"nope")

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    media_manifest.record_failure(
        ledger, "https://cdn.example.com/x/gone.png", permanent=True, status=404, message="HTTP 404"
    )
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        outcome = await media_writer.download_item(
            client, gallery_dir, "gallery",
            url="https://cdn.example.com/x/gone.png", filename_hint=None,
            prefix="localized_media", index=1, index_state=index_state,
            manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        )

    assert outcome.status == "skipped"
    assert calls == []


@pytest.mark.asyncio
async def test_download_item_content_hash_dedupe_against_existing_file(gallery_dir, thumbnail_store, monkeypatch):
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    original_png = _png_bytes()
    # Pre-normalize to what the writer would have written, so the hash matches.
    webp_data, _ = media_writer.normalize_to_webp(original_png, "image/png")
    (gallery_dir / "localized_media_1_existing.webp").write_bytes(webp_data)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=original_png, headers={"content-type": "image/png"})

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        outcome = await media_writer.download_item(
            client, gallery_dir, "gallery",
            # A name that would NOT match the name index, so this exercises
            # the content-hash path specifically.
            url="https://cdn.example.com/totally/different/name.png", filename_hint=None,
            prefix="localized_media", index=5, index_state=index_state,
            manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        )

    assert outcome.status == "skipped"
    assert outcome.file == "localized_media_1_existing.webp"
    assert outcome.reason == "already have this content"


@pytest.mark.asyncio
async def test_download_item_html_error_page_is_transient_not_permanent(gallery_dir, thumbnail_store, monkeypatch):
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<!DOCTYPE html><html>error</html>", headers={"content-type": "text/html"})

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        outcome = await media_writer.download_item(
            client, gallery_dir, "gallery",
            url="https://cdn.example.com/x/errorpage.png", filename_hint=None,
            prefix="localized_media", index=1, index_state=index_state,
            manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        )

    assert outcome.status == "error"
    assert outcome.permanent is False
    assert "https://cdn.example.com/x/errorpage.png" not in manifest["dead"]


@pytest.mark.asyncio
async def test_download_item_respects_size_cap(gallery_dir, thumbnail_store, monkeypatch):
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    monkeypatch.setattr(media_writer.media_guard, "MAX_MEDIA_BYTES", 16)
    big = _png_bytes(size=(64, 64))
    assert len(big) > 16

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "image/png"})

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        outcome = await media_writer.download_item(
            client, gallery_dir, "gallery",
            url="https://cdn.example.com/x/huge.png", filename_hint=None,
            prefix="localized_media", index=1, index_state=index_state,
            manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        )

    assert outcome.status == "error"
    assert list(gallery_dir.iterdir()) == []


# --------------------------------------------------------------------------
# finish_item -- bytes already in hand, no network (the browser-fetch door:
# MEGA/Pixiv, docs/PHASE_3C_PLAN.md §6)
# --------------------------------------------------------------------------


def test_finish_item_saves_bytes_already_fetched(gallery_dir, thumbnail_store):
    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    outcome = media_writer.finish_item(
        gallery_dir,
        "gallery",
        url="mega://folder/handle#key",
        filename_hint="cool pic",
        prefix="extgallery",
        index=1,
        index_state=index_state,
        manifest=manifest,
        ledger=ledger,
        body=_png_bytes(),
        content_type="image/png",
        thumbnail_store=thumbnail_store,
    )

    assert outcome.status == "saved"
    assert outcome.file.startswith("extgallery_1_cool_pic")
    assert outcome.file.endswith(".webp")
    written = gallery_dir / outcome.file
    assert written.is_file()
    assert "mega://folder/handle#key" in manifest["files"]
    thumb = thumbnail_store.gallery_path("gallery", outcome.file)
    assert thumb.is_file()


def test_finish_item_invalid_bytes_are_an_error_not_permanent(gallery_dir, thumbnail_store):
    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    outcome = media_writer.finish_item(
        gallery_dir,
        "gallery",
        url="mega://folder/bad-handle",
        filename_hint=None,
        prefix="extgallery",
        index=1,
        index_state=index_state,
        manifest=manifest,
        ledger=ledger,
        body=b"<!DOCTYPE html><html>error</html>",
        content_type="text/html",
        thumbnail_store=thumbnail_store,
    )

    assert outcome.status == "error"
    assert list(gallery_dir.iterdir()) == []


def test_finish_item_content_hash_dedupe_against_existing_file(gallery_dir, thumbnail_store):
    existing = gallery_dir / "extgallery_0_old.webp"
    body = _png_bytes()
    normalized, _ = media_writer.normalize_to_webp(body, "image/png")
    existing.write_bytes(normalized)

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    outcome = media_writer.finish_item(
        gallery_dir,
        "gallery",
        url="mega://folder/dup-handle",
        filename_hint="dup",
        prefix="extgallery",
        index=1,
        index_state=index_state,
        manifest=manifest,
        ledger=ledger,
        body=body,
        content_type="image/png",
        thumbnail_store=thumbnail_store,
    )

    assert outcome.status == "skipped"
    assert outcome.file == "extgallery_0_old.webp"
    assert len(list(gallery_dir.iterdir())) == 1


# --------------------------------------------------------------------------
# The exact-URL manifest hit -- the skip the name index cannot make
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_item_skips_a_url_the_manifest_already_recorded(gallery_dir, thumbnail_store, monkeypatch):
    """postimg's shape: `i.postimg.cc/<id>/1.webp`. `media_key("1")` is one
    character, under MIN_KEY_LENGTH, so the name index can never hold a key
    for it -- before the manifest hit these re-fetched every run and were
    thrown away by the hash dedupe."""
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    body = _png_bytes()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    url = "https://i.postimg.cc/6w6R72kC/1.webp"
    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        first = await media_writer.download_item(
            client, gallery_dir, "gallery",
            url=url, filename_hint=None,
            prefix="localized_media", index=1, index_state=index_state,
            manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        )
        assert first.status == "saved"
        # Nothing in the name index could match it.
        assert media_names.keys_for_item(url, None) == []

        # A fresh run: new index, same manifest -- exactly a re-localize.
        rerun_index = media_writer.GalleryIndex.build(gallery_dir)
        second = await media_writer.download_item(
            client, gallery_dir, "gallery",
            url=url, filename_hint=None,
            prefix="localized_media", index=2, index_state=rerun_index,
            manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        )

    assert second.status == "skipped"
    assert second.file == first.file
    assert second.reason == "already downloaded"
    assert len(calls) == 1  # the re-run never touched the network


@pytest.mark.asyncio
async def test_download_item_refetches_when_the_recorded_file_is_gone(gallery_dir, thumbnail_store, monkeypatch):
    """A manifest entry is only trusted while its file is still on disk --
    deleting the file must bring the download back, not leave a permanent
    phantom skip."""
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    body = _png_bytes()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    url = "https://i.postimg.cc/6w6R72kC/1.webp"
    manifest = media_manifest.empty_manifest()
    ledger: dict = {}

    async with _client_for(handler) as client:
        first = await media_writer.download_item(
            client, gallery_dir, "gallery",
            url=url, filename_hint=None,
            prefix="localized_media", index=1, index_state=media_writer.GalleryIndex.build(gallery_dir),
            manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        )
        (gallery_dir / first.file).unlink()

        second = await media_writer.download_item(
            client, gallery_dir, "gallery",
            url=url, filename_hint=None,
            prefix="localized_media", index=2, index_state=media_writer.GalleryIndex.build(gallery_dir),
            manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        )

    assert second.status == "saved"
    assert len(calls) == 2


def test_manifest_hit_ignores_a_malformed_entry(gallery_dir, thumbnail_store):
    (gallery_dir / "localized_media_1_x.webp").write_bytes(b"x")
    index_state = media_writer.GalleryIndex.build(gallery_dir)
    manifest = media_manifest.empty_manifest()
    manifest["files"]["https://a/1.webp"] = "not-a-dict"
    manifest["files"]["https://b/1.webp"] = {"sha256": "x"}  # no `file`
    manifest["files"]["https://c/1.webp"] = {"file": "localized_media_1_x.webp"}

    assert media_writer.manifest_hit(manifest, index_state, "https://a/1.webp") is None
    assert media_writer.manifest_hit(manifest, index_state, "https://b/1.webp") is None
    assert media_writer.manifest_hit(manifest, index_state, "https://missing/1.webp") is None
    assert media_writer.manifest_hit(manifest, index_state, "https://c/1.webp") == "localized_media_1_x.webp"


# --------------------------------------------------------------------------
# download_batch -- concurrency, per-host bound, cancellation
# --------------------------------------------------------------------------


async def _drain(batch) -> list:
    return [outcome async for outcome in batch]


@pytest.mark.asyncio
async def test_download_batch_fetches_concurrently(gallery_dir, thumbnail_store, monkeypatch):
    """Six distinct hosts, each stalling 50ms. Serially that is 300ms; the
    batch has to overlap them or this assertion is meaningless."""
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.05)
        finally:
            in_flight -= 1
        return httpx.Response(200, content=_png_bytes(size=(4, 4 + int(request.url.path[-5]))),
                              headers={"content-type": "image/png"})

    items = [{"url": f"https://cdn{i}.example.com/photo{i}.png", "filename": None} for i in range(6)]
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        started = time.monotonic()
        outcomes = await _drain(media_writer.download_batch(
            client, gallery_dir, "gallery",
            items=items, prefix="localized_media", start_index=1,
            index_state=index_state, manifest=media_manifest.empty_manifest(), ledger={},
            thumbnail_store=thumbnail_store, concurrency=6, per_host=3,
        ))
        elapsed = time.monotonic() - started

    assert len(outcomes) == 6
    assert peak > 1
    assert elapsed < 0.25  # six 50ms fetches serially would be 0.3s


@pytest.mark.asyncio
async def test_download_batch_bounds_concurrency_per_host(gallery_dir, thumbnail_store, monkeypatch):
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.02)
        finally:
            in_flight -= 1
        return httpx.Response(200, content=_png_bytes(size=(4, 4 + int(request.url.path[-5]))),
                              headers={"content-type": "image/png"})

    items = [{"url": f"https://i.postimg.cc/abc/{i}.png", "filename": None} for i in range(8)]
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcomes = await _drain(media_writer.download_batch(
            client, gallery_dir, "gallery",
            items=items, prefix="localized_media", start_index=1,
            index_state=index_state, manifest=media_manifest.empty_manifest(), ledger={},
            thumbnail_store=thumbnail_store, concurrency=8, per_host=2,
        ))

    assert len(outcomes) == 8
    assert peak <= 2  # one host, so the per-host cap is the binding one


@pytest.mark.asyncio
async def test_download_batch_local_skips_never_wait_on_a_busy_host(gallery_dir, thumbnail_store, monkeypatch):
    """The gate wraps only the network half. A gallery that already has every
    file must drain instantly even with a per-host width of one."""
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no item should have been fetched: {request.url}")

    manifest = media_manifest.empty_manifest()
    items = []
    for i in range(20):
        name = f"localized_media_{i}_{i}.webp"
        (gallery_dir / name).write_bytes(b"x")
        url = f"https://i.postimg.cc/abc{i}/{i}.webp"
        manifest["files"][url] = {"file": name, "sha256": "x"}
        items.append({"url": url, "filename": None})
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcomes = await _drain(media_writer.download_batch(
            client, gallery_dir, "gallery",
            items=items, prefix="localized_media", start_index=1,
            index_state=index_state, manifest=manifest, ledger={},
            thumbnail_store=thumbnail_store, concurrency=2, per_host=1,
        ))

    assert len(outcomes) == 20
    assert all(o.status == "skipped" and o.reason == "already downloaded" for o in outcomes)


@pytest.mark.asyncio
async def test_download_batch_stops_handing_out_work_once_cancelled(gallery_dir, thumbnail_store, monkeypatch):
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    cancelled = False

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return httpx.Response(200, content=_png_bytes(size=(4, 4 + int(request.url.path[-5]))),
                              headers={"content-type": "image/png"})

    items = [{"url": f"https://cdn.example.com/photo{i}.png", "filename": None} for i in range(20)]
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async for outcome in media_writer.download_batch(
            client, gallery_dir, "gallery",
            items=items, prefix="localized_media", start_index=1,
            index_state=index_state, manifest=media_manifest.empty_manifest(), ledger={},
            thumbnail_store=thumbnail_store, concurrency=2, per_host=2,
            should_cancel=lambda: cancelled,
        ):
            seen.append(outcome)
            if len(seen) == 4:
                cancelled = True

    assert 4 <= len(seen) < 20


@pytest.mark.asyncio
async def test_download_batch_empty_item_list_yields_nothing(gallery_dir, thumbnail_store):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        outcomes = await _drain(media_writer.download_batch(
            client, gallery_dir, "gallery",
            items=[], prefix="localized_media", start_index=1,
            index_state=media_writer.GalleryIndex.build(gallery_dir),
            manifest=media_manifest.empty_manifest(), ledger={},
            thumbnail_store=thumbnail_store,
        ))
    assert outcomes == []


# --------------------------------------------------------------------------
# Image-only policy: audio and video are refused at both doors
# (UNSUPPORTED_EXT_RE on the URL suffix, sniffed type in `finish_item`).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/theme.mp3",
        "https://cdn.example.com/voice.WAV",
        "https://cdn.example.com/clip.webm",
        "https://cdn.example.com/scene.mp4",
        "https://cdn.example.com/song.m4a",
        "https://cdn.example.com/track.flac",
        # The suffix is on the path, so a query string must not hide it.
        "https://cdn.example.com/theme.mp3?token=abc123",
        "https://cdn.example.com/theme.mp3#t=30",
    ],
)
def test_unsupported_url_reason_refuses_audio_and_video(url):
    assert media_writer.unsupported_url_reason(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/art.png",
        "https://cdn.example.com/art.webp",
        "https://cdn.example.com/art.gif",
        "https://cdn.example.com/art.jpeg",
        "https://cdn.example.com/art.svg",
        # A dot in the *query* is not a suffix: this is an image.
        "https://cdn.example.com/art.png?format=mp4",
        # Extensionless CDN links stay eligible -- step 5b judges them instead.
        "https://cdn.example.com/9f8a7b6c",
    ],
)
def test_unsupported_url_reason_allows_images(url):
    assert media_writer.unsupported_url_reason(url) is None


@pytest.mark.asyncio
async def test_download_item_audio_url_never_touches_the_network(gallery_dir, thumbnail_store):
    """The suffix gate is the cheap one: a creator's 14MB embedded soundtrack
    must cost zero bytes, not a fetch followed by a discard."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"ID3" + b"\x00" * 64)

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        outcome = await media_writer.download_item(
            client,
            gallery_dir,
            "gallery",
            url="https://cdn.example.com/soundtrack.mp3",
            filename_hint=None,
            prefix="localized_media",
            index=1,
            index_state=index_state,
            manifest=manifest,
            ledger=ledger,
            thumbnail_store=thumbnail_store,
        )

    assert outcome.status == "skipped"
    assert outcome.permanent is True
    assert "not an image" in (outcome.reason or "")
    assert calls == []
    assert list(gallery_dir.iterdir()) == []
    # Nothing persisted: the verdict is a pure function of the URL, so a policy
    # change later needs no manifest or ledger unwinding.
    assert manifest["dead"] == {}
    assert ledger == {}


def test_finish_item_refuses_audio_bytes_from_an_innocent_url(gallery_dir, thumbnail_store):
    """Step 5b, the backstop: the URL claimed `.png`, the bytes are an MP3."""
    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    outcome = media_writer.finish_item(
        gallery_dir,
        "gallery",
        url="https://cdn.example.com/cover.png",
        filename_hint=None,
        prefix="localized_media",
        index=1,
        index_state=index_state,
        manifest=manifest,
        ledger=ledger,
        body=b"ID3" + b"\x00" * 64,
        content_type="image/png",
        thumbnail_store=thumbnail_store,
    )

    assert outcome.status == "skipped"
    assert outcome.permanent is True
    assert "not an image" in (outcome.reason or "")
    assert list(gallery_dir.iterdir()) == []
    # This one *is* persisted in both ledgers -- the URL carries no hint, so
    # without a record every run would re-fetch the bytes to re-reject them.
    assert "https://cdn.example.com/cover.png" in manifest["dead"]
    assert media_manifest.is_dead(ledger, "https://cdn.example.com/cover.png")


def test_finish_item_refuses_video_bytes(gallery_dir, thumbnail_store):
    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    outcome = media_writer.finish_item(
        gallery_dir,
        "gallery",
        url="https://cdn.example.com/asset",
        filename_hint=None,
        prefix="localized_media",
        index=1,
        index_state=media_writer.GalleryIndex.build(gallery_dir),
        manifest=manifest,
        ledger=ledger,
        body=b"\x1a\x45\xdf\xa3" + b"\x00" * 32,
        content_type=None,
        thumbnail_store=thumbnail_store,
    )
    assert outcome.status == "skipped"
    assert outcome.permanent is True
    assert list(gallery_dir.iterdir()) == []


def test_gallery_index_ignores_leftover_audio_and_video(gallery_dir):
    """MEDIA_EXT_RE is images-only now, so a stray mp3 that predates the policy
    is not indexed as gallery media."""
    (gallery_dir / "localized_media_1_song.mp3").write_bytes(b"ID3" + b"\x00" * 16)
    (gallery_dir / "localized_media_2_clip.webm").write_bytes(b"\x1a\x45\xdf\xa3")
    (gallery_dir / "localized_media_3_art.webp").write_bytes(_png_bytes())

    index_state = media_writer.GalleryIndex.build(gallery_dir)

    assert index_state._names == ["localized_media_3_art.webp"]


# ---- mega dispatch -----------------------------------------------------------
#
# A `mega://` reference isn't a fetchable URL -- these prove `download_item`
# routes it around the ordinary guard/DNS-preflight/fetch and through
# `media_mega.fetch_and_decrypt` instead, ending up through the exact same
# sniff/normalize/dedupe/write/thumbnail steps as everything else.


@pytest.mark.asyncio
async def test_download_item_mega_url_fetches_and_decrypts(gallery_dir, thumbnail_store):
    from Crypto.Cipher import AES
    from Crypto.Util import Counter

    from proxy.media import mega as media_mega

    png = _png_bytes()
    file_key = os.urandom(16)
    nonce = os.urandom(8)
    encrypted = AES.new(file_key, AES.MODE_CTR, counter=Counter.new(64, prefix=nonce, initial_value=0)).encrypt(png)
    pseudo_url = media_mega._build_pseudo_url("folderXYZ", "handle1", file_key, nonce, len(png))

    def handler(request: httpx.Request) -> httpx.Response:
        if "g.api.mega.co.nz" in str(request.url):
            return httpx.Response(200, json=[{"g": "https://gfs1.userstorage.mega.co.nz/dl/handle1"}])
        if request.url.host == "gfs1.userstorage.mega.co.nz":
            return httpx.Response(200, content=encrypted)
        return httpx.Response(404)

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        outcome = await media_writer.download_item(
            client,
            gallery_dir,
            "gallery",
            url=pseudo_url,
            filename_hint="photo.png",
            prefix="localized_media",
            index=1,
            index_state=index_state,
            manifest=manifest,
            ledger=ledger,
            thumbnail_store=thumbnail_store,
        )

    assert outcome.status == "saved"
    written = gallery_dir / outcome.file
    assert written.is_file()
    assert written.read_bytes()[:4] == b"RIFF"  # normalized to webp, like any other image
    assert pseudo_url in manifest["files"]


@pytest.mark.asyncio
async def test_download_item_mega_api_error_is_a_skip_not_a_crash(gallery_dir, thumbnail_store):
    from proxy.media import mega as media_mega

    pseudo_url = media_mega._build_pseudo_url("folderXYZ", "handle1", os.urandom(16), os.urandom(8), 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[-9])

    manifest = media_manifest.empty_manifest()
    ledger: dict = {}
    index_state = media_writer.GalleryIndex.build(gallery_dir)

    async with _client_for(handler) as client:
        outcome = await media_writer.download_item(
            client, gallery_dir, "gallery",
            url=pseudo_url, filename_hint="photo.png",
            prefix="localized_media", index=1, index_state=index_state,
            manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        )

    assert outcome.status == "error"
    assert list(gallery_dir.iterdir()) == []


# --------------------------------------------------------------------------
# download_item(retry_dead=True) -- Rescan everything / Rescan broken
# --------------------------------------------------------------------------

# A real `dead` entry, from a dev-archive card (Vespera's file.garden images).
_DEAD_URL = "https://file.garden/akviCfKE-zqjKa2t/ves_job.jpg"
_DEAD_ENTRY = {"reason": "HTTP 404", "attempts": 1, "at": "2026-08-30T10:49:12Z"}


def _dead_everywhere(*, permanent: bool = True) -> tuple[dict, dict]:
    manifest = media_manifest.empty_manifest()
    manifest["dead"][_DEAD_URL] = dict(_DEAD_ENTRY)
    ledger: dict = {}
    attempts = 1 if permanent else media_manifest.MAX_TRANSIENT_ATTEMPTS
    for _ in range(attempts):
        media_manifest.record_failure(
            ledger, _DEAD_URL, permanent=permanent, status=404 if permanent else 503, message="x"
        )
    return manifest, ledger


async def _retry(client, gallery_dir, thumbnail_store, manifest, ledger):
    return await media_writer.download_item(
        client, gallery_dir, "gallery",
        url=_DEAD_URL, filename_hint=None,
        prefix="localized_media", index=1,
        index_state=media_writer.GalleryIndex.build(gallery_dir),
        manifest=manifest, ledger=ledger, thumbnail_store=thumbnail_store,
        retry_dead=True,
    )


@pytest.mark.asyncio
async def test_retry_dead_recovers_a_link_that_works_again(gallery_dir, thumbnail_store, monkeypatch):
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    manifest, ledger = _dead_everywhere()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_png_bytes(), headers={"content-type": "image/png"})

    async with _client_for(handler) as client:
        outcome = await _retry(client, gallery_dir, thumbnail_store, manifest, ledger)

    assert outcome.status == "saved"
    assert _DEAD_URL in manifest["files"]
    assert _DEAD_URL not in manifest["dead"]
    assert _DEAD_URL not in ledger


@pytest.mark.asyncio
async def test_retry_dead_still_gone_stays_dead_and_counts_the_attempt(gallery_dir, thumbnail_store, monkeypatch):
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    manifest, ledger = _dead_everywhere()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404)

    async with _client_for(handler) as client:
        outcome = await _retry(client, gallery_dir, thumbnail_store, manifest, ledger)

    assert len(calls) == 1
    assert outcome.status == "skipped"
    assert _DEAD_URL in manifest["dead"]
    assert media_manifest.is_dead(ledger, _DEAD_URL)
    assert ledger[_DEAD_URL]["n"] == 2


@pytest.mark.asyncio
async def test_retry_dead_transient_exhausted_link_is_dead_again_immediately(gallery_dir, thumbnail_store, monkeypatch):
    """Not reset: a link that hit the transient cap and still fails must not
    fall back to "retry on every Localize all"."""
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    manifest, ledger = _dead_everywhere(permanent=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _client_for(handler) as client:
        await _retry(client, gallery_dir, thumbnail_store, manifest, ledger)

    assert _DEAD_URL in manifest["dead"]
    assert media_manifest.is_dead(ledger, _DEAD_URL)
    assert ledger[_DEAD_URL]["n"] == media_manifest.MAX_TRANSIENT_ATTEMPTS + 1


@pytest.mark.asyncio
async def test_retry_dead_still_failing_with_no_ledger_entry_keeps_the_manifest_entry(gallery_dir, thumbnail_store, monkeypatch):
    """The global ledger evicts; the card's own `dead` entry doesn't. A first
    transient failure is below the cap, but the card's record must survive."""
    monkeypatch.setattr(media_writer, "_preflight_dns", lambda url: None)
    manifest, _ = _dead_everywhere()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _client_for(handler) as client:
        await _retry(client, gallery_dir, thumbnail_store, manifest, {})

    assert _DEAD_URL in manifest["dead"]
