"""`/api/v1` -- the archive's own contract.

These tests assert the contract's *shape*, not just its status codes: the
browser's adapter (`web/archive-api.js`) translates every field of it into the
shape the vendored frontend expects, so a silently renamed field here is a
broken browse grid there, with nothing in between to catch it.
"""

from __future__ import annotations

import os

from tests.conftest import card_png, jai_extensions


def _names(payload) -> list[str]:
    return [item["name"] for item in payload["items"]]


# --- list -------------------------------------------------------------------


def test_lists_every_card_sorted_by_name(client):
    payload = client.get("/api/v1/characters").json()
    assert payload["total"] == 3
    assert _names(payload) == ["Abbie", "Bella", "Cleo"]


def test_card_shape_is_the_contract(client):
    item = client.get("/api/v1/characters?q=abbie").json()["items"][0]
    assert item == {
        "id": "Abbie_0d162f5f.png",
        "name": "Abbie",
        "creator": "KornyPony",
        "page_name": "A Test Page | Test",
        "tags": ["Female", "Vampire"],
        "source_kind": "janitor_core",
        "source_url": "https://janitorai.com/characters/0d162f5f-86ab-4fdd-a2c2-3912adf24960",
        "card_id": "0d162f5f-86ab-4fdd-a2c2-3912adf24960",
        "fragment": "0d162f5f",
        "gallery_id": "kzbYR2QbpncC",
        "character_version": "1",
        "greetings": 1,
        "lore_entries": 2,
        "description_chars": len("Abbie is a test character."),
        # description + first_mes: the two prompt fields this fixture fills.
        "prompt_chars": len("Abbie is a test character.") + len("Hello, I am Abbie."),
        "has_creator_notes": False,
        "has_example_dialogue": False,
        "favorite": False,
        "is_fork": False,
        "size": item["size"],
        # The fixture's default image size, read off the PNG header.
        "avatar_width": 384,
        "avatar_height": 576,
        "modified": item["modified"],
        "linked_at": "2026-07-21T17:31:47.257Z",
        # Derived, not stored: this fixture carries no root `create_date`, so it
        # falls back to the earliest provider `linkedAt` -- here `jai`'s, since
        # it is the only provenance block the fixture has.
        "create_date": "2026-07-21T17:31:47.257Z",
        "thumb_url": "/api/v1/characters/Abbie_0d162f5f.png/thumb",
        "png_url": "/api/v1/characters/Abbie_0d162f5f.png/png",
        # Off unless asked for: see test_extensions_are_opt_in.
        "extensions": None,
        "error": None,
    }


def test_extensions_are_opt_in(client):
    """The identity block -- provider links, gallery_id, version uids -- is
    ~790 bytes a card, so it doubles a whole-archive listing and is not sent
    unless a client says it needs it."""
    plain = client.get("/api/v1/characters?q=abbie").json()["items"][0]
    assert plain["extensions"] is None

    rich = client.get("/api/v1/characters?q=abbie&include=extensions").json()["items"][0]
    assert rich["extensions"]["gallery_id"] == "kzbYR2QbpncC"
    assert rich["extensions"]["jai"]["id"] == "0d162f5f-86ab-4fdd-a2c2-3912adf24960"


def test_sorting_by_added_uses_acquisition_time_not_the_files_mtime(client, populated_archive):
    """"Recently added" has to mean when the card arrived.

    mtime is the obvious-looking source and the wrong one: the bulk repair
    passes rewrote 84% of the real archive within a single day, which collapses
    that ordering into alphabetical. `extensions.jai.linkedAt` is stamped at
    acquisition and every card in the archive carries one.
    """
    extensions = jai_extensions("99990000-0000-0000-0000-000000000000", creator_name="Late")
    extensions["jai"]["linkedAt"] = "2026-08-10T09:00:00.000Z"  # after the fixtures' shared date
    (populated_archive["characters"] / "Zoe_99990000.png").write_bytes(
        card_png("Zoe", extensions=extensions)
    )
    # Every *other* card looks freshly written, so mtime and linkedAt disagree.
    # Stamped rather than touched: Linux takes mtimes from the coarse clock, so
    # four writes in a row can share one timestamp, and the tie-break (filename,
    # reversed along with the sort) would hand `-modified` to Zoe anyway.
    zoe_mtime = (populated_archive["characters"] / "Zoe_99990000.png").stat().st_mtime
    for offset, name in enumerate(
        ("Abbie_0d162f5f.png", "Bella_11112222.png", "Cleo_33334444.png"), start=1
    ):
        path = populated_archive["characters"] / name
        os.utime(path, (zoe_mtime + offset, zoe_mtime + offset))
    client.post("/api/v1/refresh")

    assert _names(client.get("/api/v1/characters?sort=-added").json())[0] == "Zoe"
    assert _names(client.get("/api/v1/characters?sort=-modified").json())[0] != "Zoe"


def test_prompt_chars_sums_the_prompt_fields_not_just_the_description(client):
    """Sorting a grid by card weight has to count every field that reaches the
    model, not the description alone."""
    item = client.get("/api/v1/characters?q=cleo").json()["items"][0]
    assert item["prompt_chars"] >= item["description_chars"]


def test_urls_on_the_card_are_usable_as_given(client):
    """The client must never have to build a path by encoding an id itself."""
    item = client.get("/api/v1/characters?q=abbie").json()["items"][0]
    assert client.get(item["png_url"]).status_code == 200
    assert client.get(item["thumb_url"]).status_code == 200


def test_search_ands_its_terms(client):
    """A second word narrows. "korny abbie" is the one card, not every card by
    either."""
    assert _names(client.get("/api/v1/characters?q=korny+abbie").json()) == ["Abbie"]
    assert _names(client.get("/api/v1/characters?q=korny").json()) == ["Abbie", "Cleo"]
    assert client.get("/api/v1/characters?q=korny+nonexistent").json()["total"] == 0


def test_search_is_case_insensitive_and_covers_the_page_title(client):
    assert _names(client.get("/api/v1/characters?q=BELLA+THE+SECOND").json()) == ["Bella"]


def test_search_does_not_reach_into_prose(client):
    """Descriptions are not indexed -- 40 MB resident for a feature nobody asked
    for. Documented behaviour, so it is asserted."""
    assert client.get("/api/v1/characters?q=test+character").json()["total"] == 0


def test_search_can_be_scoped_to_one_field(client):
    """The ⌘K scope chips. A narrower scope only ever narrows: `all` is the
    union, so nothing matches in a scope that would not have matched without
    one."""
    # "korny" is a creator, not a name -- so it survives the creator scope and
    # not the name scope.
    assert _names(client.get("/api/v1/characters?q=korny").json()) == ["Abbie", "Cleo"]
    assert _names(client.get("/api/v1/characters?q=korny&scope=creator").json()) == ["Abbie", "Cleo"]
    assert client.get("/api/v1/characters?q=korny&scope=name").json()["total"] == 0
    assert _names(client.get("/api/v1/characters?q=abbie&scope=name").json()) == ["Abbie"]
    assert _names(client.get("/api/v1/characters?q=vampire&scope=tags").json()) == ["Abbie"]


def test_scope_does_not_reach_the_page_title_or_filename(client):
    """Both are in the default haystack and in none of the narrow scopes --
    which is the point of scoping: "bella" the page title should not answer a
    search of creator names."""
    assert _names(client.get("/api/v1/characters?q=second").json()) == ["Bella"]
    assert client.get("/api/v1/characters?q=second&scope=name").json()["total"] == 0
    assert client.get("/api/v1/characters?q=second&scope=creator").json()["total"] == 0


def test_an_unknown_scope_is_rejected(client):
    assert client.get("/api/v1/characters?q=abbie&scope=description").status_code == 422


def test_tag_filter_requires_every_tag(client):
    assert _names(client.get("/api/v1/characters?tag=Female").json()) == ["Abbie", "Cleo"]
    assert _names(client.get("/api/v1/characters?tag=Female&tag=Vampire").json()) == ["Abbie"]
    assert client.get("/api/v1/characters?tag=Female&tag=Male").json()["total"] == 0


def test_tag_filter_is_case_insensitive(client):
    assert _names(client.get("/api/v1/characters?tag=female").json()) == ["Abbie", "Cleo"]


def test_creator_and_source_filters(client):
    assert _names(client.get("/api/v1/characters?creator=kornypony").json()) == ["Abbie", "Cleo"]
    assert _names(client.get("/api/v1/characters?source=chub_import").json()) == ["Bella"]


def test_repeated_source_is_ored(client):
    """One platform can span two importer kinds -- `chub_import` for the bulk
    pass, `chub_core` for a live capture -- so the UI's per-platform Source
    filter sends both and expects the union, not the intersection."""
    both = "/api/v1/characters?source=chub_import&source=janitor_core"
    assert _names(client.get(both).json()) == ["Abbie", "Bella", "Cleo"]
    assert client.get("/api/v1/characters?source=nothing_at_all").json()["total"] == 0


def test_has_lorebook_filter(client):
    assert _names(client.get("/api/v1/characters?has_lorebook=true").json()) == ["Abbie"]
    assert _names(client.get("/api/v1/characters?has_lorebook=false").json()) == ["Bella", "Cleo"]


def test_favorite_filter_reads_the_card_not_a_sidecar(client, populated_archive):
    """The star lives in `extensions.fav` on the PNG, so a card starred by
    anything -- this API, the old UI, SillyTavern -- filters here with no
    migration in between."""
    extensions = jai_extensions("55556666-0000-0000-0000-000000000000")
    extensions["fav"] = True
    (populated_archive["characters"] / "Dora_55556666.png").write_bytes(
        card_png("Dora", extensions=extensions)
    )
    client.post("/api/v1/refresh")

    assert _names(client.get("/api/v1/characters?favorite=true").json()) == ["Dora"]
    assert "Dora" not in _names(client.get("/api/v1/characters?favorite=false").json())
    # Unfiltered still means everything, starred or not.
    assert client.get("/api/v1/characters").json()["total"] == 4


def test_favorite_is_read_from_the_envelope_root_too(client, populated_archive):
    """SillyTavern mirrors `fav` outside `data`, and writes it as the string
    `"true"` as often as the boolean. Reading is tolerant of both; writing is
    not, because only the extensions copy survives a re-embed."""
    (populated_archive["characters"] / "Elle_77778888.png").write_bytes(
        card_png(
            "Elle",
            extensions=jai_extensions("77778888-0000-0000-0000-000000000000"),
            envelope_extra={"fav": "true"},
        )
    )
    client.post("/api/v1/refresh")

    assert _names(client.get("/api/v1/characters?favorite=true").json()) == ["Elle"]


def test_exclude_tag_removes_cards_and_beats_include(client):
    assert _names(client.get("/api/v1/characters?exclude_tag=Female").json()) == ["Bella"]
    assert _names(client.get("/api/v1/characters?tag=Female&exclude_tag=Vampire").json()) == ["Cleo"]
    # The same tag both ways is a contradiction, and "not this" is the stronger
    # half of it -- a hand-built URL gets an empty set, not an arbitrary winner.
    assert client.get("/api/v1/characters?tag=Female&exclude_tag=female").json()["total"] == 0


def test_untagged_filter_finds_the_tagging_backlog(client, populated_archive):
    (populated_archive["characters"] / "Fern_99998888.png").write_bytes(
        card_png("Fern", extensions=jai_extensions("99998888-0000-0000-0000-000000000000"))
    )
    client.post("/api/v1/refresh")

    assert _names(client.get("/api/v1/characters?untagged=true").json()) == ["Fern"]
    assert _names(client.get("/api/v1/characters?untagged=false").json()) == ["Abbie", "Bella", "Cleo"]


def test_min_greetings_counts_the_primary_greeting(client):
    """Bella has two alternates plus her `first_mes`, so she is the only card
    the mock's multi-greeting chip should surface."""
    assert _names(client.get("/api/v1/characters?min_greetings=2").json()) == ["Bella"]
    assert client.get("/api/v1/characters?min_greetings=1").json()["total"] == 3
    assert client.get("/api/v1/characters?min_greetings=4").json()["total"] == 0


def test_added_after_compares_against_acquisition_time(client, populated_archive):
    """Same field `sort=added` orders by, compared the same way -- ISO-8601
    sorts lexically, so the chip and the sort cannot disagree."""
    extensions = jai_extensions("12341234-0000-0000-0000-000000000000")
    extensions["jai"]["linkedAt"] = "2026-08-10T09:00:00.000Z"
    (populated_archive["characters"] / "Gwen_12341234.png").write_bytes(
        card_png("Gwen", extensions=extensions)
    )
    client.post("/api/v1/refresh")

    assert _names(client.get("/api/v1/characters?added_after=2026-08-01").json()) == ["Gwen"]
    assert client.get("/api/v1/characters?added_after=2026-01-01").json()["total"] == 4


def test_added_after_excludes_cards_with_no_acquisition_stamp(client, populated_archive):
    """An undated card is not recent. Treating a missing stamp as "now" would
    put every hand-dropped card at the top of the Added This Week chip."""
    (populated_archive["characters"] / "Hana_44445555.png").write_bytes(card_png("Hana"))
    client.post("/api/v1/refresh")

    assert "Hana" not in _names(client.get("/api/v1/characters?added_after=2000-01-01").json())


def test_sort_and_reverse(client):
    assert _names(client.get("/api/v1/characters?sort=-name").json()) == ["Cleo", "Bella", "Abbie"]
    assert _names(client.get("/api/v1/characters?sort=-greetings").json())[0] == "Bella"
    assert _names(client.get("/api/v1/characters?sort=-lore").json())[0] == "Abbie"


def test_unknown_sort_is_a_422_that_says_what_is_allowed(client):
    response = client.get("/api/v1/characters?sort=drop+table")
    assert response.status_code == 422
    assert "greetings" in response.json()["detail"]


def test_pagination_reports_the_filtered_total(client):
    payload = client.get("/api/v1/characters?limit=2&offset=1").json()
    assert payload["total"] == 3, "total is the filtered set, not the page"
    assert _names(payload) == ["Bella", "Cleo"]
    assert (payload["limit"], payload["offset"]) == (2, 1)


def test_limit_zero_means_everything(client):
    payload = client.get("/api/v1/characters?limit=0").json()
    assert len(payload["items"]) == 3


def test_offset_past_the_end_is_an_empty_page_not_an_error(client):
    payload = client.get("/api/v1/characters?offset=999").json()
    assert payload["items"] == []
    assert payload["total"] == 3


def test_health_filter_surfaces_broken_cards(client, populated_archive):
    (populated_archive["characters"] / "Broken_1.png").write_bytes(b"not a png")
    client.post("/api/v1/refresh")

    assert client.get("/api/v1/characters").json()["total"] == 3, "broken cards are out by default"

    broken = client.get("/api/v1/characters?health=broken").json()
    assert broken["total"] == 1
    item = broken["items"][0]
    assert item["id"] == "Broken_1.png"
    assert item["error"] == "not a PNG stream"
    assert item["name"] == ""

    assert client.get("/api/v1/characters?health=all").json()["total"] == 4


# --- facets, stats, refresh -------------------------------------------------


def test_facets_count_over_the_whole_archive(client):
    facets = client.get("/api/v1/facets").json()
    assert facets["tags"] == [
        {"value": "Female", "count": 2},
        {"value": "Male", "count": 1},
        {"value": "Vampire", "count": 1},
    ]
    assert facets["creators"][0] == {"value": "KornyPony", "count": 2}
    assert {f["value"] for f in facets["sources"]} == {"janitor_core", "chub_import"}


def test_facets_can_be_capped(client):
    facets = client.get("/api/v1/facets?limit=1").json()
    assert facets["tags"] == [{"value": "Female", "count": 2}]


def test_stats(client):
    stats = client.get("/api/v1/stats").json()
    assert stats["cards"] == 3
    assert stats["unreadable"] == 0
    assert stats["creators"] == 2
    assert stats["tags"] == 3
    assert stats["galleries"] == 1, "only Abbie's gallery folder exists on disk"
    assert stats["bytes"] > 0
    assert stats["thumbs"] == {"cached": 0, "missing": 3, "stale": 0}
    assert stats["media"] == {
        "images": 2,
        "bytes": 306,
        "by_ext": [{"ext": "jpg", "count": 2, "bytes": 306}],
    }, "Abbie's gallery holds one.jpg (103 bytes incl. JPEG header) and two.jpg (203 bytes)"


def test_count_matches_stats(client):
    assert client.get("/api/v1/count").json()["cards"] == client.get("/api/v1/stats").json()["cards"]


def test_stats_tracks_thumb_coverage_as_it_is_generated(client):
    client.get("/api/v1/characters/Abbie_0d162f5f.png/thumb")
    assert client.get("/api/v1/stats").json()["thumbs"]["cached"] == 1


def test_an_outside_write_appears_only_after_a_rescan(client, populated_archive):
    """Reads never rescan; `POST /refresh` (the Rescan button) is the only door
    for a file that arrived behind the server's back."""
    (populated_archive["characters"] / "Dana_55556666.png").write_bytes(card_png("Dana"))
    assert "Dana" not in _names(client.get("/api/v1/characters").json())
    stats = client.post("/api/v1/refresh").json()
    assert stats["parsed"] == 1
    assert "Dana" in _names(client.get("/api/v1/characters").json())


# --- detail -----------------------------------------------------------------


def test_detail_returns_the_whole_embedded_card(client):
    payload = client.get("/api/v1/characters/Abbie_0d162f5f.png").json()
    assert payload["spec"] == "chara_card_v3"
    assert payload["spec_version"] == "3.0"
    assert payload["name"] == "Abbie"
    # Verbatim, straight off the PNG -- prose the list omits, and every extension
    # block, unrewritten.
    assert payload["card"]["description"] == "Abbie is a test character."
    assert payload["card"]["first_mes"] == "Hello, I am Abbie."
    assert payload["card"]["extensions"]["jai"]["sourceKind"] == "janitor_core"
    assert len(payload["card"]["character_book"]["entries"]) == 2


def test_detail_measures_the_gallery_on_disk(client):
    payload = client.get("/api/v1/characters/Abbie_0d162f5f.png").json()
    assert payload["gallery"] == {
        "gallery_id": "kzbYR2QbpncC",
        "folder": "Abbie_kzbYR2QbpncC",
        "exists": True,
        "images": 2,
        "bytes": 306,
    }


def test_gallery_folder_missing_is_reported_not_invented(client):
    """The common real case: a card carries a gallery_id but its images were never
    downloaded. `exists: false` with the folder name still filled in."""
    payload = client.get("/api/v1/characters/Cleo_33334444.png").json()
    assert payload["gallery"]["folder"] == "Cleo_CCCCCCCCCCCC"
    assert payload["gallery"]["exists"] is False
    assert payload["gallery"]["images"] == 0


def test_detail_of_a_broken_card_is_a_422_with_a_reason(client, populated_archive):
    (populated_archive["characters"] / "Broken_1.png").write_bytes(b"not a png")
    client.post("/api/v1/refresh")
    response = client.get("/api/v1/characters/Broken_1.png")
    assert response.status_code == 422
    assert "not a PNG" in response.json()["detail"]


def test_unknown_card_is_a_404_naming_it(client):
    response = client.get("/api/v1/characters/Nope.png")
    assert response.status_code == 404
    assert "Nope.png" in response.json()["detail"]


def test_traversal_cannot_name_a_file(client):
    """The path is resolved through the index, so `..` matches no card."""
    for attempt in ("..%2F..%2Fetc%2Fpasswd", "....%2F%2Fetc%2Fpasswd", "%2Fetc%2Fpasswd"):
        assert client.get(f"/api/v1/characters/{attempt}").status_code == 404


# --- bytes out --------------------------------------------------------------


def test_png_is_the_card_byte_for_byte(client, populated_archive):
    """No re-encoding anywhere in the path -- re-encoding is what strips the V3
    tEXt chunks, which is the whole reason the export works at all."""
    on_disk = (populated_archive["characters"] / "Abbie_0d162f5f.png").read_bytes()
    response = client.get("/api/v1/characters/Abbie_0d162f5f.png/png")
    assert response.status_code == 200
    assert response.content == on_disk
    assert response.headers["content-type"] == "image/png"


def test_png_is_offered_as_a_download_under_its_own_name(client):
    disposition = client.get("/api/v1/characters/Abbie_0d162f5f.png/png").headers[
        "content-disposition"
    ]
    assert disposition.startswith("attachment;")
    assert 'filename="Abbie_0d162f5f.png"' in disposition
    assert "filename*=UTF-8''Abbie_0d162f5f.png" in disposition


def test_download_name_survives_non_ascii(client, populated_archive):
    (populated_archive["characters"] / "Amélie_1.png").write_bytes(card_png("Amélie"))
    client.post("/api/v1/refresh")
    disposition = client.get("/api/v1/characters/Amélie_1.png/png").headers["content-disposition"]
    # An ASCII fallback for old clients, and the real name in the RFC 6266 form.
    assert 'filename="Am?lie_1.png"' in disposition
    assert "filename*=UTF-8''Am%C3%A9lie_1.png" in disposition


def test_thumb_is_generated_and_served_as_webp(client, populated_archive):
    response = client.get("/api/v1/characters/Abbie_0d162f5f.png/thumb")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == "public, max-age=86400"
    card_bytes = (populated_archive["characters"] / "Abbie_0d162f5f.png").stat().st_size
    assert len(response.content) < card_bytes


def test_thumb_falls_back_to_the_full_png_when_it_cannot_be_rendered(client, populated_archive):
    """A card too broken to thumbnail still has to appear in the grid -- being
    visible is how it gets noticed and fixed."""
    (populated_archive["characters"] / "Broken_1.png").write_bytes(b"not a png")
    client.post("/api/v1/refresh")
    response = client.get("/api/v1/characters/Broken_1.png/thumb")
    assert response.status_code == 200
    assert response.content == b"not a png"


def test_conditional_get_answers_304_with_no_body(client):
    """A grid re-asks for hundreds of thumbs on every navigation."""
    first = client.get("/api/v1/characters/Abbie_0d162f5f.png/thumb")
    etag = first.headers["etag"]
    again = client.get(
        "/api/v1/characters/Abbie_0d162f5f.png/thumb", headers={"If-None-Match": etag}
    )
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag


def test_etag_changes_when_the_card_changes(client, populated_archive):
    """Invalidation keys on (mtime_ns, size) -- the same pair the index uses, so a
    card and its thumb can never disagree about whether they changed."""
    before = client.get("/api/v1/characters/Cleo_33334444.png/png").headers["etag"]
    (populated_archive["characters"] / "Cleo_33334444.png").write_bytes(
        card_png("Cleo Rewritten Much Longer Name", extensions=jai_extensions("33334444"))
    )
    after = client.get("/api/v1/characters/Cleo_33334444.png/png").headers["etag"]
    assert before != after


def test_a_card_deleted_out_from_under_the_index_is_a_404(client, populated_archive):
    (populated_archive["characters"] / "Abbie_0d162f5f.png").unlink()
    client.post("/api/v1/refresh")
    assert client.get("/api/v1/characters/Abbie_0d162f5f.png/png").status_code == 404


def test_the_old_endpoints_still_work(client):
    """`/api/v1` is additive: the capture-and-build protocol the userscripts speak
    is untouched by it."""
    assert client.get("/health").json()["ok"] is True
