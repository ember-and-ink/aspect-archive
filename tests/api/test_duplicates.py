"""`GET /api/v1/duplicates` -- same-creator near-duplicate detection.

Builds its own tiny archive (not `populated_archive`) so avatar similarity is
under full control: a solid-colour fill (the shared `card_png` default) has no
pixel above its own average and every flat colour hashes identically, which
would make `populated_archive`'s Abbie/Cleo (same creator, both default
colour) look avatar-identical for reasons that have nothing to do with this
feature. Real cards have texture; these fixtures use a genuine two-tone split
so "same avatar" and "different avatar" mean what they say.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tests.conftest import card_png, jai_extensions


def _split_image(vertical: bool, size: tuple[int, int] = (384, 576)) -> Image.Image:
    """A real, non-flat avatar: half black, half white. Two images with the
    same split orientation hash identically; different orientations land far
    apart -- unlike any pair of flat `colour` fills, which always hash to the
    same thing regardless of the actual colour."""
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    box = (0, 0, size[0] // 2, size[1]) if vertical else (0, 0, size[0], size[1] // 2)
    draw.rectangle(box, fill=(0, 0, 0))
    return image


@pytest.fixture
def client(archive_dirs: dict[str, Path]):
    """Overrides `tests/conftest.py`'s `client` fixture for this module only,
    skipping `populated_archive` -- see the module docstring for why."""
    from fastapi.testclient import TestClient

    from aspect.server import app

    with TestClient(app) as test_client:
        yield test_client


def _write(characters: Path, filename: str, **kwargs) -> None:
    (characters / filename).write_bytes(card_png(**kwargs))


def test_same_creator_same_avatar_groups_despite_different_description(
    client, archive_dirs: dict[str, Path]
):
    """The Olivia/StormSight shape: one creator, same picture, one card's
    description is a much longer rewrite of the other's."""
    characters = archive_dirs["characters"]
    same_avatar = _split_image(vertical=True)
    _write(
        characters,
        "Olivia_11111111.png",
        name="Olivia",
        creator="StormSight",
        description="Olivia is a quiet character. " * 5,
        image=same_avatar,
        extensions=jai_extensions("11111111-0000-0000-0000-000000000000", creator_name="StormSight"),
    )
    _write(
        characters,
        "Olivia_22222222.png",
        name="Olivia",
        creator="StormSight",
        description="Olivia is a quiet character, expanded with much more backstory and detail. " * 5,
        image=same_avatar,
        extensions=jai_extensions("22222222-0000-0000-0000-000000000000", creator_name="StormSight"),
    )

    client.post("/api/v1/refresh")  # the cards were written behind the server's back
    body = client.get("/api/v1/duplicates").json()
    groups = body["groups"]
    assert len(groups) == 1
    group = groups[0]
    assert group["creator"] == "StormSight"
    assert {m["id"] for m in group["members"]} == {"Olivia_11111111.png", "Olivia_22222222.png"}
    pair = group["pairs"][0]
    assert pair["strength"] == "strong"
    assert pair["avatar_distance"] == 0
    assert "identical avatar" in pair["reasons"]


def test_same_creator_same_name_different_avatar_is_weak_or_absent(
    client, archive_dirs: dict[str, Path]
):
    """The EverNever/"Alpha" shape: same creator, same base name, but a
    different picture and unrelated prose -- likely two different characters
    that happen to share a name. Must never be silently treated as a strong
    duplicate."""
    characters = archive_dirs["characters"]
    _write(
        characters,
        "Alpha_33333333.png",
        name="Alpha",
        creator="EverNever",
        description="Alpha Prelude is a android protagonist in a sci-fi story.",
        first_mes="Systems online.",
        image=_split_image(vertical=True),
        extensions=jai_extensions("33333333-0000-0000-0000-000000000000", creator_name="EverNever"),
    )
    _write(
        characters,
        "Alpha_44444444.png",
        name="Alpha",
        creator="EverNever",
        description="Alpha the Songaloid performs concerts for her fans.",
        first_mes="The crowd roars as the lights dim.",
        image=_split_image(vertical=False),
        extensions=jai_extensions("44444444-0000-0000-0000-000000000000", creator_name="EverNever"),
    )

    client.post("/api/v1/refresh")  # the cards were written behind the server's back
    body = client.get("/api/v1/duplicates").json()
    groups = [g for g in body["groups"] if g["creator"] == "EverNever"]
    if not groups:
        return  # acceptable: name-only overlap with no other evidence never groups
    assert len(groups) == 1
    pair = groups[0]["pairs"][0]
    assert pair["strength"] == "weak"
    assert pair["avatar_distance"] is not None and pair["avatar_distance"] > 12


def test_different_creators_never_grouped_even_when_identical(
    client, archive_dirs: dict[str, Path]
):
    """The hard constraint: same name, same avatar, near-identical text -- but
    different creators. Must never appear together in any group, and since
    each creator only has one card here, neither should appear at all."""
    characters = archive_dirs["characters"]
    same_avatar = _split_image(vertical=True)
    _write(
        characters,
        "Twin_55555555.png",
        name="Twin",
        creator="CreatorA",
        description="Exactly the same description text.",
        image=same_avatar,
        extensions=jai_extensions("55555555-0000-0000-0000-000000000000", creator_name="CreatorA"),
    )
    _write(
        characters,
        "Twin_66666666.png",
        name="Twin",
        creator="CreatorB",
        description="Exactly the same description text.",
        image=same_avatar,
        extensions=jai_extensions("66666666-0000-0000-0000-000000000000", creator_name="CreatorB"),
    )

    client.post("/api/v1/refresh")  # the cards were written behind the server's back
    body = client.get("/api/v1/duplicates").json()
    member_ids = {m["id"] for g in body["groups"] for m in g["members"]}
    assert "Twin_55555555.png" not in member_ids
    assert "Twin_66666666.png" not in member_ids
    assert body["scanned"] == 0


def test_lone_card_creator_excluded(client, archive_dirs: dict[str, Path]):
    """A creator with a single card has nothing to duplicate -- it must never
    appear in the response at all, even alongside an unrelated duplicate
    group."""
    characters = archive_dirs["characters"]
    same_avatar = _split_image(vertical=True)
    _write(
        characters,
        "Dupe_77777777.png",
        name="Dupe",
        creator="BusyCreator",
        image=same_avatar,
        extensions=jai_extensions("77777777-0000-0000-0000-000000000000", creator_name="BusyCreator"),
    )
    _write(
        characters,
        "Dupe_88888888.png",
        name="Dupe",
        creator="BusyCreator",
        image=same_avatar,
        extensions=jai_extensions("88888888-0000-0000-0000-000000000000", creator_name="BusyCreator"),
    )
    _write(
        characters,
        "Solo_99999999.png",
        name="Solo",
        creator="LoneCreator",
        image=same_avatar,
        extensions=jai_extensions("99999999-0000-0000-0000-000000000000", creator_name="LoneCreator"),
    )

    client.post("/api/v1/refresh")  # the cards were written behind the server's back
    body = client.get("/api/v1/duplicates").json()
    member_ids = {m["id"] for g in body["groups"] for m in g["members"]}
    assert "Solo_99999999.png" not in member_ids
    assert member_ids == {"Dupe_77777777.png", "Dupe_88888888.png"}
    assert body["scanned"] == 2
