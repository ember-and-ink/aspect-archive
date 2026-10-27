"""`POST /api/v1/discover/preview` -- Discover's read-before-you-keep route.

The bar these tests hold is not "the route answers 200". It is **no drift**:
the preview must describe exactly the card `/build-chub` / `/build-datacat`
would write from the same captured payload. That is the whole reason the
mapping lives on the server instead of in the browser, so it is what gets
asserted -- field by field against the PNG the build route actually produces,
off the same real captures the build tests use.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from PIL import Image

from aspect.archive import catalog
from tests.api.test_acquisition import FakeResponder, make_client

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _chub_node(name: str) -> dict:
    return json.loads((FIXTURES / "chub" / f"{name}.json").read_text(encoding="utf-8"))


def _datacat_character(name: str) -> dict:
    raw = json.loads((FIXTURES / "datacat" / f"{name}.json").read_text(encoding="utf-8"))
    return raw["character"]


def _decode(path) -> dict:
    return json.loads(base64.b64decode(Image.open(Path(path)).text["ccv3"]))["data"]


# The fields a preview and a written card must agree on. `extensions` is left
# out on purpose: the build stamps `linkedAt` and `gallery_id`, which are facts
# about acquiring a card, and a preview has not acquired one.
_PROSE_FIELDS = (
    "name",
    "creator",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "creator_notes",
    "system_prompt",
    "post_history_instructions",
    "alternate_greetings",
    "tags",
)


def test_chub_preview_matches_the_card_build_chub_writes(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    node = _chub_node("raw_api_full_your_bully")
    linked = _chub_node("raw_linked_lorebook_your_bully")
    body = {"node": node, "linked_lorebook": linked}

    preview = client.post(
        "/api/v1/discover/preview", json={"provider": "chub", **body}
    )
    assert preview.status_code == 200
    seen = preview.json()

    built = client.post("/build-chub", json=body).json()
    written = _decode(built["path"])

    for field in _PROSE_FIELDS:
        assert seen["card"].get(field) == written.get(field), field
    # The lorebook is the field most likely to differ, since it is the one the
    # linked-project fetch can replace -- and the one the browser has never
    # actually sent (see the Get path).
    assert seen["card"]["character_book"] == written["character_book"]
    assert len(seen["card"]["character_book"]["entries"]) == 4


def test_chub_preview_counts_agree_with_the_archive(tmp_path):
    """The counts a preview shows are the counts the archive will show.

    Compared against `catalog.summarize` of the written PNG rather than against
    `GET /characters/{id}`: the index reads `settings.archive_dir`, not the
    tmp_path this test writes into, so the route would 404 on a card that very
    much exists. `summarize` is the code that route answers from either way.
    """
    client = make_client(FakeResponder(), tmp_path)
    node = _chub_node("raw_api_full_your_bully")
    linked = _chub_node("raw_linked_lorebook_your_bully")

    seen = client.post(
        "/api/v1/discover/preview",
        json={"provider": "chub", "node": node, "linked_lorebook": linked},
    ).json()

    built = client.post(
        "/build-chub", json={"node": node, "linked_lorebook": linked}
    ).json()
    archived = catalog.summarize(Path(built["path"]))

    assert seen["name"] == archived.name
    assert seen["creator"] == archived.creator
    assert seen["page_name"] == archived.page_name
    assert seen["card_id"] == archived.card_id
    assert seen["fragment"] == archived.fragment
    assert seen["greetings"] == archived.greeting_count
    assert seen["lore_entries"] == archived.lore_entry_count
    assert seen["description_chars"] == archived.description_chars
    assert seen["prompt_chars"] == archived.prompt_chars
    assert seen["has_creator_notes"] == archived.has_creator_notes
    assert seen["has_example_dialogue"] == archived.has_example_dialogue
    assert seen["source_url"] == archived.source_url
    assert seen["source_kind"] == archived.source_kind
    assert seen["tags"] == list(archived.tags)
    # The listing title and the character name genuinely differ on this card,
    # which is the case a preview most needs to get right.
    assert seen["name"] == "Autumn"
    assert seen["page_name"] != seen["name"]


def test_chub_preview_writes_nothing(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    node = _chub_node("raw_api_full_your_bully")

    client.post("/api/v1/discover/preview", json={"provider": "chub", "node": node})

    assert list(tmp_path.glob("*.png")) == []


def test_datacat_preview_matches_the_card_build_datacat_writes(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    character = _datacat_character("raw_api_character_abbie")

    seen = client.post(
        "/api/v1/discover/preview", json={"provider": "datacat", "character": character}
    ).json()
    written = _decode(
        client.post("/build-datacat", json={"character": character}).json()["path"]
    )

    for field in _PROSE_FIELDS:
        assert seen["card"].get(field) == written.get(field), field
    # DataCat puts the page title in `name` and the character's own name in
    # `chat_name`; the card takes the latter. Same expectation the build test
    # holds, asserted here so a preview cannot show the other one.
    assert seen["name"] == "Abbie"
    assert seen["page_name"] == "Offer You Can't Refuse | Abbie"


def test_datacat_preview_of_a_thin_row_is_an_empty_card_not_an_error(tmp_path):
    """A row with nothing in it previews; it does not fail.

    Deliberate: DataCat list payloads are summaries, and a card whose detail
    read came back thin should show as thin -- an empty preview is information
    ("there is nothing here to keep"), while an error would read as a fault in
    the archive. `build_v2_from_character` only declines an *absent* character,
    which the required-field check below covers.
    """
    client = make_client(FakeResponder(), tmp_path)

    resp = client.post(
        "/api/v1/discover/preview",
        json={"provider": "datacat", "character": {"character_id": "abc"}},
    )

    assert resp.status_code == 200
    seen = resp.json()
    assert seen["card_id"] == "abc"
    assert seen["greetings"] == 0
    assert seen["lore_entries"] == 0
    assert seen["description_chars"] == 0


def test_preview_requires_the_provider_s_payload(tmp_path):
    client = make_client(FakeResponder(), tmp_path)

    assert client.post("/api/v1/discover/preview", json={"provider": "chub"}).status_code == 422
    assert (
        client.post("/api/v1/discover/preview", json={"provider": "datacat"}).status_code == 422
    )
