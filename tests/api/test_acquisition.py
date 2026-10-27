import base64
import io
import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

import aspect.server as server_module
from aspect import deps
from aspect.cards.builder import PngWriter
from aspect.runtime import dashboard as dashboard_mod
from aspect.state.captures import CaptureStore
from aspect.state.lorebook_cache import LorebookCache
from aspect.config import settings
from aspect.runtime.dashboard import Dashboard

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _character(name: str) -> dict:
    return json.loads((FIXTURES / "hampter" / f"{name}.json").read_text(encoding="utf-8"))


def _saucepan(id_fragment: str) -> dict:
    path = next((FIXTURES / "saucepan").glob(f"saucepan_{id_fragment}*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def _prompt(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponder:
    model = settings.mock_model

    def __init__(self):
        self.last_request = None

    async def complete(self, req):
        self.last_request = req
        return {
            "id": "chatcmpl-fake",
            "model": settings.mock_model,
            "choices": [
                {
                    "message": {"role": "assistant", "content": "fake reply"},
                    "finish_reason": "stop",
                }
            ],
        }

    async def stream(self, req):
        self.last_request = req
        yield b'data: {"choices":[{"delta":{"content":"fake reply"}}]}\n\n'
        yield b"data: [DONE]\n\n"


class FakeAvatarFetcher:
    def __init__(self, png_bytes: bytes | None = None):
        buf = io.BytesIO()
        Image.new("RGBA", (8, 8), (9, 9, 9, 255)).save(buf, "PNG")
        self._bytes = png_bytes or buf.getvalue()

    async def fetch(self, url, avatar_b64=None):
        return self._bytes


def make_client(fake: FakeResponder, tmp_path=None) -> TestClient:
    deps.responder = fake
    deps.capture_store = CaptureStore(captures_dir=tmp_path)
    deps.png_writer = PngWriter(output_dir=tmp_path)
    deps.avatar_fetcher = FakeAvatarFetcher()
    deps.lorebook_cache = LorebookCache(
        cache_dir=(tmp_path / ".lorecache") if tmp_path else None
    )
    return TestClient(server_module.app)


def _decode(path) -> dict:
    return json.loads(base64.b64decode(Image.open(Path(path)).text["ccv3"]))["data"]


def test_health(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["captures"] == 0
    assert body["model"] == settings.mock_model


def test_list_models(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.json() == {
        "object": "list",
        "data": [{"id": settings.mock_model, "object": "model"}],
    }


# ---------------------------------------------------------------------------
# /v1/chat/completions -- answers from the mock responder and captures the
# hidden definition (system message) + primary greeting (first assistant).
# ---------------------------------------------------------------------------


def test_chat_completions_captures_system_prompt_and_forwards(tmp_path):
    fake = FakeResponder()
    client = make_client(fake, tmp_path)

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "whatever-janitorai-sends",
            "stream": False,
            "messages": [
                {"role": "system", "content": "<system>hidden def here</system>"},
                {"role": "user", "content": "hi"},
            ],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "fake reply"
    assert fake.last_request["messages"][0]["content"] == "<system>hidden def here</system>"

    assert client.get("/health").json()["captures"] == 1

    written = list(tmp_path.glob("system_prompt_*.txt"))
    assert len(written) == 1
    assert written[0].read_text() == "<system>hidden def here</system>"


def test_chat_completions_captures_assistant_message_as_primary_greeting(tmp_path):
    # Real captured JanitorAI chat request: [system(hidden def), user ".",
    # assistant(rendered primary greeting), user "USER: hello"]. One relay
    # captures both halves a hidden card needs.
    client = make_client(FakeResponder(), tmp_path)
    real_request = json.loads((FIXTURES / "chat_request_hidden_ari.json").read_text(encoding="utf-8"))

    client.post("/v1/chat/completions", json=real_request)

    status = client.get("/capture-status", params={"name": "Ari"}).json()
    assert status == {"name": "Ari", "system": True, "greetings": True}

    # Stored as the assistant message (index 2), stripped.
    record = deps.capture_store.get("Ari")
    assert record.greetings == [real_request["messages"][2]["content"].strip()]


def test_chat_completions_streaming_returns_sse(tmp_path):
    client = make_client(FakeResponder(), tmp_path)

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in resp.text


def test_chat_completions_capture_error_does_not_block_forward(monkeypatch, tmp_path):
    fake = FakeResponder()
    client = make_client(fake, tmp_path)
    monkeypatch.setattr(
        deps.capture_store,
        "record",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "stream": False,
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "fake reply"


# ---------------------------------------------------------------------------
# /build-jai -- open card export end-to-end (JSON API path).
# ---------------------------------------------------------------------------


def test_build_exports_open_card_png(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    akane = _character("open_akane_kujo")

    resp = client.post(
        "/build-jai",
        json={
            "character": {
                "name": "Akane Kujo",
                "id": "abc123",
                "url": "https://janitorai.com/characters/abc123",
            },
            "character_json": akane,
            "avatar_url": "https://ella.janitorai.com/bot-avatars/example.webp",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["fields_present"]["description"] is True
    assert body["fields_present"]["scenario"] is True
    assert body["fields_present"]["first_mes"] is True
    assert body["fields_present"]["alternate_greetings"] is True

    path = Path(body["path"])
    # Flat in the cards folder, name suffixed with the card-id fragment.
    assert path.exists()
    assert path.parent == tmp_path
    assert path.name == "Akane_Kujo_abc123.png"

    data = _decode(path)
    assert data["name"] == "Akane Kujo"
    assert data["creator"] == "dezea"
    assert data["tags"] == [
        "Female", "Multiple", "AnyPOV", "Angst", "Demi-Human",
        "Fluff", "Horror", "kitsune", "yandere", "TheValentine",
    ]
    assert data["first_mes"].startswith("**Scenario: Welcome to Kamii University!**")
    assert data["character_version"] == "https://janitorai.com/characters/abc123"

    jai = data["extensions"]["jai"]
    assert jai["id"] == "abc123"
    assert jai["source_url"] == "https://janitorai.com/characters/abc123"
    assert jai["sourceKind"] == "janitor_core"
    assert jai["creatorName"] == "dezea"
    # The JSON `name` field (the card-title blurb) is kept as metadata, not
    # embedded as data.name.
    assert jai["pageName"] == "The Girl in Every Yearbook | Akane Kujo"
    assert "linkedAt" in jai

    # extensions.datacat rides alongside jai -- same provenance, datacat's own
    # shape (sourceKind "janitor", creatorId from the JSON's creator_id) --
    # so the card is CharacterLibrary/datacat-linkable straight off the wire.
    datacat = data["extensions"]["datacat"]
    assert datacat["id"] == "abc123"
    assert datacat["sourceKind"] == "janitor"
    assert datacat["creatorId"] == "866c0877-ea3d-4bc6-a906-13c5d9601f9d"
    assert datacat["creatorName"] == "dezea"
    assert datacat["pageName"] == jai["pageName"]
    assert datacat["linkedAt"] == jai["linkedAt"]

    # A served card is CharacterLibrary-ready: it leaves with its own gallery id.
    gallery_id = data["extensions"]["gallery_id"]
    assert len(gallery_id) == 12 and gallery_id.isalnum()


def test_rebuilding_a_saved_card_skips_the_write(tmp_path, caplog):
    client = make_client(FakeResponder(), tmp_path)
    payload = {
        "character": {"name": "Akane Kujo", "id": "abc123"},
        "character_json": _character("open_akane_kujo"),
        "avatar_url": "https://ella.janitorai.com/bot-avatars/example.webp",
    }

    dashboard = Dashboard(title="aspect-archive", address="http://x")
    dashboard_mod.DASHBOARD = dashboard
    try:
        with caplog.at_level(logging.INFO, logger="aspect.api.build"):
            first = client.post("/build-jai", json=payload).json()
            path = Path(first["path"])
            stamped = path.read_bytes()
            second = client.post("/build-jai", json=payload).json()
    finally:
        dashboard_mod.DASHBOARD = None

    assert first["duplicate"] is False
    # Skipped, not overwritten: same path reported back, file untouched. Only by
    # deleting it does a re-export happen.
    assert second == {**first, "duplicate": True, "warnings": [], "fields_present": {}}
    assert path.read_bytes() == stamped

    saved, dup = list(dashboard.feed.rows)
    assert (saved["duplicate"], dup["duplicate"]) == (False, True)
    assert dup["filename"] == "Akane_Kujo_abc123.png"
    assert (dashboard.feed.succeeded, dashboard.feed.duplicates) == (1, 1)

    # The plain-log path (no TTY) says the same thing, and names the file so a
    # card can be found on disk straight from the log line.
    lines = [r.getMessage() for r in caplog.records if "card:" in r.getMessage()]
    assert lines == [
        "saved janitor card: Akane Kujo by dezea (Akane_Kujo_abc123.png)",
        "already have janitor card: Akane Kujo by dezea (Akane_Kujo_abc123.png)",
    ]


def test_rebuilding_after_deleting_the_card_writes_it_again(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    payload = {
        "character": {"name": "Akane Kujo", "id": "abc123"},
        "character_json": _character("open_akane_kujo"),
    }
    path = Path(client.post("/build-jai", json=payload).json()["path"])
    path.unlink()

    again = client.post("/build-jai", json=payload).json()
    assert again["duplicate"] is False
    assert Path(again["path"]).exists()


# ---------------------------------------------------------------------------
# /build-saucepan -- open card export end-to-end (saucepan JSON API path). Same
# assemble/write tail as /build-jai; differs only in the source mapper.
# ---------------------------------------------------------------------------


def test_build_saucepan_exports_open_card_png(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    eve = _saucepan("04a0c1ac")

    resp = client.post("/build-saucepan", json={"character": eve})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["fields_present"]["description"] is True
    assert body["fields_present"]["scenario"] is True
    assert body["fields_present"]["first_mes"] is True
    assert body["fields_present"]["alternate_greetings"] is True
    assert body["fields_present"]["character_book"] is True

    path = Path(body["path"])
    # Flat in the cards folder, name suffixed with the companion-id fragment.
    assert path.parent == tmp_path
    assert path.name == "Eve_04a0c1ac.png"

    data = _decode(path)
    assert data["name"] == "Eve"
    assert data["creator"] == "desslok"
    assert data["first_mes"].startswith("Throughout her first week at Crestfall High")
    assert len(data["alternate_greetings"]) == 3  # 5 scenarios, one blank dropped
    # Advanced Prompt leads scenario, raw.
    assert data["scenario"].startswith("{{char}} is an android")
    assert len(data["character_book"]["entries"]) == 19  # two lorebooks merged
    assert (
        data["character_version"]
        == "https://saucepan.ai/companion/04a0c1ac-187b-4aa0-8f5b-885533be748d"
    )

    jai = data["extensions"]["jai"]
    assert jai["sourceKind"] == "saucepan_core"
    assert jai["id"] == "04a0c1ac-187b-4aa0-8f5b-885533be748d"
    assert jai["source_url"] == "https://saucepan.ai/companion/04a0c1ac-187b-4aa0-8f5b-885533be748d"
    assert jai["creatorName"] == "desslok"
    assert jai["pageName"] == "Eve | I Did Nothing Wrong"

    datacat = data["extensions"]["datacat"]
    assert datacat["id"] == jai["id"]
    assert datacat["sourceKind"] == "saucepan"
    assert datacat["creatorId"] == "cba8693b-3a04-42fe-883d-27df186ca711"
    assert datacat["creatorName"] == "desslok"
    assert datacat["pageName"] == jai["pageName"]
    assert datacat["linkedAt"] == jai["linkedAt"]


def test_build_saucepan_response_formatting_lands_in_scenario(tmp_path):
    client = make_client(FakeResponder(), tmp_path)

    resp = client.post("/build-saucepan", json={"character": _saucepan("1155a61e")})

    data = _decode(resp.json()["path"])
    assert data["name"] == "Taryn"
    # No Advanced Prompt; Response Formatting appended under a label instead.
    assert data["scenario"].startswith("--- Response Formatting Instructions ---")


def test_build_saucepan_hidden_card_warns_but_exports_public_fields(tmp_path):
    # A hidden companion (open_definition:false) can't yield its definition, so
    # the build warns and falls back to the public fields rather than failing.
    client = make_client(FakeResponder(), tmp_path)

    resp = client.post("/build-saucepan", json={"character": _saucepan("closed_83831943")})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert any("not open" in w for w in body["warnings"])
    # Public fields (name, blurb, greetings) survive; the hidden definition
    # (scenario / example dialogue) does not.
    assert body["fields_present"]["description"] is True
    assert body["fields_present"]["first_mes"] is True
    assert body["fields_present"]["scenario"] is False
    assert body["fields_present"]["mes_example"] is False

    data = _decode(Path(body["path"]))
    assert data["name"] == "Maddie, Alice, Laila, Veronica, Sadie"
    assert data["creator"] == "GreatN"
    assert data["extensions"]["jai"]["sourceKind"] == "saucepan_core"
    assert data["mes_example"] == ""


def _chub_node(name: str) -> dict:
    return json.loads((FIXTURES / "chub" / f"{name}.json").read_text(encoding="utf-8"))


def test_build_chub_exports_open_card_png_from_real_capture(tmp_path):
    # Phase 3B: the browser posts the raw Chub API node + linked lorebook it
    # captured (both captured live 2026-08-11, see PHASE_3B_PLAN.md Step 0);
    # the server maps/cleans/writes -- same acceptance bar as /build-saucepan.
    client = make_client(FakeResponder(), tmp_path)
    node = _chub_node("raw_api_full_your_bully")
    linked = _chub_node("raw_linked_lorebook_your_bully")

    resp = client.post("/build-chub", json={"node": node, "linked_lorebook": linked})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["fields_present"]["description"] is True
    assert body["fields_present"]["first_mes"] is True
    assert body["fields_present"]["alternate_greetings"] is True
    assert body["fields_present"]["character_book"] is True
    assert body["filename"] == "Autumn_7547962.png"
    assert body["card"]["data"]["name"] == "Autumn"

    path = Path(body["path"])
    assert path.name == "Autumn_7547962.png"

    data = _decode(path)
    assert data["name"] == "Autumn"
    assert data["creator"] == "RelicGuy"
    assert len(data["alternate_greetings"]) == 11
    assert len(data["character_book"]["entries"]) == 4
    # Raw-dict passthrough: pydantic-only fields never touched this data.
    entry = data["character_book"]["entries"][0]
    assert "probability" in entry
    assert "selectiveLogic" in entry

    jai = data["extensions"]["jai"]
    assert jai["sourceKind"] == "chub_core"
    assert jai["id"] == "7547962"
    assert jai["creatorName"] == "RelicGuy"
    # The Chub *listing* title, not the character name -- it rides only on the
    # API node, so a build that read it off the card would record "Autumn".
    assert jai["pageName"] == "Your Bully Wants To Be Your Sex Slave?!"
    # Chub's own provenance block (baked in by Chub itself) survives untouched.
    assert data["extensions"]["chub"]["id"] == 7547962
    assert data["extensions"]["chub"]["full_path"] == node["fullPath"]
    # No datacat block -- Chub isn't a datacat source.
    assert "datacat" not in data["extensions"]


def test_build_chub_skips_rewrite_when_already_on_disk(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    node = _chub_node("raw_api_full_your_bully")
    linked = _chub_node("raw_linked_lorebook_your_bully")
    body = {"node": node, "linked_lorebook": linked}

    first = client.post("/build-chub", json=body).json()
    second = client.post("/build-chub", json=body).json()

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["path"] == first["path"]
    assert second["filename"] is None  # not populated on the duplicate branch


def test_build_chub_honors_explicit_gallery_id_on_replace(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    node = _chub_node("raw_api_full_your_bully")
    linked = _chub_node("raw_linked_lorebook_your_bully")

    resp = client.post(
        "/build-chub",
        json={"node": node, "linked_lorebook": linked, "gallery_id": "kept-id-123"},
    )

    data = _decode(resp.json()["path"])
    assert data["extensions"]["gallery_id"] == "kept-id-123"


def _datacat_character(name: str) -> dict:
    raw = json.loads((FIXTURES / "datacat" / f"{name}.json").read_text(encoding="utf-8"))
    return raw["character"]


def test_build_datacat_records_the_listing_title_not_the_character_name(tmp_path):
    """DataCat mirrors the source page's title into `name` and the character's
    own name into `chat_name`. The card takes chat_name; the title has nowhere
    else to go but provenance, and this exact character is in the archive via
    /build-jai (Abbie_0d162f5f.png) carrying "Offer You Can't Refuse | Abbie" --
    so the two acquisition paths must agree on it."""
    client = make_client(FakeResponder(), tmp_path)
    character = _datacat_character("raw_api_character_abbie")

    resp = client.post("/build-datacat", json={"character": character})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["filename"] == "Abbie_0d162f5f.png"

    data = _decode(body["path"])
    assert data["name"] == "Abbie"

    jai = data["extensions"]["jai"]
    assert jai["sourceKind"] == "datacat_core"
    assert jai["pageName"] == "Offer You Can't Refuse | Abbie"
    # Both provenance blocks carry it -- the datacat block is what
    # CharacterLibrary reads, and a card that disagreed with itself would show
    # one title in the archive and another there.
    assert data["extensions"]["datacat"]["pageName"] == jai["pageName"]


# ---------------------------------------------------------------------------
# Lorebook cache -- /lorebooks/existing + /clear-lorebooks + the cache-aware
# /build-saucepan path (fetch only the misses, reference cached lorebooks by id).
# ---------------------------------------------------------------------------


def test_lorebooks_existing_splits_cached_and_missing(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    eve = _saucepan("04a0c1ac")
    lb_ids = [b["id"] for b in eve["lorebooks"]]

    # Nothing cached yet: every id is missing.
    resp = client.post("/lorebooks/existing", json={"source": "saucepan", "ids": lb_ids})
    assert resp.status_code == 200
    assert resp.json() == {"cached": [], "missing": lb_ids}

    # A full build warms the cache write-through; now both come back cached.
    assert client.post("/build-saucepan", json={"character": eve}).json()["ok"] is True
    resp = client.post(
        "/lorebooks/existing",
        json={"source": "saucepan", "ids": lb_ids + ["never-seen"]},
    )
    assert resp.json() == {"cached": lb_ids, "missing": ["never-seen"]}


def test_lorebooks_existing_namespaces_by_source(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    eve = _saucepan("04a0c1ac")
    lb_ids = [b["id"] for b in eve["lorebooks"]]
    client.post("/build-saucepan", json={"character": eve})

    # The same ids under a different source are a miss -- id spaces don't cross.
    resp = client.post("/lorebooks/existing", json={"source": "janitor", "ids": lb_ids})
    assert resp.json() == {"cached": [], "missing": lb_ids}


def test_build_saucepan_reuses_cached_lorebooks_by_id(tmp_path):
    # The heart of the cache: after one build warms the lorebooks, a second build
    # that fetches NO lorebooks but references them by `cached_lorebook_ids` must
    # reproduce the identical character_book -- proving a cache-loaded lorebook is
    # indistinguishable from a freshly fetched one.
    client = make_client(FakeResponder(), tmp_path)
    eve = _saucepan("04a0c1ac")
    lb_ids = [b["id"] for b in eve["lorebooks"]]

    first = client.post("/build-saucepan", json={"character": eve}).json()
    first_book = _decode(first["path"])["character_book"]
    assert len(first_book["entries"]) == 19

    eve_cached = {k: v for k, v in eve.items() if k != "lorebooks"}
    eve_cached["lorebooks"] = []
    eve_cached["cached_lorebook_ids"] = lb_ids
    second = client.post("/build-saucepan", json={"character": eve_cached}).json()

    assert second["ok"] is True
    assert _decode(second["path"])["character_book"] == first_book


def test_build_saucepan_skips_uncached_referenced_lorebook(tmp_path):
    # A referenced-but-uncached id is skipped (graceful degrade), not an error --
    # the safety net if the cache was cleared between the /existing check and the
    # build.
    client = make_client(FakeResponder(), tmp_path)
    eve = _saucepan("04a0c1ac")
    stripped = {k: v for k, v in eve.items() if k != "lorebooks"}
    stripped["lorebooks"] = []
    stripped["cached_lorebook_ids"] = ["totally-unknown-id"]

    body = client.post("/build-saucepan", json={"character": stripped}).json()
    assert body["ok"] is True
    assert body["fields_present"]["character_book"] is False


def test_clear_lorebooks_wipes_cache(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    eve = _saucepan("04a0c1ac")
    lb_ids = [b["id"] for b in eve["lorebooks"]]
    client.post("/build-saucepan", json={"character": eve})

    assert client.get("/health").json()["lorebooks"] == 2
    assert client.post("/clear-lorebooks").json() == {"ok": True, "removed": 2}
    assert client.get("/health").json()["lorebooks"] == 0

    resp = client.post("/lorebooks/existing", json={"source": "saucepan", "ids": lb_ids})
    assert resp.json() == {"cached": [], "missing": lb_ids}


# ---------------------------------------------------------------------------
# /existing -- "which of these ids are already on disk?" for bulk skip.
# ---------------------------------------------------------------------------


def test_existing_reports_only_ids_already_on_disk(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    akane = _character("open_akane_kujo")

    # Save one card, then ask about its id plus one we never built.
    client.post(
        "/build-jai",
        json={
            "character": {"name": "Akane Kujo", "id": "abc123"},
            "character_json": akane,
        },
    )

    resp = client.post("/existing", json={"ids": ["abc123", "never-built-999"]})

    assert resp.status_code == 200
    assert resp.json() == {"existing": ["abc123"]}


def test_existing_matches_on_id_fragment_regardless_of_name(tmp_path):
    # The saved filename keys on the first 8 id chars, so a full UUID whose
    # fragment matches an on-disk card is reported even though the caller has
    # no idea what name it was saved under.
    client = make_client(FakeResponder(), tmp_path)
    client.post(
        "/build-jai",
        json={
            "character": {"name": "Whoever", "id": "deadbeef-1111-2222-3333-444455556666"},
            "character_json": {"chat_name": "Whoever", "creator_name": "acreator"},
        },
    )

    resp = client.post(
        "/existing", json={"ids": ["deadbeef-1111-2222-3333-444455556666"]}
    )

    assert resp.json()["existing"] == ["deadbeef-1111-2222-3333-444455556666"]


def test_existing_empty_request_returns_empty(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    resp = client.post("/existing", json={"ids": []})
    assert resp.status_code == 200
    assert resp.json() == {"existing": []}


def test_build_falls_back_to_character_name_without_character_json(tmp_path):
    client = make_client(FakeResponder(), tmp_path)

    resp = client.post("/build-jai", json={"character": {"name": "No Profile Card"}})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "no first_mes / greetings found" in body["warnings"]

    data = _decode(body["path"])
    assert data["name"] == "No Profile Card"
    assert data["character_version"] == "aspect-archive"


def test_build_names_card_from_chat_name_not_title_blurb(tmp_path):
    # The userscript sends no name anymore -- the server names the card (and its
    # file) from chat_name (the real character name), never from the JSON `name`
    # field (the card-title blurb), which is preserved only as metadata.
    client = make_client(FakeResponder(), tmp_path)

    resp = client.post(
        "/build-jai",
        json={
            "character": {"id": "deadbeef-1234-5678-9abc-def012345678"},
            "character_json": {
                "chat_name": "Chatname",
                "name": "Scenario Hook Blurb",
                "creator_name": "somecreator",
            },
        },
    )

    assert resp.status_code == 200
    path = Path(resp.json()["path"])
    # chat_name drives the filename stem; the id fragment suffixes it.
    assert path.name == "Chatname_deadbeef.png"

    data = _decode(path)
    assert data["name"] == "Chatname"
    # The title blurb is preserved as metadata, not embedded as data.name.
    assert data["extensions"]["jai"]["pageName"] == "Scenario Hook Blurb"


# ---------------------------------------------------------------------------
# /build-jai -- hidden-card merge: definition + primary greeting from the chat
# capture, everything else from the JSON.
# ---------------------------------------------------------------------------


# Full-length alternate greetings (real greetings clear the 100-char floor).
_ALT_1 = "*The morning bell rang across the empty courtyard as " + ("she waited by the gate, " * 4)
_ALT_2 = "*Rain streaked the classroom windows while " + ("the lesson droned on, " * 5)


def _hidden_ari_json() -> dict:
    return {
        "chat_name": "Ari",
        "name": "A Mysterious Transfer Student",
        "creator_name": "somecreator",
        "showdefinition": False,
        "custom_tags": ["mystery"],
        "tags": [{"name": "👤 AnyPOV"}],
        # first_messages[0] (primary) nulled by the server for hidden cards; a
        # "." placeholder plus two real alternates -- the placeholder is
        # dropped by the greeting floor.
        "first_messages": [None, _ALT_1, _ALT_2, "."],
        "description": "<p>The creator's authored note.</p>",
        "avatar": "ari.webp",
        "id": "ari-id-123",
    }


def _capture_ari(client, greeting: str | None) -> None:
    messages = [{"role": "system", "content": _prompt("system_prompt_hidden_ari.txt")}]
    if greeting is not None:
        messages += [{"role": "user", "content": "."}, {"role": "assistant", "content": greeting}]
    client.post("/v1/chat/completions", json={"model": "x", "stream": False, "messages": messages})


def test_build_hidden_card_with_no_capture_fails(tmp_path):
    client = make_client(FakeResponder(), tmp_path)

    resp = client.post(
        "/build-jai",
        json={"character": {"name": "Ari"}, "character_json": _hidden_ari_json()},
    )

    body = resp.json()
    assert body["ok"] is False
    assert "hidden card not exportable" in body["warnings"][0]


def test_build_hidden_card_with_definition_but_no_greeting_fails(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    _capture_ari(client, greeting=None)  # system captured, no assistant greeting

    resp = client.post(
        "/build-jai",
        json={"character": {"name": "Ari"}, "character_json": _hidden_ari_json()},
    )

    body = resp.json()
    assert body["ok"] is False
    assert "hidden card not exportable" in body["warnings"][0]


def test_build_hidden_card_merges_capture_and_json(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    _capture_ari(client, greeting="Hello there, USER")

    resp = client.post(
        "/build-jai",
        json={"character": {"name": "Ari"}, "character_json": _hidden_ari_json()},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True

    data = _decode(body["path"])
    # Definition body comes from the chat capture.
    assert data["name"] == "Ari"
    assert "Location: USA" in data["description"]
    # Primary greeting from the capture (persona name reversed to {{user}});
    # alternates from the JSON (the "." placeholder dropped by the floor).
    assert data["first_mes"] == "Hello there, {{user}}"
    assert data["alternate_greetings"] == [_ALT_1, _ALT_2]
    # Metadata from the JSON.
    assert data["creator"] == "somecreator"
    assert data["tags"] == ["AnyPOV", "mystery"]
    # creator_notes leads with a markdown reference to the original avatar.
    assert data["creator_notes"] == (
        "![Ari](https://ella.janitorai.com/bot-avatars/ari.webp)\n\n"
        "The creator's authored note."
    )
    assert data["extensions"]["jai"]["pageName"] == "A Mysterious Transfer Student"


# ---------------------------------------------------------------------------
# /build-jai -- lorebook mapping from the lorebooks payload.
# ---------------------------------------------------------------------------


def test_build_populates_character_book_from_lorebooks_payload(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    raw_script = json.loads((FIXTURES / "hampter_script_kamii_university.json").read_text(encoding="utf-8"))

    resp = client.post(
        "/build-jai",
        json={
            "character": {"name": "Akane Kujo"},
            "character_json": {"chat_name": "Akane Kujo", "first_messages": ["hi"]},
            "lorebooks": [{"id": raw_script["id"], "raw": raw_script}],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["fields_present"]["character_book"] is True

    book = _decode(body["path"])["character_book"]
    assert book["name"] == "Kamii University: A Living Campus"
    assert len(book["entries"]) == 20
    assert book["entries"][0]["content"].startswith("Kamii University: The Living Campus")


def test_build_with_no_lorebooks_has_no_character_book(tmp_path):
    client = make_client(FakeResponder(), tmp_path)

    resp = client.post(
        "/build-jai",
        json={"character": {"name": "No Lore"}, "character_json": {"chat_name": "No Lore"}},
    )

    assert resp.json()["fields_present"]["character_book"] is False


def test_build_surfaces_lorebook_mapping_warnings(tmp_path):
    client = make_client(FakeResponder(), tmp_path)

    resp = client.post(
        "/build-jai",
        json={
            "character": {"name": "Broken Script Owner"},
            "character_json": {"chat_name": "Broken Script Owner"},
            "lorebooks": [
                {"id": "broken", "raw": {"type": "lorebook", "id": "broken", "title": "Broken", "script": "not json"}}
            ],
        },
    )

    body = resp.json()
    assert any("Broken" in w for w in body["warnings"])
    assert body["fields_present"]["character_book"] is False


# ---------------------------------------------------------------------------
# /capture-status + /clear-captures
# ---------------------------------------------------------------------------


def test_capture_status_unknown_name_is_all_false(tmp_path):
    client = make_client(FakeResponder(), tmp_path)
    status = client.get("/capture-status", params={"name": "Nobody"}).json()
    assert status == {"name": "Nobody", "system": False, "greetings": False}


def test_clear_captures_wipes_state_but_leaves_pngs(tmp_path):
    captures_dir = tmp_path / "captures"
    output_dir = tmp_path / "cards"
    deps.capture_store = CaptureStore(captures_dir=captures_dir)
    deps.png_writer = PngWriter(output_dir=output_dir)
    deps.responder = FakeResponder()
    deps.avatar_fetcher = FakeAvatarFetcher()
    client = TestClient(server_module.app)

    _capture_ari(client, greeting="Hi USER")
    resp = client.post(
        "/build-jai",
        json={"character": {"name": "Ari"}, "character_json": _hidden_ari_json()},
    )
    assert resp.json()["ok"] is True
    assert any(output_dir.rglob("*.png"))

    body = client.post("/clear-captures").json()
    assert body["ok"] is True
    assert body["removed"] > 0

    status = client.get("/capture-status", params={"name": "Ari"}).json()
    assert status == {"name": "Ari", "system": False, "greetings": False}
    assert any(output_dir.rglob("*.png"))


def test_a_built_card_queues_its_images_once_and_a_duplicate_does_not(tmp_path, queued_downloads):
    # Every /build-* route shares fetch_avatar_and_write; a duplicate returns
    # before it, so the same card exported twice queues one download.
    client = make_client(FakeResponder(), tmp_path)
    body = {"node": _chub_node("raw_api_full_your_bully"), "linked_lorebook": _chub_node("raw_linked_lorebook_your_bully")}

    first = client.post("/build-chub", json=body).json()
    client.post("/build-chub", json=body)

    assert queued_downloads == [first["filename"]]
