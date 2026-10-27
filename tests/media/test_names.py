"""aspect/media/names.py must match its JS counterparts exactly -- see the
module docstring for why. This pins two things:

1. Real URLs pulled from actual cards (tests/fixtures/media_3c/real_url_names.json),
   with expected output captured by running the *actual* JS
   (`extractSanitizedUrlName` in web/library-sections/30-media-localization-feature.js)
   under node, not re-derived by hand.
2. Edge cases (CDN variant prepend, fragment/query stripping, truncation,
   unicode, empty/invalid input) diffed the same way -- see the commit that
   added this file for the node verification script.
"""

from __future__ import annotations

import json
from pathlib import Path

from aspect.media.names import (
    PREFIX_PRIORITY,
    extract_sanitized_url_name,
    keys_for_item,
    local_filename,
    media_key,
    prefix_priority,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "media_3c" / "real_url_names.json"


def test_real_urls_match_js_output():
    expected = json.loads(FIXTURE.read_text())
    for url, name in expected.items():
        assert extract_sanitized_url_name(url) == name, url


def test_cdn_variant_prepends_parent_segment():
    # parent >= 4 chars: prepend
    assert extract_sanitized_url_name("https://cdn.example.com/abcd/public") == "abcd_public"
    # parent < 4 chars: JS falls through and keeps the bare variant name
    assert extract_sanitized_url_name("https://cdn.example.com/123/public") == "public"
    assert extract_sanitized_url_name("https://cdn.example.com/ab/original") == "original"


def test_fragment_and_query_are_not_part_of_the_name():
    assert extract_sanitized_url_name("https://cdn.example.com/user/123/public?x=1") == "public"
    # '#' starts a URL fragment -- JS's `new URL()` truncates the path there too.
    assert extract_sanitized_url_name("https://example.com/weird!@#$%^&*()name.png") == "weird__"


def test_truncated_to_40_chars():
    long_name = "x" * 60 + ".png"
    result = extract_sanitized_url_name(f"https://example.com/{long_name}")
    assert result == "x" * 40


def test_invalid_or_empty_url_returns_empty_string():
    assert extract_sanitized_url_name("not a url") == ""
    assert extract_sanitized_url_name("https://example.com/") == ""
    assert extract_sanitized_url_name("https://example.com") == ""


def test_unicode_is_percent_encoded_then_sanitized():
    # Matches new URL(): each non-ASCII char becomes multi-byte UTF-8 %XX
    # triples before the [^a-zA-Z0-9_-] sanitizer runs, so this is NOT the
    # same as sanitizing the raw unicode string directly.
    assert extract_sanitized_url_name("https://example.com/Ünïcödé Name!!.png") == "_C3_9Cn_C3_AFc_C3_B6d_C3_A9_20Name__"


def test_backslash_is_a_path_separator():
    assert extract_sanitized_url_name("https://example.com/a\\b\\c.png") == "c"


def test_tab_newline_cr_are_stripped_not_encoded():
    assert extract_sanitized_url_name("https://example.com/x\ty\nz.png") == "xyz"


def test_control_char_is_percent_encoded():
    assert extract_sanitized_url_name("https://example.com/x\x01y.png") == "x_01y"


def test_media_key_strips_extension_and_lowercases():
    assert media_key("localized_media_ab12cd_MyFile.PNG") == "localized_media_ab12cd_myfile"
    assert media_key("no-ext-name") == "no-ext-name"
    assert media_key(None) == ""
    assert media_key("") == ""


def test_keys_for_item_prefers_filename_over_url():
    keys = keys_for_item("https://files.catbox.moe/2og42i.jpg", "RealName.png")
    assert keys[0] == "realname"
    assert "2og42i" in keys


def test_keys_for_item_drops_names_shorter_than_min_length():
    # 'abc' extracted-name is 3 chars, below MIN_KEY_LENGTH (4)
    keys = keys_for_item("https://cdn.example.com/abc", "ab")
    assert keys == []


def test_prefix_priority_ladder():
    assert prefix_priority("localized_media_x_name.png") == PREFIX_PRIORITY["localized_media"]
    assert prefix_priority("lorebook_media_x_name.png") == PREFIX_PRIORITY["lorebook_media"]
    assert prefix_priority("extgallery_x_name.png") == PREFIX_PRIORITY["extgallery"]
    assert prefix_priority("chubgallery_x_name.png") == 1
    assert prefix_priority("datacatgallery_x_name.png") == 1
    assert prefix_priority("plainname.png") == 0


def test_local_filename_format():
    assert local_filename("localized_media", 3, "myfile", "webp") == "localized_media_3_myfile.webp"
    assert local_filename("chubgallery", 0, "abc123", ".png") == "chubgallery_0_abc123.png"
