"""`aspect/media/mega.py` -- MEGA folder listing + decrypt, ported from
`web/modules/gallery-extractors/mega.js`.

The fixtures below build a small, real MEGA-shaped folder (root -> one file,
root -> subfolder -> one file, plus a non-image file to prove filtering)
using pycryptodome directly to encrypt, independently of the module under
test, so a bug in the key-hierarchy walk, the attribute decrypt, or the
pseudo-URL round trip actually fails these tests rather than passing by
construction.
"""

from __future__ import annotations

import base64
import json
import os

import httpx
import pytest
from Crypto.Cipher import AES
from Crypto.Util import Counter

from aspect.media import mega

MASTER_KEY = b"0123456789abcdef"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _ecb_encrypt(key: bytes, data: bytes) -> bytes:
    return AES.new(key, AES.MODE_ECB).encrypt(data)


def _cbc_encrypt_attrs(key: bytes, filename: str) -> bytes:
    plaintext = f'MEGA{{"n":"{filename}"}}'.encode()
    return AES.new(key, AES.MODE_CBC, iv=bytes(16)).encrypt(plaintext + b"\x00" * (-len(plaintext) % 16))


def _ctr_encrypt(key: bytes, nonce: bytes, data: bytes) -> bytes:
    return AES.new(key, AES.MODE_CTR, counter=Counter.new(64, prefix=nonce, initial_value=0)).encrypt(data)


def _file_node(handle: str, parent_handle: str, parent_key: bytes, filename: str, content: bytes) -> tuple[dict, bytes]:
    """A file node keyed under `parent_key`, plus the encrypted bytes it
    should decrypt back into `content`."""
    raw = os.urandom(32)
    file_key = bytes(a ^ b for a, b in zip(raw[:16], raw[16:32]))
    nonce = raw[16:24]
    node = {
        "h": handle,
        "p": parent_handle,
        "t": 0,
        "k": f"{parent_handle}:{_b64(_ecb_encrypt(parent_key, raw))}",
        "a": _b64(_cbc_encrypt_attrs(file_key, filename)),
        "s": len(content),
    }
    return node, _ctr_encrypt(file_key, nonce, content)


def _folder_node(handle: str, parent_handle: str, parent_key: bytes) -> tuple[dict, bytes]:
    """A subfolder keyed under `parent_key`; returns the node and its own
    16-byte key (for building children under it)."""
    key = os.urandom(16)
    node = {
        "h": handle,
        "p": parent_handle,
        "t": 1,
        "k": f"{parent_handle}:{_b64(_ecb_encrypt(parent_key, key))}",
    }
    return node, key


PIC_CONTENT = b"pretend-this-is-a-png\x89PNG\r\n"
DEEP_CONTENT = b"a-deeply-nested-file"

FOLDER_ID = "abc123folder"
FOLDER_URL = f"https://mega.nz/folder/{FOLDER_ID}#{_b64(MASTER_KEY)}"

root_node = {"h": "root", "p": "outside-this-listing", "t": 1}
pic_node, pic_encrypted = _file_node("pic", "root", MASTER_KEY, "photo.png", PIC_CONTENT)
zip_node, _zip_encrypted = _file_node("zip", "root", MASTER_KEY, "archive.zip", b"not an image")
sub_node, sub_key = _folder_node("sub", "root", MASTER_KEY)
deep_node, deep_encrypted = _file_node("deep", "sub", sub_key, "deep.jpg", DEEP_CONTENT)

ALL_NODES = [root_node, pic_node, zip_node, sub_node, deep_node]
ENCRYPTED_BY_HANDLE = {"pic": pic_encrypted, "deep": deep_encrypted}
DOWNLOAD_HOST = "https://gfs123n456.userstorage.mega.co.nz"


def _mega_transport(handler):
    return httpx.MockTransport(handler)


def _list_and_download_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host and request.url.host.endswith("mega.co.nz") and "g.api" in str(request.url):
        commands = json.loads(request.content)
        cmd = commands[0]
        if cmd["a"] == "f":
            return httpx.Response(200, json=[{"f": ALL_NODES}])
        if cmd["a"] == "g":
            handle = cmd["n"]
            return httpx.Response(200, json=[{"g": f"{DOWNLOAD_HOST}/dl/{handle}"}])
        return httpx.Response(200, json=[-1])
    if request.url.host == "gfs123n456.userstorage.mega.co.nz":
        handle = str(request.url).rsplit("/", 1)[-1]
        return httpx.Response(200, content=ENCRYPTED_BY_HANDLE[handle])
    return httpx.Response(404)


# ---- folder URL parsing -----------------------------------------------------


def test_parses_new_format():
    assert mega.parse_folder_url(FOLDER_URL) == (FOLDER_ID, MASTER_KEY)


def test_parses_legacy_format():
    url = f"https://mega.nz/#F!{FOLDER_ID}!{_b64(MASTER_KEY)}"
    assert mega.parse_folder_url(url) == (FOLDER_ID, MASTER_KEY)


def test_rejects_non_mega_urls():
    assert mega.parse_folder_url("https://example.com/folder/x#y") is None


def test_is_mega_url():
    assert mega.is_mega_url("mega://abc/def?k=x&n=y&s=1")
    assert not mega.is_mega_url("https://mega.nz/folder/x")


# ---- listing: key hierarchy + attribute decrypt + image filter -------------


def test_extract_images_walks_the_key_hierarchy_and_filters_to_images():
    with httpx.Client(transport=_mega_transport(_list_and_download_handler)) as client:
        images = mega.extract_images(client, FOLDER_URL)

    names = sorted(i.filename for i in images)
    # archive.zip is excluded (not an image extension); photo.png (root) and
    # deep.jpg (two levels down, through the subfolder's own encrypted key)
    # both resolve correctly.
    assert names == ["deep.jpg", "photo.png"]
    assert all(mega.is_mega_url(i.url) for i in images)


def test_extract_images_empty_folder_yields_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"f": []}])

    with httpx.Client(transport=_mega_transport(handler)) as client:
        assert mega.extract_images(client, FOLDER_URL) == []


def test_extract_images_api_error_is_caught_not_raised():
    """One failing folder must not sink a card's media run."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[-9])

    with httpx.Client(transport=_mega_transport(handler)) as client:
        assert mega.extract_images(client, FOLDER_URL) == []


def test_extract_images_invalid_url_yields_nothing():
    with httpx.Client(transport=_mega_transport(_list_and_download_handler)) as client:
        assert mega.extract_images(client, "https://mega.nz/not-a-folder-link") == []


# ---- download-time fetch + decrypt ------------------------------------------


async def test_fetch_and_decrypt_round_trips_real_content():
    with httpx.Client(transport=_mega_transport(_list_and_download_handler)) as client:
        images = mega.extract_images(client, FOLDER_URL)
    pic = next(i for i in images if i.filename == "photo.png")

    async with httpx.AsyncClient(transport=_mega_transport(_list_and_download_handler)) as client:
        body, content_type, error = await mega.fetch_and_decrypt(client, pic.url)

    assert error is None
    assert content_type is None
    assert body == PIC_CONTENT


async def test_fetch_and_decrypt_invalid_reference():
    async with httpx.AsyncClient(transport=_mega_transport(_list_and_download_handler)) as client:
        body, content_type, error = await mega.fetch_and_decrypt(client, "mega://bad")

    assert body is None
    assert error == "invalid mega reference"


async def test_fetch_and_decrypt_api_error_surfaces_as_error_not_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[-9])

    async with httpx.AsyncClient(transport=_mega_transport(_list_and_download_handler)) as client:
        images = mega.extract_images(httpx.Client(transport=_mega_transport(_list_and_download_handler)), FOLDER_URL)
    pic = next(i for i in images if i.filename == "photo.png")

    async with httpx.AsyncClient(transport=_mega_transport(handler)) as client:
        body, content_type, error = await mega.fetch_and_decrypt(client, pic.url)

    assert body is None
    assert error is not None
