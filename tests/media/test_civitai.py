"""`aspect/media/civitai.py` -- Civitai post/image galleries.

The shapes below are trimmed from real 2026-09-04 responses: `/api/v1/images`
answers `{"items": [...]}` with `url` already pointing at the `original=true`
CDN variant, and `/images/{id}` renders both an `original=true` and a
`width=450` URL into its markup.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aspect.config import settings
from aspect.media import civitai, extractors


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _image(image_id: int, uuid: str, *, kind: str = "image") -> dict:
    return {
        "id": image_id,
        "type": kind,
        "url": f"https://image.civitai.com/xG1nkq/{uuid}/original=true/{uuid}.jpeg",
    }


# ---- URL recognition -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://civitai.com/posts/1981754",
        "https://civitai.red/posts/1981754",
        "https://civitai.com/images/9173928",
        "https://civitai.red/images/9173928",
    ],
)
def test_recognized_urls_route_to_the_extractor(url: str):
    assert extractors.extractor_for(url).id == "civitai"


@pytest.mark.parametrize(
    "url",
    [
        # A model page's images belong to a model version, not to anything the
        # card linked.
        "https://civitai.com/models/1234",
        "https://civitai.com/user/someone",
        # Already an image: the embedded phase handles it, and matching it here
        # would fetch the same bytes twice under a different prefix.
        "https://image.civitai.com/xG1nkq/abc/original=true/abc.jpeg",
    ],
)
def test_unrecognized_civitai_urls_are_left_alone(url: str):
    assert extractors.extractor_for(url) is None


def test_find_gallery_urls_picks_civitai_links_out_of_prose():
    text = """
    Gallery: https://civitai.com/posts/1981754.
    One shot: (https://civitai.red/images/9173928)
    ![](https://image.civitai.com/xG1nkq/abc/original=true/abc.jpeg)
    """
    assert extractors.find_gallery_urls(text) == [
        "https://civitai.com/posts/1981754",
        "https://civitai.red/images/9173928",
    ]


# ---- posts -----------------------------------------------------------------


def test_post_merges_both_hosts_and_dedupes_by_image_id():
    """`.red` exposes images hidden on the main site and vice versa, so both
    are queried; an image both return counts once."""
    per_host = {
        "civitai.com": [_image(1, "aaa"), _image(2, "bbb")],
        "civitai.red": [_image(2, "bbb"), _image(3, "ccc")],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["postId"] == "1981754"
        assert request.url.params["nsfw"] == "X"
        return httpx.Response(200, json={"items": per_host[request.url.host]})

    with _client(handler) as client:
        images = civitai.extract_images(client, "https://civitai.com/posts/1981754")

    assert [i.url for i in images] == [
        "https://image.civitai.com/xG1nkq/aaa/original=true/aaa.jpeg",
        "https://image.civitai.com/xG1nkq/bbb/original=true/bbb.jpeg",
        "https://image.civitai.com/xG1nkq/ccc/original=true/ccc.jpeg",
    ]
    assert images[0].filename == "aaa.jpeg"


def test_one_host_failing_does_not_sink_the_other():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "civitai.red":
            return httpx.Response(503)
        return httpx.Response(200, json={"items": [_image(1, "aaa")]})

    with _client(handler) as client:
        images = civitai.extract_images(client, "https://civitai.com/posts/1981754")

    assert [i.url for i in images] == ["https://image.civitai.com/xG1nkq/aaa/original=true/aaa.jpeg"]


def test_non_image_items_never_enter_the_list():
    """The images-only policy's discovery point. A post's videos are dropped
    here rather than fetched and then refused by the sniff."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [_image(1, "aaa"), _image(2, "bbb", kind="video")]})

    with _client(handler) as client:
        images = civitai.extract_images(client, "https://civitai.com/posts/1981754")

    assert [i.url for i in images] == ["https://image.civitai.com/xG1nkq/aaa/original=true/aaa.jpeg"]


def test_malformed_json_is_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>", headers={"content-type": "text/html"})

    with _client(handler) as client:
        assert civitai.extract_images(client, "https://civitai.com/posts/1981754") == []


# ---- single images ---------------------------------------------------------


def test_image_url_uses_the_imageid_param():
    """The reference extractor scraped HTML here because it believed the public
    API could not look up an image by id. It can (verified 2026-09-04), so the
    documented endpoint comes first and the scrape is only a fallback."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"items": [_image(9173928, "ddd")]})

    with _client(handler) as client:
        images = civitai.extract_images(client, "https://civitai.com/images/9173928")

    assert all("imageId=9173928" in url for url in seen)
    assert not any("/images/9173928" in url and "api" not in url for url in seen)
    assert [i.url for i in images] == ["https://image.civitai.com/xG1nkq/ddd/original=true/ddd.jpeg"]


_PAGE_HTML = """
<html><body>
<img src="https://image.civitai.com/xG1nkq/cc242d6c/width=450/cc242d6c.jpeg">
<meta content="https://image.civitai.com/xG1nkq/cc242d6c/original=true/cc242d6c.jpeg">
</body></html>
"""


def test_html_fallback_runs_only_when_the_api_is_empty_and_prefers_the_original():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, text=_PAGE_HTML, headers={"content-type": "text/html"})

    with _client(handler) as client:
        images = civitai.extract_images(client, "https://civitai.com/images/9173928")

    assert [i.url for i in images] == [
        "https://image.civitai.com/xG1nkq/cc242d6c/original=true/cc242d6c.jpeg"
    ]


def test_html_fallback_prefers_the_widest_variant_when_none_is_original():
    # A real CDN uuid, because `_CDN_UUID_RE` insists on one -- a short token
    # in that slot is not an image id and must not be treated as one.
    html = """
    <img src="https://image.civitai.com/xG1nkq/94884ddc/width=450/94884ddc.jpeg">
    <img src="https://image.civitai.com/xG1nkq/94884ddc/width=1200/94884ddc.jpeg">
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    with _client(handler) as client:
        images = civitai.extract_images(client, "https://civitai.com/images/9173928")

    assert [i.url for i in images] == ["https://image.civitai.com/xG1nkq/94884ddc/width=1200/94884ddc.jpeg"]


# ---- auth ------------------------------------------------------------------


def test_no_authorization_header_without_a_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("authorization")
        return httpx.Response(200, json={"items": []})

    with _client(handler) as client:
        civitai.extract_images(client, "https://civitai.com/posts/1981754")

    assert set(seen.values()) == {None}


def test_saved_key_is_sent_as_a_bearer_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Read from `data/settings.json`'s root `civitaiApiKey`, the same flat key
    and the same defensive read as `chubToken` (`extractors._settings_key`)."""
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"civitaiApiKey": "civ-secret"}')
    monkeypatch.setattr(settings, "settings_file", settings_file)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("authorization")
        return httpx.Response(200, json={"items": []})

    with _client(handler) as client:
        extractors.resolve_gallery_url(client, "https://civitai.com/posts/1981754")

    assert set(seen.values()) == {"Bearer civ-secret"}


def test_an_unreadable_settings_file_degrades_to_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{not json")
    monkeypatch.setattr(settings, "settings_file", settings_file)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json={"items": []})

    with _client(handler) as client:
        assert extractors.resolve_gallery_url(client, "https://civitai.com/posts/1981754") == []
