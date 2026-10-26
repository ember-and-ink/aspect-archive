import base64
import io

import httpx
from PIL import Image

from aspect.cards.avatar_fetch import PLACEHOLDER_PNG, AvatarFetcher


def _png_bytes(color=(10, 20, 30, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_returns_url_content_on_success():
    good = _png_bytes()

    def handler(request):
        return httpx.Response(200, content=good)

    fetcher = AvatarFetcher(client=_client(handler))
    result = await fetcher.fetch("https://cdn.example/avatar.webp")
    assert result == good


async def test_fetch_falls_back_to_avatar_b64_on_url_failure():
    def handler(request):
        return httpx.Response(500, content=b"boom")

    b64 = base64.b64encode(_png_bytes((1, 2, 3, 255))).decode()
    fetcher = AvatarFetcher(client=_client(handler))
    result = await fetcher.fetch("https://cdn.example/avatar.webp", avatar_b64=b64)
    assert result == base64.b64decode(b64)


async def test_fetch_falls_back_to_avatar_b64_on_network_error():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    b64 = base64.b64encode(_png_bytes()).decode()
    fetcher = AvatarFetcher(client=_client(handler))
    result = await fetcher.fetch("https://cdn.example/avatar.webp", avatar_b64=b64)
    assert result == base64.b64decode(b64)


async def test_fetch_falls_back_to_placeholder_when_nothing_works():
    def handler(request):
        return httpx.Response(404)

    fetcher = AvatarFetcher(client=_client(handler))
    result = await fetcher.fetch("https://cdn.example/missing.webp")
    assert result == PLACEHOLDER_PNG


async def test_fetch_falls_back_to_placeholder_on_invalid_b64():
    fetcher = AvatarFetcher(client=_client(lambda r: httpx.Response(404)))
    result = await fetcher.fetch(None, avatar_b64="not-a-valid-image-at-all")
    assert result == PLACEHOLDER_PNG


async def test_fetch_falls_back_to_placeholder_when_url_and_b64_absent():
    fetcher = AvatarFetcher(client=_client(lambda r: httpx.Response(404)))
    result = await fetcher.fetch(None, None)
    assert result == PLACEHOLDER_PNG


async def test_fetch_rejects_non_image_url_content():
    def handler(request):
        return httpx.Response(200, content=b"<html>not an image</html>")

    fetcher = AvatarFetcher(client=_client(handler))
    result = await fetcher.fetch("https://cdn.example/avatar.webp")
    assert result == PLACEHOLDER_PNG


def test_placeholder_is_a_valid_png():
    img = Image.open(io.BytesIO(PLACEHOLDER_PNG))
    img.verify()
    assert img.format == "PNG"
