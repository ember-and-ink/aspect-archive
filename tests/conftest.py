"""Shared fixtures for the archive tests.

The archive under test is a real directory of real card PNGs -- built here rather
than mocked, because every bug this code has had came from the *bytes*: a JPEG
behind a `.png` name, a tEXt chunk stripped by a re-encode, a filename that
differs from its thumb only in case. A fake filesystem would have hidden all
three.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from aspect import deps
from aspect.archive import catalog
from aspect.cards import pngtools
from aspect.cards.builder import PngWriter
from aspect.archive import thumbs
from aspect.api.v1 import _shared as v1_shared
from aspect.config import settings


def card_png(
    name: str = "Test",
    *,
    # 2:3, and comfortably larger than any thumbnail tier -- real cards are, and
    # the thumbnailer never upscales, so a fixture smaller than a thumb box would
    # make every geometry assertion in the suite test the fixture instead.
    size: tuple[int, int] = (384, 576),
    colour: tuple[int, int, int, int] = (120, 80, 200, 255),
    image: Image.Image | None = None,
    envelope_extra: dict[str, Any] | None = None,
    **fields: Any,
) -> bytes:
    """A V3 card PNG: real pixels, real tEXt chunks, both spec keys.

    `fields` go straight into the card's `data`, so a test says what it is about
    (`tags=[...]`, `extensions={...}`) and inherits a valid card around it.
    `envelope_extra` writes *outside* `data`, which is where SillyTavern keeps
    the handful of root-only fields (`create_date`, `fav`) -- the ones a re-embed
    drops unless something carries them across.

    `image` overrides the default flat `colour` fill with real pixels -- needed
    by anything comparing avatars for similarity, since a solid-colour fill has
    no pixel above its own average and every flat colour hashes identically.
    """
    data: dict[str, Any] = {
        "name": name,
        "description": f"{name} is a test character.",
        "personality": "",
        "scenario": "",
        "first_mes": f"Hello, I am {name}.",
        "mes_example": "",
        "creator": "tester",
        "creator_notes": "",
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [],
        "tags": [],
        "character_version": "1",
        "extensions": {},
    }
    data.update(fields)
    envelope = {"spec": "chara_card_v3", "spec_version": "3.0", "data": data, **data}
    envelope.update(envelope_extra or {})

    buffer = io.BytesIO()
    (image or Image.new("RGBA", size, colour)).save(buffer, "PNG")
    payload = base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")
    return pngtools.inject_text_chunks(buffer.getvalue(), {"chara": payload, "ccv3": payload})


def jai_extensions(
    card_id: str = "0d162f5f-86ab-4fdd-a2c2-3912adf24960",
    *,
    gallery_id: str = "kzbYR2QbpncC",
    source_kind: str = "janitor_core",
    creator_name: str = "tester",
    page_name: str = "A Test Page | Test",
) -> dict[str, Any]:
    """The `extensions` block CardBuilder stamps on every card, which is where
    the index reads provenance from."""
    return {
        "gallery_id": gallery_id,
        "jai": {
            "id": card_id,
            "source_url": f"https://janitorai.com/characters/{card_id}",
            "sourceKind": source_kind,
            "creatorName": creator_name,
            "pageName": page_name,
            "linkedAt": "2026-07-21T17:31:47.257Z",
        },
    }


@pytest.fixture(autouse=True)
def _no_real_login_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the developer's own `data/security.json`.

    `archive_dirs` repoints it too, but the tests that build their own
    TestClient never ask for that fixture -- and with the gate switched on in
    the real archive, each of them answers 401 for a reason unrelated to what it
    is testing.
    """
    monkeypatch.setattr(settings, "security_file", tmp_path / "security.json")


@pytest.fixture(autouse=True)
def queued_downloads(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every card id a write handed to `queue_direct_images`, instead of a real
    background download.

    Autouse because every build and import route now queues one, and a real
    job would fetch the fixture cards' image URLs off the internet in the
    background of an unrelated test. A test of the function itself opts out
    with `@pytest.mark.real_media_queue` (and stubs the download instead).
    """
    calls: list[str] = []
    if request.node.get_closest_marker("real_media_queue"):
        return calls
    from aspect.api.v1 import media as v1_media

    monkeypatch.setattr(v1_media, "queue_direct_images", calls.append)
    return calls


@pytest.fixture
def archive_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """An empty archive laid out exactly like `data/`, with settings pointed at
    it and the process-wide index reset so it is rebuilt against the temp dir."""
    dirs = {
        "characters": tmp_path / "characters",
        "galleries": tmp_path / "galleries",
        "expressions": tmp_path / "expressions",
        "thumbs": tmp_path / "cache" / "thumbs",
    }
    for path in dirs.values():
        path.mkdir(parents=True)
    monkeypatch.setattr(settings, "archive_dir", dirs["characters"])
    monkeypatch.setattr(settings, "galleries_dir", dirs["galleries"])
    monkeypatch.setattr(settings, "expressions_dir", dirs["expressions"])
    monkeypatch.setattr(settings, "thumbs_dir", dirs["thumbs"])
    # Not optional. The settings route WRITES, and the real file is the only
    # copy of the developer's Chub and DataCat tokens -- an unredirected test
    # would overwrite them with whatever it happened to PUT.
    dirs["settings"] = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "settings_file", dirs["settings"])
    # Same reasoning for the cross-character dead-URL ledger (Phase 3C): it is
    # one real file shared by the whole archive, and an unredirected test run
    # permanently poisons it with synthetic test URLs -- which is exactly what
    # happened here before this line existed (`mega://folder/bad` on disk).
    dirs["dead_urls"] = tmp_path / "dead_urls.json"
    monkeypatch.setattr(settings, "dead_urls_file", dirs["dead_urls"])
    # And again for Discover's first-seen stamps, which are never rewritten --
    # a test that wrote to the real file would leave synthetic provider ids in
    # the developer's own Following history, with nothing in the UI to undo it.
    dirs["seen"] = tmp_path / "discover_seen.json"
    monkeypatch.setattr(settings, "seen_file", dirs["seen"])
    # And for the login gate, where the stakes are the other way round: a test
    # that enabled it against the real file would lock the developer out of
    # their own archive behind a password only the test knew.
    dirs["security"] = tmp_path / "security.json"
    monkeypatch.setattr(settings, "security_file", dirs["security"])
    # The index and the thumbnail store are process-wide singletons bound at
    # import time; both have to be re-pointed or a test reads the real 3 GB
    # archive on the developer's machine and passes for the wrong reason.
    monkeypatch.setattr(catalog, "_index", None)
    monkeypatch.setattr(v1_shared, "thumbnail_store", thumbs.ThumbnailStore(dirs["thumbs"], dirs["characters"]))
    # And the writer, for the same reason: PngWriter reads `settings.archive_dir`
    # once, in its constructor, at import time -- so repointing the setting above
    # is not enough, and a write route that goes through `deps.png_writer` (card
    # import) would drop test cards into the developer's real archive.
    monkeypatch.setattr(deps, "png_writer", PngWriter(output_dir=dirs["characters"]))
    return dirs


@pytest.fixture
def populated_archive(archive_dirs: dict[str, Path]) -> dict[str, Path]:
    """Three cards worth telling apart, and one gallery folder.

    Abbie has a gallery on disk and a lorebook; Bella has two alternate
    greetings and shares no tags; Cleo has no gallery folder despite carrying a
    gallery_id, which is the common real case (images never downloaded).
    """
    characters = archive_dirs["characters"]
    (characters / "Abbie_0d162f5f.png").write_bytes(
        card_png(
            "Abbie",
            tags=["Female", "Vampire"],
            creator="KornyPony",
            extensions=jai_extensions(creator_name="KornyPony"),
            character_book={"entries": [{"keys": ["a"], "content": "x"}, {"keys": ["b"], "content": "y"}]},
        )
    )
    (characters / "Bella_11112222.png").write_bytes(
        card_png(
            "Bella",
            tags=["Male"],
            creator="Someone Else",
            alternate_greetings=["hi", "hey"],
            creator_notes="notes here",
            mes_example="<START>\n{{user}}: hi",
            extensions=jai_extensions(
                "11112222-0000-0000-0000-000000000000",
                gallery_id="BBBBBBBBBBBB",
                source_kind="chub_import",
                creator_name="Someone Else",
                page_name="Bella the Second",
            ),
        )
    )
    (characters / "Cleo_33334444.png").write_bytes(
        card_png(
            "Cleo",
            tags=["Female"],
            creator="KornyPony",
            extensions=jai_extensions(
                "33334444-0000-0000-0000-000000000000",
                gallery_id="CCCCCCCCCCCC",
                creator_name="KornyPony",
            ),
        )
    )
    gallery = archive_dirs["galleries"] / "Abbie_kzbYR2QbpncC"
    gallery.mkdir()
    (gallery / "one.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    (gallery / "two.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 200)
    # Same folder-naming scheme as the gallery, under its own root -- the two
    # never collide even though Abbie has both. Real filenames end in .webp;
    # the bytes are JPEG magic, same "extension lies" shape as the inherited
    # avatar cache (aspect/archive/thumbs.py), which sniffing already handles.
    expressions = archive_dirs["expressions"] / "Abbie_kzbYR2QbpncC"
    expressions.mkdir()
    (expressions / "neutral-_00001_.webp").write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    (expressions / "joy-_00001_.webp").write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    return archive_dirs


@pytest.fixture
def client(populated_archive: dict[str, Path]):
    """A TestClient over the real app, with the archive pointed at temp dirs.

    Imported inside the fixture so collecting a test module that does not use it
    never pays for importing the server (and never triggers its startup work).
    """
    from fastapi.testclient import TestClient

    from aspect.server import app

    with TestClient(app) as test_client:
        yield test_client
