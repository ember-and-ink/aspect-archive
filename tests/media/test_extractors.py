"""`aspect/media/extractors.py` -- album page -> direct image URLs.

Ported from `web/modules/gallery-extractors/*` at Stage 6B, for the four hosts a
scan of all 3,868 cards showed are actually used. The HTML fixtures below are
trimmed from the real page shapes each legacy extractor was written against.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aspect.config import settings
from aspect.media import extractors


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---- URL discovery ---------------------------------------------------------


def test_finds_album_pages_and_ignores_direct_files():
    text = """
    Gallery: https://catbox.moe/c/x5uzds
    ![](https://files.catbox.moe/a8rjmb.jpg)
    More: https://imgchest.com/p/lqyelvelz4d
    ![](https://cdn.imgchest.com/files/890055276362.webp)
    """
    found = extractors.find_gallery_urls(text)

    # Only the album pages. The direct files are already images and belong to
    # the ordinary embedded phase -- matching them here would fetch the same
    # bytes twice.
    assert found == [
        "https://catbox.moe/c/x5uzds",
        "https://imgchest.com/p/lqyelvelz4d",
    ]


def test_trims_punctuation_glued_on_by_prose():
    text = "see https://catbox.moe/c/abc123, and https://postimg.cc/gallery/xyz789."

    assert extractors.find_gallery_urls(text) == [
        "https://catbox.moe/c/abc123",
        "https://postimg.cc/gallery/xyz789",
    ]


def test_trims_markdown_emphasis_glued_on_by_the_link_text():
    # Verbatim from Hotaru_Kitsuragi_c7f78e8c: the bold link text is the URL
    # itself, so the scan meets it twice -- once with `**` glued on. That copy
    # 404'd on every run.
    text = "[**https://ibb.co/album/7Q9DCg**](https://ibb.co/album/7Q9DCg)"

    assert extractors.find_gallery_urls(text) == ["https://ibb.co/album/7Q9DCg"]


def test_keeps_a_trailing_underscore():
    # `_` is emphasis too, but real ids end in one (seen in the archive).
    url = "https://tools.theworkshop.team/api/image/ybAoERMl1_"

    assert extractors.find_urls(f"![]({url})") == [url]


def test_deduplicates_and_keeps_order():
    text = "https://catbox.moe/c/aaa https://catbox.moe/c/bbb https://catbox.moe/c/aaa"

    assert extractors.find_gallery_urls(text) == [
        "https://catbox.moe/c/aaa",
        "https://catbox.moe/c/bbb",
    ]


def test_imgbox_is_not_matched():
    # Measured: 0 cards need an imgbox extractor. Every card that mentions it
    # carries direct image URLs the embedded phase already handles.
    assert extractors.find_gallery_urls("https://imgbox.com/g/abc123") == []


def test_gdrive_is_not_matched():
    assert extractors.find_gallery_urls("https://drive.google.com/drive/folders/x") == []


def test_mega_folder_links_are_matched():
    assert extractors.find_gallery_urls("https://mega.nz/folder/6YMGHRCR#A2jl") == [
        "https://mega.nz/folder/6YMGHRCR#A2jl"
    ]


def test_mega_legacy_format_is_matched():
    assert extractors.find_gallery_urls("https://mega.nz/#F!6YMGHRCR!A2jl") == [
        "https://mega.nz/#F!6YMGHRCR!A2jl"
    ]


# ---- catbox ----------------------------------------------------------------


def test_catbox_reads_the_plain_text_file_links():
    body = (
        "<pre>>https://files.catbox.moe/one.png<\n"
        ">https://files.catbox.moe/two.jpg<\n"
        ">https://files.catbox.moe/one.png<</pre>"
    )
    images = extractors._parse_catbox(body)

    assert [i.url for i in images] == [
        "https://files.catbox.moe/one.png",
        "https://files.catbox.moe/two.jpg",
    ]
    assert images[0].filename == "one.png"


# ---- postimg ---------------------------------------------------------------


def test_postimg_builds_cdn_urls_from_card_data_attributes():
    body = (
        '<a data-hotlink="1s6LX5yh" data-name="1" data-ext="webp"></a>'
        '<a data-hotlink="nhXNkd5V" data-name="two" data-ext="png"></a>'
    )
    images = extractors._parse_postimg(body)

    assert [i.url for i in images] == [
        "https://i.postimg.cc/1s6LX5yh/1.webp",
        "https://i.postimg.cc/nhXNkd5V/two.png",
    ]


# ---- imgchest --------------------------------------------------------------


def test_imgchest_prefers_the_embedded_json_blob():
    body = (
        '<div data-page="{&quot;props&quot;:{&quot;post&quot;:{&quot;files&quot;:'
        '[{&quot;link&quot;:&quot;https://cdn.imgchest.com/files/aaa.webp&quot;},'
        '{&quot;link&quot;:&quot;https://cdn.imgchest.com/files/bbb.png&quot;}]}}}"></div>'
    )
    images = extractors._parse_imgchest(body)

    assert [i.url for i in images] == [
        "https://cdn.imgchest.com/files/aaa.webp",
        "https://cdn.imgchest.com/files/bbb.png",
    ]


def test_imgchest_falls_back_to_scraping_when_the_blob_is_unusable():
    # The blob's shape follows imgchest's frontend and has changed before; the
    # regex still finds the images when it does.
    body = (
        '<div data-page="{not json"></div>'
        '<img src="https://cdn.imgchest.com/files/ccc.png">'
    )
    images = extractors._parse_imgchest(body)

    assert [i.url for i in images] == ["https://cdn.imgchest.com/files/ccc.png"]


def test_imgchest_password_gated_post_yields_nothing_quietly():
    images = extractors._parse_imgchest("<form>PostPassword</form>")

    assert images == []


# ---- imgbb -----------------------------------------------------------------


def test_imgbb_album_reads_every_data_object():
    body = (
        """<div data-object='%7B%22image%22%3A%7B%22url%22%3A%22https%3A//i.ibb.co/a/1.png%22%7D%7D'></div>"""
        """<div data-object='%7B%22image%22%3A%7B%22url%22%3A%22https%3A//i.ibb.co/b/2.png%22%7D%7D'></div>"""
    )
    images = extractors._parse_imgbb(body)

    assert [i.url for i in images] == [
        "https://i.ibb.co/a/1.png",
        "https://i.ibb.co/b/2.png",
    ]


def test_imgbb_single_image_page_falls_back_to_og_image():
    body = '<meta property="og:image" content="https://i.ibb.co/x/only.png">'
    images = extractors._parse_imgbb(body)

    assert [i.url for i in images] == ["https://i.ibb.co/x/only.png"]


# ---- resolution across hosts ----------------------------------------------


def test_one_failing_album_does_not_sink_the_rest():
    """These are third-party pages that go away, rate-limit and change markup.
    A card's media run must survive one of them failing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "dead" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, text=">https://files.catbox.moe/ok.png<")

    with _client(handler) as client:
        images = extractors.resolve_gallery_urls(
            client,
            ["https://catbox.moe/c/dead", "https://catbox.moe/c/live"],
        )

    assert [i.url for i in images] == ["https://files.catbox.moe/ok.png"]


@pytest.mark.parametrize("status", [404, 410])
def test_a_deleted_album_is_gone_not_unreachable(status):
    """Elle_4345977 links an imgchest post that 404s. Returned as None, it
    kept the card unsatisfied and re-resolved every gallery on every run."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with _client(handler) as client, pytest.raises(extractors.GalleryGone):
        extractors.resolve_gallery_url(client, "https://imgchest.com/p/m9yx53zvq7q")


def test_a_server_error_is_still_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with _client(handler) as client:
        assert extractors.resolve_gallery_url(client, "https://imgchest.com/p/m9yx53zvq7q") is None


def test_a_gone_album_does_not_sink_the_rest():
    def handler(request: httpx.Request) -> httpx.Response:
        if "dead" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, text=">https://files.catbox.moe/ok.png<")

    with _client(handler) as client:
        images = extractors.resolve_gallery_urls(
            client,
            ["https://catbox.moe/c/dead", "https://catbox.moe/c/live"],
        )

    assert [i.url for i in images] == ["https://files.catbox.moe/ok.png"]


def test_mega_dispatches_through_fetch_not_parse():
    # extractor.fetch (not .parse) means resolve_gallery_urls must never call
    # _get_text on a mega.nz page -- listing goes through MEGA's API instead.
    extractor = extractors.extractor_for("https://mega.nz/folder/xyz#key")
    assert extractor is not None
    assert extractor.id == "mega"
    assert extractor.fetch is not None
    assert extractor.parse is None


# ---- chub gallery (first-party, not URL-triggered) --------------------------


@pytest.fixture(autouse=True)
def _no_real_settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `resolve_chub_gallery` reads `chubToken` from settings.json the same
    # narrow way `runtime.net` reads `httpProxyUrl` -- redirect it, or these
    # tests read (and could be flaky against) the developer's real file.
    monkeypatch.setattr(settings, "settings_file", tmp_path / "settings.json")


def test_chub_gallery_reads_nodes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/gallery/project/555"
        return httpx.Response(
            200,
            json={
                "nodes": [
                    {"primary_image_path": "https://cdn.chub.ai/a.webp", "uuid": "1"},
                    {"primary_image_path": "https://cdn.chub.ai/b.webp", "uuid": "2"},
                ]
            },
        )

    with _client(handler) as client:
        images = extractors.resolve_chub_gallery(client, "555")

    assert [i.url for i in images] == [
        "https://cdn.chub.ai/a.webp",
        "https://cdn.chub.ai/b.webp",
    ]


def test_chub_gallery_no_project_id_short_circuits():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch with no project id")

    with _client(handler) as client:
        assert extractors.resolve_chub_gallery(client, "") == []


def test_chub_gallery_failure_is_unreachable_not_empty():
    """None, not `[]`: an unreachable gallery must leave the card unsatisfied
    so the next run retries, where a genuinely empty one is settled."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with _client(handler) as client:
        assert extractors.resolve_chub_gallery(client, "555") is None


def test_chub_gallery_attaches_saved_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"chubToken": "secret-token"}')
    monkeypatch.setattr(settings, "settings_file", settings_file)

    seen_auth = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth["value"] = request.headers.get("authorization")
        return httpx.Response(200, json={"nodes": []})

    with _client(handler) as client:
        extractors.resolve_chub_gallery(client, "555")

    assert seen_auth["value"] == "Bearer secret-token"
