"""Forks -- docs/FORKS_AND_EXTRAS_PLAN.md §3.

Two primitives: `POST /characters/{id}/fork` (born here, a full copy the
caller rewrites) and `POST /characters` with `fork_of=` (adopted, a file that
already is a rewrite). Both share one invariant worth testing hardest:
lineage is flattened, not chained -- forking a fork produces a sibling of the
existing fork, and `fork.of` always names the root original.
"""

from __future__ import annotations

from aspect.cards import pngtools
from tests.conftest import card_png, jai_extensions


def read_card(raw: bytes) -> dict:
    envelope = pngtools.read_envelope(raw)
    assert envelope is not None
    return envelope[1]


def post_import(client, png: bytes, name: str = "upload.png", **data):
    return client.post(
        "/api/v1/characters",
        files={"file": (name, png, "image/png")},
        data=data,
    )


# --- born here: POST /characters/{id}/fork ----------------------------------


def test_forking_mints_a_fresh_fragment_and_shares_the_gallery(client, archive_dirs):
    resp = client.post("/api/v1/characters/Abbie_0d162f5f.png/fork")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["id"] != "Abbie_0d162f5f.png"
    assert body["fragment"] != "0d162f5f"
    assert body["gallery_id"] == "kzbYR2QbpncC"  # the parent's, forced
    assert body["is_fork"] is True
    assert body["forked_from"] == {"id": "Abbie_0d162f5f.png", "name": "Abbie"}

    on_disk = read_card((archive_dirs["characters"] / body["id"]).read_bytes())
    fork = on_disk["extensions"]["fork"]
    assert fork["of"] == "0d162f5f"
    assert fork["of_filename"] == "Abbie_0d162f5f.png"
    assert on_disk["extensions"]["jai"]["sourceKind"] == "fork"
    # Attribution clones from the parent -- only the id and sourceKind change.
    assert on_disk["extensions"]["jai"]["creatorName"] == "KornyPony"
    # A fork starts as a full copy of the parent's text.
    assert on_disk["description"] == "Abbie is a test character."


def test_forking_twice_does_not_collide(client):
    first = client.post("/api/v1/characters/Abbie_0d162f5f.png/fork").json()
    second = client.post("/api/v1/characters/Abbie_0d162f5f.png/fork").json()
    assert first["id"] != second["id"]
    assert first["fragment"] != second["fragment"]


def test_forking_a_fork_produces_a_sibling_not_a_grandchild(client):
    """The flattening decision: fork.of always names the root, never an
    intermediate fork -- so a rewrite of a rewrite is a sibling of the first
    fork, not one level deeper."""
    child = client.post("/api/v1/characters/Abbie_0d162f5f.png/fork").json()
    grandchild = client.post(f"/api/v1/characters/{child['id']}/fork").json()

    assert grandchild["forked_from"] == {"id": "Abbie_0d162f5f.png", "name": "Abbie"}
    assert grandchild["forked_from"] != {"id": child["id"], "name": child["name"]}

    listing = client.get("/api/v1/characters?fork_of=0d162f5f&limit=0").json()
    ids = {item["id"] for item in listing["items"]}
    assert ids == {child["id"], grandchild["id"]}


def test_a_fork_survives_its_parent_being_deleted(client):
    fork = client.post("/api/v1/characters/Abbie_0d162f5f.png/fork").json()
    resp = client.delete("/api/v1/characters/Abbie_0d162f5f.png")
    assert resp.status_code == 200, resp.text

    detail = client.get(f"/api/v1/characters/{fork['id']}").json()
    assert detail["forked_from"] is None  # dangling, not an error
    assert detail["card"]["extensions"]["fork"]["of"] == "0d162f5f"


# --- adopted: POST /characters?fork_of= -------------------------------------


def test_adopting_a_file_as_a_fork_forces_the_parent_gallery_and_stamps_fork(client, archive_dirs):
    png = card_png("Abbie Rewrite", creator="someone else")
    resp = post_import(client, png, fork_of="0d162f5f", note="expanded dialogue")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    on_disk = read_card((archive_dirs["characters"] / body["id"]).read_bytes())
    assert on_disk["extensions"]["gallery_id"] == "kzbYR2QbpncC"
    fork = on_disk["extensions"]["fork"]
    assert fork["of"] == "0d162f5f"
    assert fork["of_filename"] == "Abbie_0d162f5f.png"
    assert fork["note"] == "expanded dialogue"


def test_adopting_the_same_forked_file_twice_dedupes(client):
    png = card_png("Abbie Rewrite", creator="someone else")
    first = post_import(client, png, fork_of="0d162f5f").json()
    second = post_import(client, png, fork_of="0d162f5f").json()
    assert second["duplicate"] is True
    assert second["id"] == first["id"]


def test_adopting_a_rewrite_that_kept_the_parents_id_gets_one_of_its_own(client, archive_dirs):
    """The collision the fork-import path exists to survive: a card exported to
    SillyTavern, rewritten and handed back still carries the id it was built
    with, which is its parent's -- and the fragment alone is the dedupe key, so
    without a new id the rewrite is waved through as its own parent's duplicate
    and never lands."""
    png = card_png(
        "Abbie Rewritten",
        description="A different Abbie entirely.",
        extensions=jai_extensions(),  # the parent's own id and gallery
    )
    resp = post_import(client, png, fork_of="0d162f5f")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["duplicate"] is False
    assert body["id"] != "Abbie_0d162f5f.png"

    on_disk = read_card((archive_dirs["characters"] / body["id"]).read_bytes())
    assert on_disk["extensions"]["jai"]["id"] != "0d162f5f-86ab-4fdd-a2c2-3912adf24960"
    assert on_disk["extensions"]["fork"]["of"] == "0d162f5f"
    assert on_disk["extensions"]["gallery_id"] == "kzbYR2QbpncC"  # shared, still

    # The parent is untouched -- the collision must not have overwritten it.
    parent = read_card((archive_dirs["characters"] / "Abbie_0d162f5f.png").read_bytes())
    assert parent["description"] == "Abbie is a test character."


def test_adopting_a_rewrite_carrying_the_parents_provider_id(client, archive_dirs):
    """Same collision by the other route: the id arrives in a provider block
    rather than ours, which is what a card acquired from Chub or DataCat and
    then rewritten looks like."""
    png = card_png(
        "Abbie Rewritten",
        description="A different Abbie entirely.",
        extensions={"datacat": {"id": "0d162f5f-86ab-4fdd-a2c2-3912adf24960"}},
    )
    body = post_import(client, png, fork_of="0d162f5f").json()

    assert body["duplicate"] is False
    on_disk = read_card((archive_dirs["characters"] / body["id"]).read_bytes())
    assert on_disk["extensions"]["fork"]["of"] == "0d162f5f"
    # The provider block keeps its own id; only the archive's identity moves.
    assert on_disk["extensions"]["datacat"]["id"] == "0d162f5f-86ab-4fdd-a2c2-3912adf24960"
    assert on_disk["extensions"]["jai"]["id"] != "0d162f5f-86ab-4fdd-a2c2-3912adf24960"


def test_a_reidentified_fork_still_dedupes_against_itself(client):
    """The new id is derived from the rewrite's own text, not minted at random,
    so dropping the same file twice is still caught as one card."""
    png = card_png(
        "Abbie Rewritten",
        description="A different Abbie entirely.",
        extensions=jai_extensions(),
    )
    first = post_import(client, png, fork_of="0d162f5f").json()
    second = post_import(client, png, fork_of="0d162f5f").json()
    assert second["duplicate"] is True
    assert second["id"] == first["id"]


def test_adopting_a_fork_of_a_fork_flattens_to_the_root(client):
    child = client.post("/api/v1/characters/Abbie_0d162f5f.png/fork").json()
    png = card_png("Adopted Grandchild", creator="someone else")
    resp = post_import(client, png, fork_of=child["fragment"])
    assert resp.status_code == 200, resp.text

    detail = client.get(f"/api/v1/characters/{resp.json()['id']}").json()
    assert detail["card"]["extensions"]["fork"]["of"] == "0d162f5f"


def test_fork_of_a_bogus_fragment_is_422(client):
    resp = post_import(client, card_png("Nobody"), fork_of="deadbeef")
    assert resp.status_code == 422


# --- filtering and the gallery claimants fix --------------------------------


def test_is_fork_filter(client):
    fork = client.post("/api/v1/characters/Abbie_0d162f5f.png/fork").json()

    forks_only = client.get("/api/v1/characters?is_fork=true&limit=0").json()
    assert {item["id"] for item in forks_only["items"]} == {fork["id"]}

    originals_only = client.get("/api/v1/characters?is_fork=false&limit=0").json()
    assert fork["id"] not in {item["id"] for item in originals_only["items"]}
    assert "Abbie_0d162f5f.png" in {item["id"] for item in originals_only["items"]}


def test_galleries_route_reports_every_claimant(client):
    fork = client.post("/api/v1/characters/Abbie_0d162f5f.png/fork").json()

    folders = client.get("/api/v1/galleries").json()
    abbie_folder = next(f for f in folders if f["folder"] == "Abbie_kzbYR2QbpncC")
    assert set(abbie_folder["card_ids"]) == {"Abbie_0d162f5f.png", fork["id"]}
