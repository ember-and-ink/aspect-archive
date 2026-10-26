"""The in-memory archive index."""

from __future__ import annotations

import os
from pathlib import Path

from proxy.archive import catalog
from tests.conftest import card_png, jai_extensions


def _index(root: Path) -> catalog.ArchiveIndex:
    index = catalog.ArchiveIndex(root)
    index.refresh()
    return index


def test_summarizes_a_card(populated_archive):
    index = _index(populated_archive["characters"])
    record = index.get("Abbie_0d162f5f.png")

    assert record is not None
    assert record.name == "Abbie"
    assert record.creator == "KornyPony"
    assert record.tags == ("Female", "Vampire")
    assert record.card_id == "0d162f5f-86ab-4fdd-a2c2-3912adf24960"
    assert record.fragment == "0d162f5f"
    assert record.gallery_id == "kzbYR2QbpncC"
    assert record.source_kind == "janitor_core"
    assert record.lore_entry_count == 2
    assert record.greeting_count == 1
    assert record.ok


def test_counts_greetings_as_a_reader_would(populated_archive):
    """"3 greetings" has to mean the primary plus two alternates -- the number a
    user sees in the UI, not the length of the alternates array."""
    index = _index(populated_archive["characters"])
    assert index.get("Bella_11112222.png").greeting_count == 3


def test_a_card_with_no_first_mes_has_no_greetings(archive_dirs):
    (archive_dirs["characters"] / "Empty_1.png").write_bytes(card_png("Empty", first_mes=""))
    index = _index(archive_dirs["characters"])
    assert index.get("Empty_1.png").greeting_count == 0


def test_unreadable_cards_are_recorded_not_skipped(archive_dirs):
    """The whole point of the error field: an archive that silently drops files it
    cannot read is the failure an archive exists to prevent."""
    characters = archive_dirs["characters"]
    (characters / "Fine_1.png").write_bytes(card_png("Fine"))
    (characters / "NotEvenAPng.png").write_bytes(b"this is not a png")
    (characters / "PngWithNoCard.png").write_bytes(_bare_png())

    index = _index(characters)

    assert len(index.all()) == 3
    assert {r.filename for r in index.cards()} == {"Fine_1.png"}
    broken = {r.filename: r.error for r in index.broken()}
    assert set(broken) == {"NotEvenAPng.png", "PngWithNoCard.png"}
    assert "not a PNG stream" in broken["NotEvenAPng.png"]
    assert broken["PngWithNoCard.png"] == "no character card embedded"
    # A broken record still knows its file, so the UI can show it and a human can
    # act on it.
    assert index.get("NotEvenAPng.png").size > 0


def test_refresh_only_reparses_what_changed(populated_archive):
    characters = populated_archive["characters"]
    index = _index(characters)
    assert index.last_stats.parsed == 3

    stats = index.refresh()
    assert stats.parsed == 0
    assert stats.unchanged == 3

    # Rewrite one card with different content *and* a different length: the pair
    # (mtime_ns, size) is what invalidation keys on.
    (characters / "Cleo_33334444.png").write_bytes(card_png("Cleopatra Renamed"))
    stats = index.refresh()
    assert stats.parsed == 1
    assert stats.unchanged == 2
    assert index.get("Cleo_33334444.png").name == "Cleopatra Renamed"


def test_refresh_picks_up_new_and_deleted_cards(populated_archive):
    """No reindex step, no restart -- which is how an acquisition lands in the
    browse grid live."""
    characters = populated_archive["characters"]
    index = _index(characters)

    (characters / "Dana_55556666.png").write_bytes(card_png("Dana"))
    stats = index.refresh()
    assert stats.parsed == 1
    assert index.get("Dana_55556666.png").name == "Dana"

    (characters / "Dana_55556666.png").unlink()
    stats = index.refresh()
    assert stats.removed == 1
    assert index.get("Dana_55556666.png") is None


def test_reads_never_rescan_on_their_own(populated_archive):
    """Only an explicit refresh sees an outside write -- reads trust the index."""
    characters = populated_archive["characters"]
    index = catalog.ArchiveIndex(characters)
    index.refresh()

    (characters / "Late_77778888.png").write_bytes(card_png("Late"))
    assert index.get("Late_77778888.png") is None
    index.refresh()
    assert index.get("Late_77778888.png") is not None


def test_lookup_tolerates_case_and_normalization(populated_archive):
    """A card's filename arrives from a URL, and the filesystem this archive
    lives on is both case- and NFC-insensitive. Exact match still wins."""
    index = _index(populated_archive["characters"])
    assert index.get("ABBIE_0D162F5F.PNG").filename == "Abbie_0d162f5f.png"


def test_lookup_normalizes_unicode(archive_dirs):
    characters = archive_dirs["characters"]
    # NFC on disk (é as one codepoint), NFD in the request (e + combining acute).
    (characters / "Amélie_1.png").write_bytes(card_png("Amélie"))
    index = _index(characters)
    assert index.get("Amélie_1.png") is not None


def test_path_of_refuses_anything_not_indexed(populated_archive):
    """Traversal cannot name a file: a path is resolved through the index, never
    by joining user input onto the root."""
    index = _index(populated_archive["characters"])
    assert index.path_of("../../etc/passwd") is None
    assert index.path_of("Abbie_0d162f5f.png").name == "Abbie_0d162f5f.png"


def test_by_fragment(populated_archive):
    index = _index(populated_archive["characters"])
    assert [r.name for r in index.by_fragment("0d162f5f")] == ["Abbie"]
    assert index.by_fragment("") == ()
    assert index.by_fragment("deadbeef") == ()


def test_by_fragment_is_a_dict_lookup_not_a_scan(populated_archive):
    """The fragment index is rebuilt on every rescan, not just at construction
    -- a card added after the index already existed must still be found."""
    characters = populated_archive["characters"]
    index = _index(characters)
    assert index.by_fragment("cafebabe") == ()
    (characters / "New_cafebabe.png").write_bytes(
        card_png(
            "New",
            extensions=jai_extensions(card_id="cafebabe-0000-0000-0000-000000000000"),
        )
    )
    index.refresh()
    assert [r.name for r in index.by_fragment("cafebabe")] == ["New"]


def test_fragments_is_every_on_disk_fragment(populated_archive):
    index = _index(populated_archive["characters"])
    fragments = index.fragments()
    assert "0d162f5f" in fragments
    assert "deadbeef" not in fragments
    # Every non-empty fragment `by_fragment` can answer for is in the set.
    assert all(index.by_fragment(f) for f in fragments)


def test_short_source_ids_yield_short_fragments(archive_dirs):
    """Chub's ids are short integers, so the fragment is shorter than 8 chars --
    it has to match what the filename was actually built from."""
    (archive_dirs["characters"] / "Chubby_16477.png").write_bytes(
        card_png("Chubby", extensions=jai_extensions("16477", source_kind="chub_import"))
    )
    index = _index(archive_dirs["characters"])
    assert index.get("Chubby_16477.png").fragment == "16477"


def test_haystack_covers_the_searchable_fields(populated_archive):
    index = _index(populated_archive["characters"])
    haystack = index.get("Bella_11112222.png").haystack
    for term in ("bella", "someone else", "bella the second", "male", "11112222"):
        assert term in haystack
    assert "test character" not in haystack, "prose is deliberately not indexed"


def test_survives_a_card_with_junk_field_types(archive_dirs):
    """Five importers write these cards. A None where a string belongs must not
    end a scan of thousands."""
    (archive_dirs["characters"] / "Junk_1.png").write_bytes(
        card_png("Junk", tags=["ok", None, 42, "  "], creator=None, description=None, extensions=None)
    )
    index = _index(archive_dirs["characters"])
    record = index.get("Junk_1.png")
    assert record.ok
    assert record.tags == ("ok",)
    assert record.creator == ""
    assert record.description_chars == 0


def test_nested_layout_is_indexed_too(archive_dirs):
    """`card_layout=nested` puts cards under a `<creator>/` level."""
    nested = archive_dirs["characters"] / "KornyPony"
    nested.mkdir()
    (nested / "Deep_9999.png").write_bytes(card_png("Deep"))
    index = _index(archive_dirs["characters"])
    assert index.get("Deep_9999.png") is not None


def test_ignores_non_png_files(archive_dirs):
    characters = archive_dirs["characters"]
    (characters / "Fine_1.png").write_bytes(card_png("Fine"))
    (characters / "notes.txt").write_text("not a card")
    (characters / ".DS_Store").write_bytes(b"\x00")
    index = _index(characters)
    assert [r.filename for r in index.all()] == ["Fine_1.png"]


def test_missing_archive_dir_is_empty_not_fatal(tmp_path):
    """A fresh container mounts an empty volume; the server has to start anyway."""
    index = catalog.ArchiveIndex(tmp_path / "nope")
    index.refresh()
    assert index.all() == ()


def _bare_png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, "PNG")
    return buffer.getvalue()


def test_a_card_vanishing_mid_scan_is_survivable(populated_archive, monkeypatch):
    """`make names` renames cards in place; a scan can race one."""
    characters = populated_archive["characters"]
    index = catalog.ArchiveIndex(characters)
    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "Bella_11112222.png":
            raise OSError(2, "No such file or directory")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    index.refresh()
    assert {r.name for r in index.cards()} == {"Abbie", "Cleo"}
    monkeypatch.undo()
    index.refresh()


def test_index_is_a_singleton_bound_to_the_configured_dir(populated_archive):
    first = catalog.index()
    assert first is catalog.index()
    assert first.root == populated_archive["characters"]
    assert len(first.cards()) == 3
    assert os.path.isdir(first.root)
