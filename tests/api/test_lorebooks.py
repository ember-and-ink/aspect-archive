"""`/api/v1/lorebooks` -- lorebooks derived from the cards that embed them.

Builds its own archive: the point of every test here is which cards share
which entries, so the fixture has to say exactly that and nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import card_png, jai_extensions


def _entry(title: str, content: str) -> dict:
    return {"comment": title, "name": title, "content": content, "keys": [title.lower()]}


def _jai_book(character: str, *sources: tuple[str, str, list[dict]]) -> dict:
    """A book as `LorebookMapper` writes it: sources concatenated, comments
    prefixed with the book title when there is more than one."""
    multi = len(sources) > 1
    entries = []
    for _sid, title, source_entries in sources:
        for entry in source_entries:
            entries.append({**entry, "comment": f"[{title}] {entry['comment']}" if multi else entry["comment"]})
    return {
        "name": f"{character} — JanitorAI lorebooks" if multi else sources[0][1],
        "extensions": {
            "jai_sources": [{"id": sid, "title": title, "entry_count": len(es)} for sid, title, es in sources]
        },
        "entries": entries,
    }


WORLD_V1 = [_entry("Town", "A small town."), _entry("River", "A river.")]
WORLD_V2 = [_entry("Town", "A small town, now with a market."), _entry("River", "A river.")]
PEOPLE = [_entry("Mayor", "The mayor.")]


@pytest.fixture
def client(archive_dirs: dict[str, Path]):
    from fastapi.testclient import TestClient

    from proxy.server import app

    def write(filename: str, name: str, card_id: str, book: dict | None, date: str) -> None:
        fields = {"character_book": book} if book else {}
        (archive_dirs["characters"] / filename).write_bytes(
            card_png(
                name,
                extensions=jai_extensions(card_id, gallery_id=card_id[:12], creator_name="worldsmith"),
                envelope_extra={"create_date": date},
                **fields,
            )
        )

    # Two cards on the old World, one on the new; Cleo also attaches People.
    write("Abbie_aaaaaaaa.png", "Abbie", "aaaaaaaa-0000", _jai_book("Abbie", ("w", "World", WORLD_V1)), "2026-01-01T00:00:00Z")
    write("Bea_bbbbbbbb.png", "Bea", "bbbbbbbb-0000", _jai_book("Bea", ("w", "World", WORLD_V1)), "2026-02-01T00:00:00Z")
    write(
        "Cleo_cccccccc.png",
        "Cleo",
        "cccccccc-0000",
        _jai_book("Cleo", ("w", "World", WORLD_V2), ("p", "People", PEOPLE)),
        "2026-03-01T00:00:00Z",
    )
    # No provider id at all, but the same entries as People -- the same book.
    write("Dana_dddddddd.png", "Dana", "dddddddd-0000", {"name": "Dana Lorebook", "entries": PEOPLE}, "2026-04-01T00:00:00Z")
    write("Eve_eeeeeeee.png", "Eve", "eeeeeeee-0000", None, "2026-05-01T00:00:00Z")

    with TestClient(app) as test_client:
        yield test_client


def _by_name(client) -> dict[str, dict]:
    return {book["name"]: book for book in client.get("/api/v1/lorebooks").json()["lorebooks"]}


def test_books_are_deduplicated_across_cards(client):
    body = client.get("/api/v1/lorebooks").json()
    books = {book["name"]: book for book in body["lorebooks"]}
    assert set(books) == {"World", "People"}
    assert books["World"]["card_count"] == 3
    assert books["World"]["refs"] == ["jai:w"]
    # Dana's id-less copy joined People on content alone.
    assert books["People"]["card_count"] == 2
    assert books["People"]["kinds"] == ["embedded", "janitor"]
    assert body["stats"] == {
        "cards": 4,
        "lorebooks": 2,
        "shared_lorebooks": 2,
        "entries_embedded": 8,
        "entries_unique": 4,
        "changed_entries": 1,
        "unresolved_entries": 1,
        "pending_cards": 0,
        "listed_only": 0,
    }


def test_revisions_newest_first_and_changed_entries_flagged(client):
    world = _by_name(client)["World"]
    assert (world["revision_count"], world["entry_count"], world["changed_entries"]) == (2, 2, 1)

    detail = client.get(f"/api/v1/lorebooks/{world['id']}").json()
    assert [[c["name"] for c in r["cards"]] for r in detail["revisions"]] == [["Cleo"], ["Abbie", "Bea"]]
    # The changed entry leads, and previews its newest text.
    town = detail["entries"][0]
    assert (town["title"], town["version_count"], town["card_count"]) == ("Town", 2, 3)
    assert "market" in town["preview"]


def test_entry_detail_lists_every_version_with_its_cards(client):
    world = _by_name(client)["World"]
    entries = client.get("/api/v1/lorebooks/entries", params={"changed": True}).json()
    assert entries["total"] == 1
    entry = client.get(f"/api/v1/lorebooks/entries/{entries['entries'][0]['id']}").json()
    assert entry["lorebook_id"] == world["id"]
    assert [(v["content"], [c["name"] for c in v["cards"]]) for v in entry["versions"]] == [
        ("A small town, now with a market.", ["Cleo"]),
        ("A small town.", ["Abbie", "Bea"]),
    ]


def test_entries_filter_search_and_page(client):
    everything = client.get("/api/v1/lorebooks/entries").json()
    assert everything["total"] == 3
    people = _by_name(client)["People"]
    scoped = client.get("/api/v1/lorebooks/entries", params={"lorebook": people["id"]}).json()
    assert [e["title"] for e in scoped["entries"]] == ["Mayor"]
    assert client.get("/api/v1/lorebooks/entries", params={"q": "MARKET"}).json()["total"] == 1
    paged = client.get("/api/v1/lorebooks/entries", params={"limit": 1, "offset": 1, "sort": "title"}).json()
    assert [e["title"] for e in paged["entries"]] == ["River"] and paged["total"] == 3


def test_card_lorebooks_in_entry_order_with_staleness(client):
    cleo = client.get("/api/v1/characters/Cleo_cccccccc.png/lorebooks").json()
    assert [(b["name"], b["entry_count"], b["is_newest"]) for b in cleo] == [("World", 2, True), ("People", 1, True)]
    abbie = client.get("/api/v1/characters/Abbie_aaaaaaaa.png/lorebooks").json()
    assert [(b["name"], b["is_newest"], b["revision_count"]) for b in abbie] == [("World", False, 2)]
    assert client.get("/api/v1/characters/Eve_eeeeeeee.png/lorebooks").json() == []


def test_unknown_ids_are_404(client):
    assert client.get("/api/v1/lorebooks/nope").status_code == 404
    assert client.get("/api/v1/lorebooks/entries/nope").status_code == 404
    assert client.get("/api/v1/characters/Nobody.png/lorebooks").status_code == 404


def test_overlapping_books_are_offered_as_similar(client, archive_dirs):
    # A bigger id-less book that contains all of World's newest entries.
    extra = [*WORLD_V2, _entry("Castle", "A castle."), _entry("Forest", "A forest.")]
    (archive_dirs["characters"] / "Fay_ffffffff.png").write_bytes(
        card_png(
            "Fay",
            extensions=jai_extensions("ffffffff-0000", gallery_id="ffffffff0000"),
            character_book={"name": "World (expanded)", "entries": extra},
        )
    )
    from proxy.api.v1 import _shared

    _shared.index().refresh(force=True)
    books = _by_name(client)
    assert books["World (expanded)"]["similar_count"] == 1
    detail = client.get(f"/api/v1/lorebooks/{books['World (expanded)']['id']}").json()
    assert [(s["name"], s["shared"], s["relation"]) for s in detail["similar"]] == [("World", 2, "overlap")]


# --- lorebooks a provider lists but a card does not embed --------------------


def _script(script_id: str, title: str, *, kind: str = "lorebook", author: str = "worldsmith") -> dict:
    """One row of `extensions.jai.scripts`, as `janitor.script_manifest` writes it."""
    return {"id": script_id, "type": kind, "title": title, "is_public": False, "creator_name": author, "updated_at": "2026-06-01T00:00:00Z"}


def _add_listing_card(archive_dirs, filename: str, name: str, card_id: str, scripts: list[dict], book: dict | None = None) -> None:
    extensions = jai_extensions(card_id, gallery_id=card_id[:12], creator_name="worldsmith")
    extensions["jai"]["scripts"] = scripts
    fields = {"character_book": book} if book else {}
    (archive_dirs["characters"] / filename).write_bytes(card_png(name, extensions=extensions, **fields))
    from proxy.api.v1 import _shared

    _shared.index().refresh(force=True)


def test_listed_scripts_become_lorebooks_without_content(client, archive_dirs):
    # Gus embeds nothing; JanitorAI listed World (which other cards embed), a
    # private book nobody has, and a code script.
    _add_listing_card(
        archive_dirs,
        "Gus_99999999.png",
        "Gus",
        "99999999-0000",
        [_script("w", "World"), _script("secret", "Secret Codex"), _script("code", "Dice roller", kind="advanced")],
    )
    body = client.get("/api/v1/lorebooks").json()
    books = {book["name"]: book for book in body["lorebooks"]}
    assert set(books) == {"World", "People", "Secret Codex"}
    # Listing a book does not make a card carry it.
    assert (books["World"]["card_count"], books["World"]["listed_card_count"]) == (3, 1)
    assert books["World"]["has_content"] is True
    secret = books["Secret Codex"]
    assert (secret["has_content"], secret["entry_count"], secret["card_count"], secret["listed_card_count"]) == (False, 0, 0, 1)
    assert (secret["refs"], secret["authors"]) == (["jai:secret"], ["worldsmith"])
    assert body["stats"]["listed_only"] == 1

    rows = client.get("/api/v1/characters/Gus_99999999.png/lorebooks").json()
    assert [(r["name"], r["state"], r["entry_count"]) for r in rows] == [("World", "listed", 2), ("Secret Codex", "listed", 0)]
    detail = client.get(f"/api/v1/lorebooks/{secret['id']}").json()
    assert [c["name"] for c in detail["listed_cards"]] == ["Gus"]

    # Nothing to put on a card yet.
    refused = client.put(f"/api/v1/characters/Gus_99999999.png/lorebooks/{secret['id']}")
    assert refused.status_code == 422


def test_a_listed_id_merges_into_the_book_it_is_a_copy_of(client, archive_dirs):
    # Ivy embeds the public "Secret Codex" under one id; Gus lists a private
    # copy of it under another. Same title, same author: offered as a merge.
    codex = [_entry("Rune", "A rune."), _entry("Ward", "A ward.")]
    _add_listing_card(
        archive_dirs,
        "Ivy_77777777.png",
        "Ivy",
        "77777777-0000",
        [{**_script("codex-pub", "Secret Codex"), "is_public": True}],
        _jai_book("Ivy", ("codex-pub", "Secret Codex", codex)),
    )
    _add_listing_card(archive_dirs, "Gus_99999999.png", "Gus", "99999999-0000", [_script("secret", "Secret Codex")])
    books = {(b["name"], b["has_content"]): b for b in client.get("/api/v1/lorebooks").json()["lorebooks"]}
    public, private = books[("Secret Codex", True)], books[("Secret Codex", False)]
    detail = client.get(f"/api/v1/lorebooks/{private['id']}").json()
    assert [(s["id"], s["relation"]) for s in detail["similar"]] == [(public["id"], "title")]

    merged_id = client.post(f"/api/v1/lorebooks/{private['id']}/merge", json={"into": public["id"]}).json()["id"]
    merged = client.get(f"/api/v1/lorebooks/{merged_id}").json()
    assert merged["refs"] == ["jai:codex-pub", "jai:secret"]
    assert (merged["card_count"], merged["listed_card_count"], merged["entry_count"]) == (1, 1, 2)
    # Gus now lists a book with entries -- one a sync could put on it.
    rows = client.get("/api/v1/characters/Gus_99999999.png/lorebooks").json()
    assert [(r["lorebook_id"], r["state"], r["entry_count"]) for r in rows] == [(merged_id, "listed", 2)]
    assert client.put(f"/api/v1/characters/Gus_99999999.png/lorebooks/{merged_id}").status_code == 200


# --- decisions ---------------------------------------------------------------


def _town(client) -> dict:
    listed = client.get("/api/v1/lorebooks/entries", params={"unresolved": True}).json()["entries"]
    return client.get(f"/api/v1/lorebooks/entries/{listed[0]['id']}").json()


def test_choosing_a_version_resolves_the_entry_and_counts_pending_cards(client, archive_dirs):
    town = _town(client)
    old = town["versions"][1]
    assert town["chosen"] == ""

    assert client.put(f"/api/v1/lorebooks/entries/{town['id']}/chosen", json={"hash": old["hash"]}).status_code == 200
    after = client.get(f"/api/v1/lorebooks/entries/{town['id']}").json()
    assert after["chosen"] == old["hash"]
    world = _by_name(client)["World"]
    # Cleo holds the other version, so she is what a sync would rewrite.
    assert (world["unresolved_entries"], world["pending_cards"]) == (0, 1)
    assert client.get("/api/v1/lorebooks/entries", params={"unresolved": True}).json()["total"] == 0
    # The list now previews the chosen text, not the newest.
    row = client.get("/api/v1/lorebooks/entries", params={"changed": True}).json()["entries"][0]
    assert "market" not in row["preview"] and row["resolved"] and row["pending_cards"] == 1

    # Choosing again replaces the choice rather than adding a second.
    new = town["versions"][0]
    client.put(f"/api/v1/lorebooks/entries/{town['id']}/chosen", json={"hash": new["hash"]})
    assert _by_name(client)["World"]["pending_cards"] == 2

    assert client.delete(f"/api/v1/lorebooks/entries/{town['id']}/chosen").status_code == 200
    assert _by_name(client)["World"]["unresolved_entries"] == 1
    # No card was touched by any of it.
    assert archive_dirs["lorebook_decisions"].is_file()


def test_choosing_a_foreign_version_is_refused(client):
    town = _town(client)
    response = client.put(f"/api/v1/lorebooks/entries/{town['id']}/chosen", json={"hash": "not-a-version"})
    assert response.status_code == 422


def test_choose_newest_decides_only_the_undecided(client):
    world = _by_name(client)["World"]
    assert client.post(f"/api/v1/lorebooks/{world['id']}/choose-newest").json() == {"chosen": 1}
    town = client.get("/api/v1/lorebooks/entries", params={"changed": True}).json()["entries"][0]
    assert "market" in town["preview"] and town["pending_cards"] == 2
    assert client.post(f"/api/v1/lorebooks/{world['id']}/choose-newest").json() == {"chosen": 0}


def _add_retitled_world(archive_dirs) -> None:
    """An id-less copy of World where Town was retitled and reworded -- by title
    a different entry, and by content a different book."""
    entries = [_entry("The Town", "A small town, now with a market and a mill."), _entry("River", "A river.")]
    (archive_dirs["characters"] / "Gia_99999999.png").write_bytes(
        card_png(
            "Gia",
            extensions=jai_extensions("99999999-0000", gallery_id="999999990000"),
            envelope_extra={"create_date": "2026-06-01T00:00:00Z"},
            character_book={"name": "World (old export)", "entries": entries},
        )
    )
    from proxy.api.v1 import _shared

    _shared.index().refresh(force=True)


def test_merging_lorebooks_then_entries_and_undoing_both(client, archive_dirs):
    _add_retitled_world(archive_dirs)
    books = _by_name(client)
    world, stray = books["World"], books["World (old export)"]

    merged_id = client.post(f"/api/v1/lorebooks/{stray['id']}/merge", json={"into": world["id"]}).json()["id"]
    # The provider-identified book keeps its id and absorbs the stray's card.
    assert merged_id == world["id"]
    merged = client.get(f"/api/v1/lorebooks/{merged_id}").json()
    assert (merged["card_count"], merged["revision_count"]) == (4, 3)
    assert [(m["a_name"], m["b_name"]) for m in merged["merges"]] == [("World", "World (old export)")]
    assert "World (old export)" not in _by_name(client)

    # "The Town" arrived as its own entry; it is offered as a candidate for Town.
    entries = {e["title"]: e for e in merged["entries"]}
    assert set(entries) == {"Town", "The Town", "River"}
    town = client.get(f"/api/v1/lorebooks/entries/{entries['Town']['id']}").json()
    assert [c["title"] for c in town["candidates"]] == ["The Town"]
    # River is on every card Town is on, so it is never a candidate.

    line_id = client.post(
        f"/api/v1/lorebooks/entries/{entries['The Town']['id']}/merge", json={"into": entries["Town"]["id"]}
    ).json()["id"]
    line = client.get(f"/api/v1/lorebooks/entries/{line_id}").json()
    assert len(line["versions"]) == 3 and len(line["merges"]) == 1
    assert client.get(f"/api/v1/lorebooks/{merged_id}").json()["entry_count"] == 2

    assert client.delete(f"/api/v1/lorebooks/entries/merges/{line['merges'][0]['id']}").status_code == 204
    assert client.get(f"/api/v1/lorebooks/{merged_id}").json()["entry_count"] == 3
    assert client.delete(f"/api/v1/lorebooks/merges/{merged['merges'][0]['id']}").status_code == 204
    assert _by_name(client)["World"]["card_count"] == 3
    assert client.delete(f"/api/v1/lorebooks/merges/{merged['merges'][0]['id']}").status_code == 404


def test_merges_are_refused_across_lorebooks_and_with_self(client):
    books = _by_name(client)
    world = books["World"]
    assert client.post(f"/api/v1/lorebooks/{world['id']}/merge", json={"into": world["id"]}).status_code == 422
    assert client.post(f"/api/v1/lorebooks/{world['id']}/merge", json={"into": "nope"}).status_code == 404
    town = _town(client)
    mayor = client.get("/api/v1/lorebooks/entries", params={"lorebook": books["People"]["id"]}).json()["entries"][0]
    assert client.post(f"/api/v1/lorebooks/entries/{town['id']}/merge", json={"into": mayor["id"]}).status_code == 422


def test_a_damaged_decisions_file_is_reported_not_overwritten(client, archive_dirs):
    archive_dirs["lorebook_decisions"].write_text("{not json")
    assert client.get("/api/v1/lorebooks").status_code == 500
    assert archive_dirs["lorebook_decisions"].read_text() == "{not json"


# --- sync --------------------------------------------------------------------


def _book_on(archive_dirs, filename: str) -> dict | None:
    from proxy.cards import pngtools

    return pngtools.extract_embedded_card((archive_dirs["characters"] / filename).read_bytes()).get("character_book")


def _texts(book: dict | None) -> list[tuple[str, str]]:
    return [(e["comment"], e["content"]) for e in (book or {}).get("entries", [])]


def test_sync_writes_chosen_versions_and_keeps_the_original(client, archive_dirs):
    from proxy.cards import pngtools

    world = _by_name(client)["World"]
    client.post(f"/api/v1/lorebooks/{world['id']}/choose-newest")
    path = archive_dirs["characters"] / "Abbie_aaaaaaaa.png"
    pixels = pngtools.non_text_chunks(path.read_bytes())
    original = _book_on(archive_dirs, "Abbie_aaaaaaaa.png")

    plan = client.get("/api/v1/lorebooks/sync").json()["cards"]
    assert [(c["card"]["name"], c["upgrades"], c["attach"], c["detach"], c["has_original"]) for c in plan] == [
        ("Abbie", 1, [], [], False),
        ("Bea", 1, [], [], False),
    ]
    # Reading the plan wrote nothing.
    assert _book_on(archive_dirs, "Abbie_aaaaaaaa.png") == original

    result = client.post("/api/v1/lorebooks/sync", json={}).json()
    assert result == {
        "synced": 2,
        "upgraded": 2,
        "removed": 0,
        "attached": 0,
        "detached": 0,
        "originals_saved": 2,
        "failed": [],
    }

    book = _book_on(archive_dirs, "Abbie_aaaaaaaa.png")
    assert _texts(book) == [("Town", "A small town, now with a market."), ("River", "A river.")]
    # Only the embedded card changed, and the book says what it holds.
    assert pngtools.non_text_chunks(path.read_bytes()) == pixels
    assert book["extensions"]["jai_lorebooks"] == [
        {"refs": ["jai:w"], "title": "World", "kind": "janitor", "entry_count": 2, "prefix": ""}
    ]

    after = _by_name(client)["World"]
    assert (after["id"], after["card_count"], after["revision_count"], after["pending_cards"]) == (world["id"], 3, 1, 0)
    assert client.get("/api/v1/lorebooks/sync").json() == {"cards": []}
    # A second sync has nothing to do and saves nothing.
    assert client.post("/api/v1/lorebooks/sync", json={}).json()["synced"] == 0

    saved = client.get("/api/v1/characters/Abbie_aaaaaaaa.png/lorebook-original").json()
    assert (saved["exists"], saved["entry_count"], saved["differs"]) == (True, 2, True)
    restored = client.post("/api/v1/characters/Abbie_aaaaaaaa.png/lorebook-original/restore").json()
    assert (restored["exists"], restored["differs"]) == (True, False)
    assert _book_on(archive_dirs, "Abbie_aaaaaaaa.png") == original
    # The choice still stands, so the restored card is simply pending again --
    # and its original is not saved a second time.
    assert [c["has_original"] for c in client.get("/api/v1/lorebooks/sync").json()["cards"]] == [True]
    assert client.post("/api/v1/lorebooks/sync", json={}).json()["originals_saved"] == 0
    assert len(list(archive_dirs["lorebook_snapshots"].glob("*.json"))) == 2


def test_sync_can_be_scoped_to_a_lorebook_or_a_card(client):
    books = _by_name(client)
    client.post(f"/api/v1/lorebooks/{books['World']['id']}/choose-newest")
    assert client.get("/api/v1/lorebooks/sync", params={"lorebook": books["People"]["id"]}).json() == {"cards": []}
    assert client.post("/api/v1/lorebooks/sync", json={"lorebook": books["People"]["id"]}).json()["synced"] == 0
    assert client.post("/api/v1/lorebooks/sync", json={"cards": ["Bea_bbbbbbbb.png"]}).json()["synced"] == 1
    assert _by_name(client)["World"]["pending_cards"] == 1
    assert client.post("/api/v1/lorebooks/sync", json={"lorebook": "nope"}).status_code == 404
    assert client.get("/api/v1/characters/Eve_eeeeeeee.png/lorebook-original").json()["exists"] is False
    assert client.post("/api/v1/characters/Eve_eeeeeeee.png/lorebook-original/restore").status_code == 404


def test_a_merged_book_with_no_source_id_stays_merged_through_a_sync(client, archive_dirs):
    _add_retitled_world(archive_dirs)
    books = _by_name(client)
    merged_id = client.post(
        f"/api/v1/lorebooks/{books['World (old export)']['id']}/merge", json={"into": books["World"]["id"]}
    ).json()["id"]
    entries = {e["title"]: e for e in client.get(f"/api/v1/lorebooks/{merged_id}").json()["entries"]}
    line_id = client.post(
        f"/api/v1/lorebooks/entries/{entries['The Town']['id']}/merge", json={"into": entries["Town"]["id"]}
    ).json()["id"]
    line = client.get(f"/api/v1/lorebooks/entries/{line_id}").json()
    market = next(v for v in line["versions"] if v["content"] == "A small town, now with a market.")
    client.put(f"/api/v1/lorebooks/entries/{line_id}/chosen", json={"hash": market["hash"]})

    assert client.post("/api/v1/lorebooks/sync", json={}).json()["synced"] == 3
    # Gia's copy was identified by its content alone, and the sync changed its
    # content -- the retitled entry took the chosen version, title and all.
    gia = _book_on(archive_dirs, "Gia_99999999.png")
    assert _texts(gia) == [("Town", "A small town, now with a market."), ("River", "A river.")]
    assert [s["refs"][0][:3] for s in gia["extensions"]["jai_lorebooks"]] == ["fp:"]
    world = client.get(f"/api/v1/lorebooks/{merged_id}").json()
    assert (world["card_count"], world["revision_count"], world["entry_count"], world["pending_cards"]) == (4, 1, 2, 0)
    assert world["refs"] == ["jai:w"] and len(world["merges"]) == 1
    assert "World (old export)" not in _by_name(client)


def test_attach_and_detach_wait_for_the_sync(client, archive_dirs):
    books = _by_name(client)
    people, world = books["People"]["id"], books["World"]["id"]
    eve, cleo = "/api/v1/characters/Eve_eeeeeeee.png", "/api/v1/characters/Cleo_cccccccc.png"

    attaching = client.put(f"{eve}/lorebooks/{people}").json()
    assert [(b["name"], b["state"], b["entry_count"]) for b in attaching] == [("People", "attaching", 1)]
    detaching = client.delete(f"{cleo}/lorebooks/{people}").json()
    assert [(b["name"], b["state"]) for b in detaching] == [("World", "attached"), ("People", "detaching")]
    # Decisions only: no card has changed, and both show as work for a sync.
    assert _book_on(archive_dirs, "Eve_eeeeeeee.png") is None
    detail = client.get(f"/api/v1/lorebooks/{people}").json()
    assert ([c["name"] for c in detail["attaching"]], [c["name"] for c in detail["detaching"]]) == (["Eve"], ["Cleo"])
    assert (detail["card_count"], detail["pending_cards"]) == (2, 2)
    assert client.get("/api/v1/lorebooks").json()["stats"]["pending_cards"] == 2

    # Asking for what the card already is withdraws the decision.
    assert [b["state"] for b in client.put(f"{cleo}/lorebooks/{people}").json()] == ["attached", "attached"]
    assert client.get(f"/api/v1/lorebooks/{people}").json()["pending_cards"] == 1
    client.delete(f"{cleo}/lorebooks/{people}")
    # And attaching what is already attached records nothing.
    client.put(f"{cleo}/lorebooks/{world}")
    assert _by_name(client)["World"]["pending_cards"] == 0

    plan = client.get("/api/v1/lorebooks/sync").json()["cards"]
    assert [(c["card"]["name"], c["attach"], c["detach"]) for c in plan] == [
        ("Cleo", [], ["People"]),
        ("Eve", ["People"], []),
    ]
    result = client.post("/api/v1/lorebooks/sync", json={}).json()
    assert (result["synced"], result["attached"], result["detached"], result["originals_saved"]) == (2, 1, 1, 2)

    assert _texts(_book_on(archive_dirs, "Eve_eeeeeeee.png")) == [("Mayor", "The mayor.")]
    # Cleo keeps World exactly as it was, prefix included.
    assert _texts(_book_on(archive_dirs, "Cleo_cccccccc.png")) == [
        ("[World] Town", "A small town, now with a market."),
        ("[World] River", "A river."),
    ]
    assert [b["name"] for b in client.get(f"{cleo}/lorebooks").json()] == ["World"]
    after = client.get(f"/api/v1/lorebooks/{people}").json()
    assert sorted(c["name"] for r in after["revisions"] for c in r["cards"]) == ["Dana", "Eve"]
    assert (after["attaching"], after["detaching"], after["pending_cards"]) == ([], [], 0)

    # Eve had no lorebook, and that is what restoring gives back.
    assert client.get(f"{eve}/lorebook-original").json()["entry_count"] == 0
    client.post(f"{eve}/lorebook-original/restore")
    assert _book_on(archive_dirs, "Eve_eeeeeeee.png") is None
    assert client.get(f"{eve}/lorebooks").json() == []


def test_detaching_a_cards_only_lorebook_leaves_it_with_none(client, archive_dirs):
    people = _by_name(client)["People"]["id"]
    client.delete(f"/api/v1/characters/Dana_dddddddd.png/lorebooks/{people}")
    assert client.post("/api/v1/lorebooks/sync", json={}).json()["detached"] == 1
    assert _book_on(archive_dirs, "Dana_dddddddd.png") is None
    assert _by_name(client)["People"]["card_count"] == 1


def test_attach_refuses_unknown_cards_and_lorebooks(client):
    people = _by_name(client)["People"]["id"]
    assert client.put(f"/api/v1/characters/Nobody.png/lorebooks/{people}").status_code == 404
    assert client.put("/api/v1/characters/Eve_eeeeeeee.png/lorebooks/nope").status_code == 404


def test_sync_follows_the_cards_own_entry_when_the_same_text_sits_on_two_lines(client, archive_dirs):
    # Zoe's copy of World has the old Town text under another title: by title a
    # different entry, by content the very version Abbie and Bea are behind on.
    hamlet = [_entry("Zzz Hamlet", "A small town."), _entry("River", "A river.")]
    (archive_dirs["characters"] / "Zoe_22222222.png").write_bytes(
        card_png(
            "Zoe",
            extensions=jai_extensions("22222222-0000", gallery_id="222222220000"),
            envelope_extra={"create_date": "2026-01-15T00:00:00Z"},
            character_book=_jai_book("Zoe", ("w", "World", hamlet)),
        )
    )
    from proxy.api.v1 import _shared

    _shared.index().refresh(force=True)
    world = _by_name(client)["World"]
    client.post(f"/api/v1/lorebooks/{world['id']}/choose-newest")

    result = client.post("/api/v1/lorebooks/sync", json={}).json()
    assert (result["synced"], result["upgraded"], result["failed"]) == (2, 2, [])
    assert _texts(_book_on(archive_dirs, "Abbie_aaaaaaaa.png"))[0] == ("Town", "A small town, now with a market.")
    # Zoe's entry is its own line with one version, and is left alone.
    assert _texts(_book_on(archive_dirs, "Zoe_22222222.png"))[0] == ("Zzz Hamlet", "A small town.")
    assert _by_name(client)["World"]["pending_cards"] == 0


def test_a_card_holding_old_and_new_of_one_entry_keeps_only_the_chosen(client, archive_dirs):
    # Hal's copy had the new Town appended without the old one being removed.
    both = [*WORLD_V1, _entry("Town", "A small town, now with a market.")]
    (archive_dirs["characters"] / "Hal_33333333.png").write_bytes(
        card_png(
            "Hal",
            extensions=jai_extensions("33333333-0000", gallery_id="333333330000"),
            envelope_extra={"create_date": "2026-01-20T00:00:00Z"},
            character_book=_jai_book("Hal", ("w", "World", both)),
        )
    )
    from proxy.api.v1 import _shared

    _shared.index().refresh(force=True)
    client.post(f"/api/v1/lorebooks/{_by_name(client)['World']['id']}/choose-newest")

    plan = {c["card"]["name"]: (c["upgrades"], c["duplicates"]) for c in client.get("/api/v1/lorebooks/sync").json()["cards"]}
    assert plan == {"Abbie": (1, 0), "Bea": (1, 0), "Hal": (0, 1)}
    result = client.post("/api/v1/lorebooks/sync", json={}).json()
    assert (result["synced"], result["upgraded"], result["removed"]) == (3, 2, 1)
    assert _texts(_book_on(archive_dirs, "Hal_33333333.png")) == [
        ("River", "A river."),
        ("Town", "A small town, now with a market."),
    ]
