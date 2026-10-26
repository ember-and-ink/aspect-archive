"""The source ledger -- `discovery.enumerate_sources` and the
`manifest.effective_sources` / `sources_satisfied` pair it feeds.

This is the mechanism that answers "why did this card never download its
images". `files` and `dead` record what was fetched; between them they cannot
say that a URL was *seen* and could not be handled, which is the state every
Civitai-linked card sat in. Recording it is what lets adding an extractor
re-arm exactly the cards it can now handle, with no archive-wide rescan.
"""

from __future__ import annotations

from aspect.media import discovery, manifest as media_manifest


def _card(**fields) -> dict:
    return {"name": "Test", **fields}


def _refs(*specs) -> list[discovery.SourceRef]:
    return [discovery.SourceRef(*spec) for spec in specs]


# ---- enumeration -----------------------------------------------------------


def test_classifies_each_kind_of_source():
    refs = discovery.enumerate_sources(
        _card(
            description=(
                "![](https://cdn.example.com/a.png)\n"
                "Gallery https://civitai.com/posts/1981754\n"
                "Album https://catbox.moe/c/x5uzds\n"
                "Theme https://cdn.example.com/song.mp3\n"
                "Read https://rentry.co/lore"
            ),
            character_book={"entries": [{"content": "![](https://cdn.example.com/b.png)"}]},
            extensions={"chub": {"id": 555}},
        )
    )
    by_key = {r.key: r for r in refs}

    assert by_key["https://cdn.example.com/a.png"].handler == "embedded"
    assert by_key["https://cdn.example.com/b.png"].handler == "lorebook"
    assert by_key["https://civitai.com/posts/1981754"].handler == "civitai"
    assert by_key["https://catbox.moe/c/x5uzds"].handler == "catbox"
    assert by_key["chub:555"].handler == "chub"

    # Seen and refused by policy, not a failure -- and recorded, so a policy
    # change would re-arm exactly these cards.
    mp3 = by_key["https://cdn.example.com/song.mp3"]
    assert (mp3.handler, mp3.status) == (None, discovery.IGNORED)

    # Seen and nothing here can fetch it. This is the Civitai case, before
    # `media/civitai.py` existed.
    unhandled = by_key["https://rentry.co/lore"]
    assert (unhandled.handler, unhandled.status) == (None, discovery.UNHANDLED)


def test_a_card_with_no_urls_has_no_sources():
    assert discovery.enumerate_sources(_card(description="just prose")) == []


def test_sources_are_deduped_and_keep_first_seen_order():
    refs = discovery.enumerate_sources(
        _card(description="https://rentry.co/a and again https://rentry.co/a then https://rentry.co/b")
    )
    assert [r.key for r in refs] == ["https://rentry.co/a", "https://rentry.co/b"]


def test_an_image_only_the_css_pattern_finds_is_still_a_source():
    """`find_urls` reads the text differently from the image regexes -- a
    `url('...')` value ends at the quote, which the bare pattern doesn't see.
    A source must never be dropped because the second pass read it differently
    from the first."""
    refs = discovery.enumerate_sources(
        _card(description="<style>body{background:url('https://cdn.example.com/bg.png')}</style>")
    )
    assert [(r.key, r.handler) for r in refs] == [("https://cdn.example.com/bg.png", "embedded")]


# ---- the satisfied check ---------------------------------------------------


def test_a_downloaded_image_needs_no_ledger_entry_of_its_own():
    """`files` already proves it. Deriving instead of duplicating is what makes
    every pre-ledger manifest migrate for free."""
    manifest = media_manifest.empty_manifest()
    media_manifest.record_saved(manifest, "https://cdn.example.com/a.png", "localized_media_0_a.webp", "sha")

    assert media_manifest.sources_satisfied(manifest, _refs(("https://cdn.example.com/a.png", "embedded", "ready")))


def test_a_permanently_dead_image_counts_as_settled():
    manifest = media_manifest.empty_manifest()
    media_manifest.record_dead(manifest, "https://cdn.example.com/gone.png", "HTTP 404")

    assert media_manifest.sources_satisfied(manifest, _refs(("https://cdn.example.com/gone.png", "embedded", "ready")))


def test_an_unrecorded_url_leaves_the_card_unsatisfied():
    manifest = media_manifest.empty_manifest()

    assert not media_manifest.sources_satisfied(manifest, _refs(("https://cdn.example.com/new.png", "embedded", "ready")))


def test_a_gallery_root_is_never_derivable_so_a_pre_ledger_card_runs_once():
    """`files` holds a MEGA folder's decrypted children, never the folder. A
    manifest from before the ledger therefore cannot prove its gallery was
    listed -- so it re-runs exactly once, records itself, and skips thereafter.
    """
    manifest = media_manifest.empty_manifest()
    media_manifest.record_saved(manifest, "mega://handle/key", "extgallery_0_x.webp", "sha")
    root = ("https://mega.nz/folder/X#Y", "mega", "ready")

    assert not media_manifest.sources_satisfied(manifest, _refs(root))

    media_manifest.record_source(manifest, root[0], "mega", media_manifest.SOURCE_DONE, count=1)
    assert media_manifest.sources_satisfied(manifest, _refs(root))


def test_an_unhandled_url_re_arms_once_a_handler_exists():
    """The mechanism, stated plainly. Recorded when nothing could fetch it;
    un-satisfied the moment something can."""
    manifest = media_manifest.empty_manifest()
    url = "https://civitai.com/posts/1981754"
    media_manifest.record_source(manifest, url, None, media_manifest.SOURCE_UNHANDLED)

    # As it was: no handler, so nothing to redo.
    assert media_manifest.sources_satisfied(manifest, _refs((url, None, discovery.UNHANDLED)))
    # As it is now: `media/civitai.py` exists, so this card comes back round.
    assert not media_manifest.sources_satisfied(manifest, _refs((url, "civitai", "ready")))


def test_a_replaced_handler_re_arms_the_card():
    manifest = media_manifest.empty_manifest()
    url = "https://gallery.example/album/1"
    media_manifest.record_source(manifest, url, "old-extractor", media_manifest.SOURCE_DONE, count=3)

    assert not media_manifest.sources_satisfied(manifest, _refs((url, "new-extractor", "ready")))


def test_an_ignored_url_stays_settled():
    manifest = media_manifest.empty_manifest()
    url = "https://cdn.example.com/song.mp3"
    media_manifest.record_source(
        manifest, url, None, media_manifest.SOURCE_IGNORED, reason="audio/video not archived"
    )

    assert media_manifest.sources_satisfied(manifest, _refs((url, None, discovery.IGNORED)))


def test_stored_records_win_over_derived_ones():
    manifest = media_manifest.empty_manifest()
    url = "https://cdn.example.com/a.png"
    media_manifest.record_saved(manifest, url, "localized_media_0_a.webp", "sha")
    media_manifest.record_source(manifest, url, "embedded", media_manifest.SOURCE_DONE)

    view = media_manifest.effective_sources(manifest, _refs((url, "embedded", "ready")))
    assert view[url]["st"] == media_manifest.SOURCE_DONE
    assert "at" in view[url]  # the stored entry, not the derived stand-in


def test_a_corrupt_sources_map_is_discarded_not_trusted(tmp_path):
    """Same rule the rest of `load_manifest` follows: a damaged store loses its
    own history rather than blocking new downloads."""
    gallery_dir = tmp_path / "g"
    gallery_dir.mkdir()
    media_manifest.manifest_path(gallery_dir).write_text('{"files": {}, "dead": {}, "runs": [], "sources": []}')

    manifest = media_manifest.load_manifest(gallery_dir)
    assert "sources" not in manifest
    assert not media_manifest.sources_satisfied(manifest, _refs(("https://x.example/a.png", "embedded", "ready")))
