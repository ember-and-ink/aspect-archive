"""aspect/media/discovery.py: `extract_media_urls` must match `extractMediaUrls`
(web/library-sections/30-media-localization-feature.js) exactly -- a URL it
mangles is a file that never downloads and, worse, a fabricated URL that
404s and gets filed as permanently dead.

Ported case-for-case from web/tests/media-urls.test.js, the acceptance suite
named in docs/UI_REWRITE_PLAN.md §1.3 for this salvage item.
"""

from __future__ import annotations

from aspect.media.discovery import collect_card_text_chunks, extract_media_urls, find_character_media_urls


def test_janitorai_random_macro_yields_each_url_not_one_run_on_string():
    text = (
        "![]{{random:(https://files.catbox.moe/2og42i.jpg),"
        "(https://files.catbox.moe/mgnq2e.jpg),"
        "(https://files.catbox.moe/4p0fdu.jpg)}}"
    )
    assert extract_media_urls(text) == [
        "https://files.catbox.moe/2og42i.jpg",
        "https://files.catbox.moe/mgnq2e.jpg",
        "https://files.catbox.moe/4p0fdu.jpg",
    ]


def test_markdown_images_closing_paren_is_not_part_of_the_url():
    text = "![](https://media.datacat.run/a/b.webp/0501d0ff901a583e)\n\nprose"
    assert extract_media_urls(text) == ["https://media.datacat.run/a/b.webp/0501d0ff901a583e"]


def test_a_real_url_containing_parens_still_survives_via_the_markdown_branch():
    text = "![](https://i.postimg.cc/CLPDhrp9/(16).jpg)"
    assert extract_media_urls(text) == ["https://i.postimg.cc/CLPDhrp9/(16).jpg"]


def test_bare_urls_html_tags_and_css_url_all_still_resolve():
    text = "\n".join(
        [
            "see https://example.com/plain.png here",
            '<img src="https://example.com/tag.jpg">',
            "background-image: url('https://example.com/css.webp')",
        ]
    )
    assert sorted(extract_media_urls(text)) == [
        "https://example.com/css.webp",
        "https://example.com/plain.png",
        "https://example.com/tag.jpg",
    ]


def test_audio_and_video_urls_are_not_discovered():
    text = "\n".join(
        [
            "theme https://cdn.example.com/song.mp3 here",
            '<audio src="https://cdn.example.com/voice.mp3"></audio>',
            '<video><source src="https://cdn.example.com/clip.webm"></video>',
            "![](https://cdn.example.com/scene.mp4)",
            "background-image: url('https://cdn.example.com/loop.mov')",
            "https://cdn.example.com/take.wav",
            "https://cdn.example.com/track.m4a",
            "https://cdn.example.com/lossless.flac",
        ]
    )
    assert extract_media_urls(text) == []


def test_images_alongside_audio_still_come_through():
    text = "\n".join(
        [
            "![](https://cdn.example.com/portrait.png)",
            "https://cdn.example.com/theme.mp3",
            '<img src="https://cdn.example.com/ref.webp">',
        ]
    )
    assert extract_media_urls(text) == [
        "https://cdn.example.com/portrait.png",
        "https://cdn.example.com/ref.webp",
    ]


def test_a_query_string_neither_hides_an_mp3_nor_condemns_a_png():
    text = "\n".join(
        [
            "https://cdn.example.com/song.mp3?token=abc",
            "https://cdn.example.com/art.png?format=mp4",
        ]
    )
    assert extract_media_urls(text) == ["https://cdn.example.com/art.png"]


def test_a_markdown_linked_mp3_with_a_signature_query_is_refused():
    assert extract_media_urls("![](https://cdn.example.com/song.mp3?sig=xyz)") == []


# --- collect_card_text_chunks / find_character_media_urls -----------------
# Not in the JS acceptance suite (those walk `character.data`, not raw text),
# but the same server-side scan route depends on them being right.


def test_collect_card_text_chunks_covers_the_documented_surfaces():
    data = {
        "description": "see https://cdn.example.com/desc.png",
        "personality": "",
        "creator_notes": "https://cdn.example.com/notes.jpg",
        "alternate_greetings": ["hi https://cdn.example.com/greet.png", 123],
        "extensions": {"jai": {"tagline": "https://cdn.example.com/tag.png"}, "bogus": "not a dict"},
        "character_book": {
            "entries": [
                {"content": "https://cdn.example.com/lore.png"},
                {"content": None},
            ]
        },
    }
    main, lorebook = collect_card_text_chunks(data)
    assert main == [
        "see https://cdn.example.com/desc.png",
        "https://cdn.example.com/notes.jpg",
        "https://cdn.example.com/tag.png",
        "hi https://cdn.example.com/greet.png",
    ]
    assert lorebook == ["https://cdn.example.com/lore.png"]


def test_find_character_media_urls_splits_embedded_from_lorebook_and_dedupes_across_them():
    data = {
        "description": "https://cdn.example.com/shared.png https://cdn.example.com/embed.png",
        "character_book": {
            "entries": [
                # Already seen in `description` -- must not also appear in lorebook.
                {"content": "https://cdn.example.com/shared.png https://cdn.example.com/lore.png"},
            ]
        },
    }
    embedded, lorebook = find_character_media_urls(data)
    assert embedded == ["https://cdn.example.com/shared.png", "https://cdn.example.com/embed.png"]
    assert lorebook == ["https://cdn.example.com/lore.png"]


def test_find_character_media_urls_on_an_empty_card():
    assert find_character_media_urls({}) == ([], [])
