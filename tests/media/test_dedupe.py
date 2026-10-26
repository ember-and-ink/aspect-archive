"""aspect/media/dedupe.py: trashing exact-duplicate gallery files left behind
by a media re-download that repointed `.media.json` at a new filename without
cleaning up the old one."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aspect.archive import thumbs
from aspect.config import settings
from aspect.media import dedupe as media_dedupe, manifest as media_manifest


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def galleries_root(tmp_path: Path) -> Path:
    return tmp_path / "galleries"


def _manifest_with(gallery_dir: Path, url: str, filename: str, data: bytes) -> None:
    manifest = media_manifest.load_manifest(gallery_dir)
    media_manifest.record_saved(manifest, url, filename, _sha(data))
    media_manifest.save_manifest(gallery_dir, manifest)


def test_trashes_a_stale_redownload_that_matches_the_current_files_hash(
    galleries_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    gallery = galleries_root / "Alex_EOuYqgqXXBfX"
    gallery.mkdir(parents=True)
    payload = b"same bytes both times"
    stale = gallery / "extgallery_1000_abc.webp"
    current = gallery / "extgallery_2000_abc.webp"
    stale.write_bytes(payload)
    current.write_bytes(payload)
    _manifest_with(gallery, "https://cdn.example.com/x.png", current.name, payload)

    result = media_dedupe.dedupe_galleries(galleries_root, apply=True)

    assert result.files_trashed == 1
    assert result.folders_touched == 1
    assert result.bytes_freed == len(payload)
    assert result.unresolved == 0
    assert not stale.exists()
    assert current.exists()
    assert list((tmp_path / "trash").rglob("extgallery_1000_abc.webp"))


def test_leaves_a_manually_added_file_alone(galleries_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    gallery = galleries_root / "Alex_EOuYqgqXXBfX"
    gallery.mkdir(parents=True)
    current = gallery / "extgallery_2000_abc.webp"
    current.write_bytes(b"tracked image")
    manual = gallery / "Alex Bennett.webp"
    manual.write_bytes(b"a completely different picture, added by hand")
    _manifest_with(gallery, "https://cdn.example.com/x.png", current.name, current.read_bytes())

    result = media_dedupe.dedupe_galleries(galleries_root, apply=True)

    assert result.files_trashed == 0
    assert result.unresolved == 1
    assert result.unresolved_details == ["Alex_EOuYqgqXXBfX/Alex Bennett.webp"]
    assert manual.exists()


def test_dry_run_reports_without_moving_anything(
    galleries_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    gallery = galleries_root / "Alex_EOuYqgqXXBfX"
    gallery.mkdir(parents=True)
    payload = b"same bytes both times"
    stale = gallery / "extgallery_1000_abc.webp"
    current = gallery / "extgallery_2000_abc.webp"
    stale.write_bytes(payload)
    current.write_bytes(payload)
    _manifest_with(gallery, "https://cdn.example.com/x.png", current.name, payload)

    result = media_dedupe.dedupe_galleries(galleries_root, apply=False)

    assert result.files_trashed == 1
    assert stale.exists()
    assert not (tmp_path / "trash").exists()


def test_a_folder_with_no_manifest_files_is_skipped_entirely(galleries_root: Path):
    gallery = galleries_root / "Empty_AAAAAAAAAAAA"
    gallery.mkdir(parents=True)
    (gallery / "something.webp").write_bytes(b"whatever")

    result = media_dedupe.dedupe_galleries(galleries_root, apply=True)

    assert result.files_trashed == 0
    assert result.folders_touched == 0
    assert (gallery / "something.webp").exists()


def test_forgets_the_cached_thumbnail_for_a_trashed_file(
    galleries_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    gallery = galleries_root / "Alex_EOuYqgqXXBfX"
    gallery.mkdir(parents=True)
    payload = b"same bytes both times"
    stale = gallery / "extgallery_1000_abc.webp"
    current = gallery / "extgallery_2000_abc.webp"
    stale.write_bytes(payload)
    current.write_bytes(payload)
    _manifest_with(gallery, "https://cdn.example.com/x.png", current.name, payload)

    store = thumbs.ThumbnailStore(tmp_path / "cache" / "thumbs", galleries_root)
    thumb_dir = store.gallery_dir / gallery.name
    thumb_dir.mkdir(parents=True)
    cached_thumb = thumb_dir / f"{stale.name}_200.jpg"
    cached_thumb.write_bytes(b"fake thumb")

    media_dedupe.dedupe_galleries(galleries_root, apply=True, thumb_store=store)

    assert not cached_thumb.exists()
