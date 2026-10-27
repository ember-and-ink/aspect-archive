"""`POST /api/v1/characters/{id}/media` and `GET .../media` -- the download
route and manifest read, wired on top of `aspect/media/writer.py`
(docs/PHASE_3C_PLAN.md §3, step 4). `media_writer.download_item` itself is
covered exhaustively in tests/test_media_writer.py; these tests are about the
route's own job: resolving the gallery folder from the card (creating it and
its gallery_id on first use), streaming NDJSON, and persisting the manifest
and ledger a run produces.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from aspect.config import settings
from aspect.media import extractors as media_extractors, manifest as media_manifest, writer as media_writer
from aspect.api.v1 import _shared as v1_shared, media as v1_media


def _ndjson(response) -> list[dict]:
    return [json.loads(line) for line in response.text.strip().splitlines()]


@pytest.fixture
def stub_download_item(monkeypatch):
    """Replaces the real network-touching pipeline with a scripted outcome
    per URL, keyed by a simple counter -- the route's own logic (folder
    resolution, streaming, manifest persistence) is what these tests check,
    not the pipeline itself."""

    calls: list[dict] = []

    async def fake(client, gallery_dir, folder_name, *, url, filename_hint, prefix, index, index_state, manifest, ledger, thumbnail_store, fetch_gate=None, retry_dead=False):
        calls.append(
            {"gallery_dir": gallery_dir, "folder_name": folder_name, "url": url, "prefix": prefix, "retry_dead": retry_dead}
        )
        outcome = media_writer.DownloadOutcome("saved", url, file=f"localized_media_{index}_x.webp", bytes=123)
        media_manifest.record_saved(manifest, url, outcome.file, "deadbeef")
        return outcome

    monkeypatch.setattr(v1_media.media_writer, "download_item", fake)
    return calls


def test_download_creates_gallery_folder_for_a_card_with_no_folder_yet(client, populated_archive, stub_download_item):
    # Bella carries a gallery_id but has no folder on disk (the common real
    # case: images never downloaded yet).
    resp = client.post(
        "/api/v1/characters/Bella_11112222.png/media",
        json={"items": [{"url": "https://cdn.example.com/x/pic.png"}]},
    )
    assert resp.status_code == 200
    lines = _ndjson(resp)
    assert lines[0]["type"] == "item"
    assert lines[0]["url"] == "https://cdn.example.com/x/pic.png"
    assert lines[0]["status"] == "saved"
    assert lines[0]["file"].startswith("localized_media_")
    assert lines[0]["bytes"] == 123
    assert lines[-1]["type"] == "done"
    assert lines[-1] == {"type": "done", "saved": 1, "skipped": 0, "errors": 0, "total": 1}

    folder = populated_archive["galleries"] / "Bella_BBBBBBBBBBBB"
    assert folder.is_dir()
    assert (folder / media_manifest.MANIFEST_NAME).is_file()


def test_download_resolves_existing_folder_for_a_card_that_already_has_one(client, populated_archive, stub_download_item):
    resp = client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media",
        json={"items": [{"url": "https://cdn.example.com/x/a.png"}]},
    )
    assert resp.status_code == 200
    assert stub_download_item[0]["folder_name"] == "Abbie_kzbYR2QbpncC"


def test_download_mints_a_gallery_id_for_a_card_that_has_none(client, populated_archive, stub_download_item, monkeypatch):
    from tests.conftest import card_png, jai_extensions

    (populated_archive["characters"] / "NoGallery_55556666.png").write_bytes(
        card_png("NoGallery", extensions=jai_extensions("55556666-0000-0000-0000-000000000000", gallery_id=None))
    )
    client.post("/api/v1/refresh")

    resp = client.post(
        "/api/v1/characters/NoGallery_55556666.png/media",
        json={"items": [{"url": "https://cdn.example.com/x/a.png"}]},
    )
    assert resp.status_code == 200
    folder_name = stub_download_item[0]["folder_name"]
    assert folder_name.startswith("NoGallery_")
    assert (populated_archive["galleries"] / folder_name).is_dir()

    # The minted id was written back onto the card.
    detail = client.get("/api/v1/characters/NoGallery_55556666.png").json()
    assert detail["card"]["extensions"]["gallery_id"]


def test_download_unknown_card_is_404(client, stub_download_item):
    resp = client.post(
        "/api/v1/characters/DoesNotExist_00000000.png/media",
        json={"items": [{"url": "https://cdn.example.com/x/a.png"}]},
    )
    assert resp.status_code == 404
    assert stub_download_item == []


def test_get_media_manifest_for_a_card_never_downloaded_is_empty(client):
    resp = client.get("/api/v1/characters/Cleo_33334444.png/media")
    assert resp.status_code == 200
    assert resp.json() == {"folder": "Cleo_CCCCCCCCCCCC", "files": {}, "dead": {}, "runs": []}


def test_get_media_manifest_reflects_a_completed_run(client, populated_archive, stub_download_item):
    client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media",
        json={"items": [{"url": "https://cdn.example.com/x/a.png"}], "phase": "embedded"},
    )
    manifest = client.get("/api/v1/characters/Abbie_0d162f5f.png/media").json()
    assert manifest["folder"] == "Abbie_kzbYR2QbpncC"
    assert "https://cdn.example.com/x/a.png" in manifest["files"]
    assert manifest["runs"][-1]["phase"] == "embedded"
    assert manifest["runs"][-1]["saved"] == 1


def test_download_totals_count_mixed_outcomes(client, populated_archive, monkeypatch):
    async def fake(client, gallery_dir, folder_name, *, url, filename_hint, prefix, index, index_state, manifest, ledger, thumbnail_store, fetch_gate=None, retry_dead=False):
        if "ok" in url:
            outcome = media_writer.DownloadOutcome("saved", url, file="localized_media_0_ok.webp", bytes=1)
            media_manifest.record_saved(manifest, url, outcome.file, "abc")
            return outcome
        if "dup" in url:
            return media_writer.DownloadOutcome("skipped", url, file="localized_media_0_ok.webp", reason="filename match")
        return media_writer.DownloadOutcome("error", url, reason="boom", permanent=False)

    monkeypatch.setattr(v1_media.media_writer, "download_item", fake)

    resp = client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media",
        json={
            "items": [
                {"url": "https://cdn.example.com/ok.png"},
                {"url": "https://cdn.example.com/dup.png"},
                {"url": "https://cdn.example.com/bad.png"},
            ]
        },
    )
    lines = _ndjson(resp)
    assert lines[-1] == {"type": "done", "saved": 1, "skipped": 1, "errors": 1, "total": 3}


# --------------------------------------------------------------------------
# POST /characters/{id}/media/bytes -- the browser-fetch door (MEGA/Pixiv,
# docs/PHASE_3C_PLAN.md §6). No network mock needed: media_writer.finish_item
# is plain sync code operating on bytes already in the request body.
# --------------------------------------------------------------------------


def _png_bytes() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def test_media_bytes_saves_an_already_fetched_item(client, populated_archive):
    resp = client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media/bytes",
        data={"url": "mega://folder/handle#key", "filename": "cool pic", "prefix": "extgallery"},
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "saved"
    assert body["file"].startswith("extgallery_")
    assert body["file"].endswith(".webp")

    folder = populated_archive["galleries"] / "Abbie_kzbYR2QbpncC"
    assert (folder / body["file"]).is_file()

    manifest = client.get("/api/v1/characters/Abbie_0d162f5f.png/media").json()
    assert "mega://folder/handle#key" in manifest["files"]


def test_media_bytes_defaults_prefix_to_extgallery(client, populated_archive):
    resp = client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media/bytes",
        data={"url": "mega://folder/handle2"},
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json()["file"].startswith("extgallery_")


def test_media_bytes_invalid_content_is_an_error(client, populated_archive):
    resp = client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media/bytes",
        data={"url": "mega://folder/bad"},
        files={"file": ("error.html", b"<!DOCTYPE html>error</html>", "text/html")},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


def test_media_bytes_empty_upload_is_422(client, populated_archive):
    resp = client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media/bytes",
        data={"url": "mega://folder/empty"},
        files={"file": ("empty.png", b"", "image/png")},
    )
    assert resp.status_code == 422


def test_media_bytes_unknown_card_is_404(client):
    resp = client.post(
        "/api/v1/characters/DoesNotExist_00000000.png/media/bytes",
        data={"url": "mega://folder/handle"},
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# POST /characters/{id}/media/have -- the name-index check the browser-fetch
# door asks *before* fetching, so a re-run over a complete MEGA folder doesn't
# download and decrypt every file only to be told we already had it.
# --------------------------------------------------------------------------


def test_media_have_reports_a_file_the_gallery_already_has(client, populated_archive):
    folder = populated_archive["galleries"] / "Abbie_kzbYR2QbpncC"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "extgallery_1782213642053_vF9hgQCD.webp").write_bytes(_png_bytes())

    resp = client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media/have",
        json={
            "items": [
                {"url": "mega://Ab0CdEfG/vF9hgQCD", "filename": "01.png"},
                {"url": "mega://Ab0CdEfG/notHereYet", "filename": "02.png"},
            ],
            "prefix": "extgallery",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["have"] == {"mega://Ab0CdEfG/vF9hgQCD": "extgallery_1782213642053_vF9hgQCD.webp"}


def test_media_have_records_the_match_in_the_manifest(client, populated_archive):
    """A skip that goes unrecorded leaves /media/status reporting the card as
    never downloaded -- same reason download_item records its name matches."""
    folder = populated_archive["galleries"] / "Abbie_kzbYR2QbpncC"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "extgallery_1_vF9hgQCD.webp").write_bytes(_png_bytes())

    client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media/have",
        json={"items": [{"url": "mega://Ab0CdEfG/vF9hgQCD"}], "prefix": "extgallery"},
    )

    manifest = client.get("/api/v1/characters/Abbie_0d162f5f.png/media").json()
    entry = manifest["files"]["mega://Ab0CdEfG/vF9hgQCD"]
    assert entry["file"] == "extgallery_1_vF9hgQCD.webp"
    assert entry["sha256"] == hashlib.sha256(_png_bytes()).hexdigest()


def test_media_have_is_empty_for_a_gallery_with_nothing_in_it(client, populated_archive):
    resp = client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media/have",
        json={"items": [{"url": "mega://Ab0CdEfG/vF9hgQCD"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["have"] == {}


def test_media_have_never_downgrades_a_higher_priority_prefix(client, populated_archive):
    folder = populated_archive["galleries"] / "Abbie_kzbYR2QbpncC"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "localized_media_1_vF9hgQCD.webp").write_bytes(_png_bytes())

    resp = client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media/have",
        json={"items": [{"url": "mega://Ab0CdEfG/vF9hgQCD"}], "prefix": "extgallery"},
    )
    assert resp.json()["have"] == {"mega://Ab0CdEfG/vF9hgQCD": "localized_media_1_vF9hgQCD.webp"}


def test_media_have_unknown_card_is_404(client):
    resp = client.post(
        "/api/v1/characters/DoesNotExist_00000000.png/media/have",
        json={"items": [{"url": "mega://folder/handle"}]},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# GET /media/status -- the archive-wide summary Bulk Localize reads instead
# of one request per card (docs/PHASE_3C_PLAN.md §3).
# --------------------------------------------------------------------------


def test_media_status_omits_cards_with_no_gallery_folder(client):
    body = client.get("/api/v1/media/status").json()
    # Cleo has a gallery_id but no folder on disk yet -- "never downloaded",
    # not a zeroed row.
    assert "Cleo_33334444.png" not in body["cards"]


def test_media_status_omits_cards_with_a_folder_but_no_manifest(client, populated_archive):
    # Abbie's fixture gallery folder has files but no run has ever gone
    # through the download route, so there is no .media.json yet.
    body = client.get("/api/v1/media/status").json()
    assert "Abbie_0d162f5f.png" not in body["cards"]


def test_media_status_reflects_a_completed_run(client, populated_archive, stub_download_item):
    client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media",
        json={"items": [{"url": "https://cdn.example.com/x/a.png"}]},
    )
    body = client.get("/api/v1/media/status").json()
    entry = body["cards"]["Abbie_0d162f5f.png"]
    assert entry["files"] == 1
    assert entry["dead"] == 0
    assert entry["complete"] is True
    assert entry["last_run"] is not None


def test_media_status_incomplete_when_last_run_had_errors(client, populated_archive, monkeypatch):
    async def fake(client, gallery_dir, folder_name, *, url, filename_hint, prefix, index, index_state, manifest, ledger, thumbnail_store, fetch_gate=None, retry_dead=False):
        return media_writer.DownloadOutcome("error", url, reason="boom", permanent=False)

    monkeypatch.setattr(v1_media.media_writer, "download_item", fake)
    client.post(
        "/api/v1/characters/Abbie_0d162f5f.png/media",
        json={"items": [{"url": "https://cdn.example.com/x/bad.png"}]},
    )
    body = client.get("/api/v1/media/status").json()
    assert body["cards"]["Abbie_0d162f5f.png"]["complete"] is False


# --------------------------------------------------------------------------
# POST /galleries/{folder}/thumbs/prune -- docs/PHASE_3C_PLAN.md §5.
# --------------------------------------------------------------------------


def test_prune_thumbs_drops_orphans_and_keeps_live_ones(client, populated_archive):
    folder = "Abbie_kzbYR2QbpncC"
    gallery_dir = populated_archive["galleries"] / folder
    live_files = [p.name for p in gallery_dir.iterdir() if p.is_file()]
    assert live_files, "fixture gallery must have at least one file"
    live_name = live_files[0]

    thumb_dir = populated_archive["thumbs"] / "gallery" / folder
    thumb_dir.mkdir(parents=True, exist_ok=True)
    (thumb_dir / f"{live_name}_288.webp").write_bytes(b"webp")
    (thumb_dir / "ghost.png_288.webp").write_bytes(b"webp")
    # The pre-WebP encoding, which the cache is still full of. A pruner that only
    # recognises the current format abandons the files it exists to clean.
    (thumb_dir / f"{live_name}_384.jpg").write_bytes(b"jpg")
    (thumb_dir / "ghost.png_384.jpg").write_bytes(b"jpg")

    resp = client.post(f"/api/v1/galleries/{folder}/thumbs/prune")
    assert resp.status_code == 200
    body = resp.json()
    assert body["folder"] == folder
    assert body["removed"] == 2
    assert (thumb_dir / f"{live_name}_288.webp").is_file()
    assert (thumb_dir / f"{live_name}_384.jpg").is_file()
    assert not (thumb_dir / "ghost.png_288.webp").is_file()
    assert not (thumb_dir / "ghost.png_384.jpg").is_file()


def test_prune_thumbs_unknown_folder_removes_nothing(client, populated_archive):
    resp = client.post("/api/v1/galleries/NoSuchFolder_ABCDEFGHIJKL/thumbs/prune")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 0


# --------------------------------------------------------------------------
# POST /characters/{id}/media/scan -- salvage item 1 (UI_REWRITE_PLAN.md
# §1.3, §3.4), the server-side discovery preview.
# --------------------------------------------------------------------------


def test_scan_finds_embedded_and_lorebook_urls_separately(client, archive_dirs):
    from tests.conftest import card_png, jai_extensions

    (archive_dirs["characters"] / "Scan_77778888.png").write_bytes(
        card_png(
            "Scan",
            description="see https://cdn.example.com/desc.png",
            character_book={"entries": [{"keys": ["a"], "content": "https://cdn.example.com/lore.png"}]},
            extensions=jai_extensions("77778888-0000-0000-0000-000000000000", gallery_id="DDDDDDDDDDDD"),
        )
    )
    client.post("/api/v1/refresh")

    resp = client.post("/api/v1/characters/Scan_77778888.png/media/scan")
    assert resp.status_code == 200
    assert resp.json() == {
        "embedded": ["https://cdn.example.com/desc.png"],
        "lorebook": ["https://cdn.example.com/lore.png"],
        "sources": [],
    }


def test_scan_finds_nothing_in_a_card_with_no_urls(client, populated_archive):
    resp = client.post("/api/v1/characters/Cleo_33334444.png/media/scan")
    assert resp.status_code == 200
    assert resp.json() == {"embedded": [], "lorebook": [], "sources": []}


def test_scan_unknown_card_is_404(client):
    resp = client.post("/api/v1/characters/DoesNotExist_00000000.png/media/scan")
    assert resp.status_code == 404


# ------------------------------------------------------------------
# POST /media/jobs -- 3C-2, the job runner (docs/PHASE_3C_PLAN.md §7)
# ------------------------------------------------------------------


def _poll_job(client, job_id: str, *, after: int = 0, timeout: float = 5.0) -> dict:
    """Poll until the job leaves queued/running. The worker runs on the
    TestClient's own portal loop, concurrently with these synchronous
    requests, so a real (short) wait loop is the honest way to test it."""
    import time

    deadline = time.monotonic() + timeout
    body = {"state": "queued"}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/media/jobs/{job_id}", params={"after": after})
        assert resp.status_code == 200
        body = resp.json()
        if body["state"] not in ("queued", "running"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish in time: {body}")


def test_media_job_runs_in_the_background_and_writes_the_manifest(client, populated_archive, stub_download_item):
    resp = client.post(
        "/api/v1/media/jobs",
        json={
            "card_id": "Bella_11112222.png",
            "items": [{"url": "https://cdn.example.com/x/pic.png"}],
            "prefix": "localized_media",
            "phase": "embedded",
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert resp.json()["total"] == 1

    body = _poll_job(client, job_id)
    assert body["state"] == "done"
    assert body["saved"] == 1
    assert body["done"] == 1
    assert body["events"][0]["status"] == "saved"
    assert body["events"][0]["file"].startswith("localized_media_")

    folder = populated_archive["galleries"] / "Bella_BBBBBBBBBBBB"
    assert (folder / media_manifest.MANIFEST_NAME).is_file()


def test_media_job_events_cursor_only_returns_new_items(client, populated_archive, stub_download_item):
    resp = client.post(
        "/api/v1/media/jobs",
        json={
            "card_id": "Bella_11112222.png",
            "items": [{"url": "https://cdn.example.com/x/a.png"}, {"url": "https://cdn.example.com/x/b.png"}],
        },
    )
    job_id = resp.json()["job_id"]
    body = _poll_job(client, job_id)
    assert body["total"] == 2
    assert len(body["events"]) == 2

    # Re-polling with the cursor the first call handed back yields nothing new.
    resp = client.get(f"/api/v1/media/jobs/{job_id}", params={"after": body["next_cursor"]})
    assert resp.json()["events"] == []


def test_media_job_unknown_card_404s(client, populated_archive):
    resp = client.post(
        "/api/v1/media/jobs",
        json={"card_id": "NoSuchCard.png", "items": [{"url": "https://cdn.example.com/x.png"}]},
    )
    assert resp.status_code == 404


def test_media_job_unknown_job_id_404s(client, populated_archive):
    resp = client.get("/api/v1/media/jobs/does-not-exist")
    assert resp.status_code == 404


def test_cancel_unknown_job_404s(client, populated_archive):
    resp = client.post("/api/v1/media/jobs/does-not-exist/cancel")
    assert resp.status_code == 404


def test_cancel_stops_a_job_before_it_finishes_every_item(client, populated_archive, monkeypatch):
    """A slow-item stub gives the test a window to cancel mid-run.

    Concurrency is pinned low here on purpose: a cancel can only stop items
    the batch hasn't dispatched yet, so with the default width every item of a
    five-item job is already in flight before the cancel arrives.
    """
    import asyncio

    monkeypatch.setattr(settings, "media_concurrency", 2)

    calls: list[str] = []

    async def slow_fake(client, gallery_dir, folder_name, *, url, filename_hint, prefix, index, index_state, manifest, ledger, thumbnail_store, fetch_gate=None, retry_dead=False):
        calls.append(url)
        await asyncio.sleep(0.05)
        outcome = media_writer.DownloadOutcome("saved", url, file=f"localized_media_{index}_x.webp", bytes=1)
        media_manifest.record_saved(manifest, url, outcome.file, "deadbeef")
        return outcome

    monkeypatch.setattr(v1_media.media_writer, "download_item", slow_fake)

    resp = client.post(
        "/api/v1/media/jobs",
        json={
            "card_id": "Bella_11112222.png",
            "items": [{"url": f"https://cdn.example.com/{i}.png"} for i in range(5)],
        },
    )
    job_id = resp.json()["job_id"]

    # Give the worker a moment to start, then cancel.
    import time

    time.sleep(0.06)
    cancel_resp = client.post(f"/api/v1/media/jobs/{job_id}/cancel")
    assert cancel_resp.status_code == 200

    body = _poll_job(client, job_id)
    assert body["state"] == "cancelled"
    assert body["done"] < 5


def test_media_job_discover_scans_the_card_instead_of_taking_items(client, archive_dirs, stub_download_item):
    from tests.conftest import card_png, jai_extensions

    (archive_dirs["characters"] / "Scan_77778888.png").write_bytes(
        card_png(
            "Scan",
            description="see https://cdn.example.com/desc.png",
            character_book={"entries": [{"keys": ["a"], "content": "https://cdn.example.com/lore.png"}]},
            extensions=jai_extensions("77778888-0000-0000-0000-000000000000", gallery_id="DDDDDDDDDDDD"),
        )
    )
    client.post("/api/v1/refresh")

    resp = client.post(
        "/api/v1/media/jobs",
        json={"card_id": "Scan_77778888.png", "discover": True, "items": [{"url": "https://ignored.example.com/x.png"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    body = _poll_job(client, resp.json()["job_id"])
    assert body["state"] == "done"
    urls = {call["url"] for call in stub_download_item}
    assert urls == {"https://cdn.example.com/desc.png", "https://cdn.example.com/lore.png"}


def test_list_media_jobs_filters_by_card_and_active(client, populated_archive, stub_download_item):
    r1 = client.post(
        "/api/v1/media/jobs",
        json={"card_id": "Bella_11112222.png", "items": [{"url": "https://cdn.example.com/a.png"}]},
    )
    r2 = client.post(
        "/api/v1/media/jobs",
        json={"card_id": "Abbie_0d162f5f.png", "items": [{"url": "https://cdn.example.com/b.png"}]},
    )
    _poll_job(client, r1.json()["job_id"])
    _poll_job(client, r2.json()["job_id"])

    resp = client.get("/api/v1/media/jobs", params={"card_id": "Bella_11112222.png"})
    assert resp.status_code == 200
    jobs = resp.json()
    assert len(jobs) == 1
    assert jobs[0]["card_id"] == "Bella_11112222.png"
    assert jobs[0]["events"] == []  # list view omits per-item events

    resp = client.get("/api/v1/media/jobs", params={"active": True})
    assert resp.json() == []


# ---------------------------------------------------------------------------
# The "Needs media" filter on GET /characters (docs/UI_REWRITE_PLAN.md §3.3).
# ---------------------------------------------------------------------------


def _write_manifest(gallery_dir, *, dead: dict[str, str] | None = None, errors: int = 0) -> None:
    """A manifest for a gallery that has had a media run."""
    manifest = media_manifest.empty_manifest()
    media_manifest.record_saved(manifest, "https://cdn.example.com/ok.png", "localized_media_0_ok.webp", "abc")
    for url, reason in (dead or {}).items():
        media_manifest.record_dead(manifest, url, reason)
    media_manifest.append_run(manifest, {"at": media_manifest.now_iso(), "saved": 1, "skipped": 0, "errors": errors})
    media_manifest.save_manifest(gallery_dir, manifest)


def test_needs_media_finds_cards_whose_manifest_carries_dead_urls(client, populated_archive):
    gallery_dir = populated_archive["galleries"] / "Abbie_kzbYR2QbpncC"
    _write_manifest(gallery_dir, dead={"https://cdn.example.com/gone.png": "404"})

    resp = client.get("/api/v1/characters", params={"needs_media": True, "limit": 0})

    assert resp.status_code == 200
    assert [c["id"] for c in resp.json()["items"]] == ["Abbie_0d162f5f.png"]


def test_needs_media_finds_a_run_that_reported_errors(client, populated_archive):
    gallery_dir = populated_archive["galleries"] / "Abbie_kzbYR2QbpncC"
    _write_manifest(gallery_dir, errors=2)

    resp = client.get("/api/v1/characters", params={"needs_media": True, "limit": 0})

    assert [c["id"] for c in resp.json()["items"]] == ["Abbie_0d162f5f.png"]


def test_a_clean_run_does_not_need_media(client, populated_archive):
    gallery_dir = populated_archive["galleries"] / "Abbie_kzbYR2QbpncC"
    _write_manifest(gallery_dir)

    assert client.get("/api/v1/characters", params={"needs_media": True, "limit": 0}).json()["items"] == []
    # And its complement returns everything, including the never-scanned cards.
    resp = client.get("/api/v1/characters", params={"needs_media": False, "limit": 0})
    assert len(resp.json()["items"]) == 3


def test_a_card_never_scanned_is_not_claimed_to_need_media(client, populated_archive):
    # No manifest written at all: whether Cleo has remote URLs is a question
    # only /media/scan can answer, and the list path does not guess.
    resp = client.get("/api/v1/characters", params={"needs_media": True, "limit": 0})

    assert resp.json()["items"] == []


def test_needs_media_composes_with_the_other_filters(client, populated_archive):
    gallery_dir = populated_archive["galleries"] / "Abbie_kzbYR2QbpncC"
    _write_manifest(gallery_dir, dead={"https://cdn.example.com/gone.png": "404"})

    # Abbie needs media but is not tagged Male, so the AND is empty.
    resp = client.get("/api/v1/characters", params={"needs_media": True, "tag": "Male", "limit": 0})
    assert resp.json()["items"] == []

    resp = client.get("/api/v1/characters", params={"needs_media": True, "tag": "Vampire", "limit": 0})
    assert [c["id"] for c in resp.json()["items"]] == ["Abbie_0d162f5f.png"]


def test_omitting_needs_media_pays_no_manifest_io(client, populated_archive, monkeypatch):
    """The filter is opt-in: an ordinary list call must not read manifests."""
    reads: list = []
    real = media_manifest.load_manifest
    monkeypatch.setattr(
        "aspect.api.v1.characters.media_manifest.load_manifest",
        lambda gallery_dir: (reads.append(gallery_dir), real(gallery_dir))[1],
    )

    client.get("/api/v1/characters", params={"limit": 0})

    assert reads == []


# ---------------------------------------------------------------------------
# POST /media/jobs {scope: "all"} -- bulk localize (UI_REWRITE_PLAN.md Stage 6B)
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_discovery(monkeypatch):
    """One media URL per card, so a batch has something to do. The fixture
    cards carry no URLs of their own; discovery itself is covered by
    tests/media/test_discovery.py."""

    def fake(data):
        return ([f"https://cdn.example.com/{data.get('name', 'x')}.png"], [])

    monkeypatch.setattr(v1_media.media_discovery, "find_character_media_urls", fake)


def _submit_batch(client, **body) -> str:
    resp = client.post("/api/v1/media/jobs", json={"scope": "all", **body})
    assert resp.status_code == 200, resp.text
    return resp.json()["job_id"]


def test_batch_scope_visits_every_card(client, populated_archive, stub_download_item, stub_discovery):
    body = _poll_job(client, _submit_batch(client))

    assert body["state"] == "done"
    assert body["scope"] == "all"
    # A batch counts cards, not URLs -- `unit` is how a client knows which.
    assert body["unit"] == "cards"
    assert body["cards_total"] == 3
    assert body["cards_done"] == 3
    assert body["cards_skipped"] == 0
    assert body["current_card_id"] is None
    assert {call["url"] for call in stub_download_item} == {
        "https://cdn.example.com/Abbie.png",
        "https://cdn.example.com/Bella.png",
        "https://cdn.example.com/Cleo.png",
    }


def _account_for_abbies_media(galleries) -> None:
    """A manifest that accounts for exactly the URL `stub_discovery` puts on
    Abbie -- the state a finished run leaves behind."""
    manifest = media_manifest.empty_manifest()
    media_manifest.record_saved(
        manifest, "https://cdn.example.com/Abbie.png", "localized_media_0_Abbie.webp", "abc"
    )
    media_manifest.append_run(
        manifest, {"at": media_manifest.now_iso(), "saved": 1, "skipped": 0, "errors": 0}
    )
    media_manifest.save_manifest(galleries / "Abbie_kzbYR2QbpncC", manifest)


def test_batch_skips_a_card_whose_sources_are_all_accounted_for(client, populated_archive, stub_download_item, stub_discovery):
    _account_for_abbies_media(populated_archive["galleries"])

    body = _poll_job(client, _submit_batch(client))

    assert body["cards_skipped"] == 1
    assert body["cards_done"] == 2
    # The point of skipping: no work was done for that card at all.
    assert all("Abbie" not in call["url"] for call in stub_download_item)


def test_batch_visits_a_clean_card_whose_source_is_unaccounted_for(client, populated_archive, stub_download_item, stub_discovery):
    """The regression this replaced a `complete` check to catch. A run with no
    errors used to mean "done forever", so a card whose gallery link nothing
    could resolve at the time -- a Civitai post, before `media/civitai.py` --
    stayed finished no matter what was added later. Skipping now keys on the
    card's sources, not on how its last run went."""
    _write_manifest(populated_archive["galleries"] / "Abbie_kzbYR2QbpncC")

    body = _poll_job(client, _submit_batch(client))

    assert body["cards_skipped"] == 0
    assert any("Abbie" in call["url"] for call in stub_download_item)


def test_batch_re_arms_a_source_that_has_since_gained_a_handler(client, populated_archive, stub_download_item, stub_discovery, monkeypatch):
    """The whole point of recording `unhandled`: the card carries a URL a
    previous build could not fetch, this build can, and nobody has to press
    "Rescan everything" for it to be noticed."""
    monkeypatch.setattr(
        v1_media.media_discovery,
        "collect_card_text_chunks",
        lambda data: (["gallery: https://catbox.moe/c/abc123"], []),
    )
    monkeypatch.setattr(v1_media.media_discovery, "find_character_media_urls", lambda data: ([], []))
    monkeypatch.setattr(
        media_extractors, "resolve_gallery_url", lambda client_, url: []
    )

    galleries = populated_archive["galleries"]
    manifest = media_manifest.empty_manifest()
    media_manifest.record_source(manifest, "https://catbox.moe/c/abc123", None, media_manifest.SOURCE_UNHANDLED)
    media_manifest.append_run(
        manifest, {"at": media_manifest.now_iso(), "saved": 0, "skipped": 0, "errors": 0}
    )
    media_manifest.save_manifest(galleries / "Abbie_kzbYR2QbpncC", manifest)

    body = _poll_job(client, _submit_batch(client))

    assert body["cards_skipped"] == 0
    # ...and the run that re-armed it records the handler, so the next one skips.
    reloaded = media_manifest.load_manifest(galleries / "Abbie_kzbYR2QbpncC")
    assert reloaded["sources"]["https://catbox.moe/c/abc123"]["h"] == "catbox"


def test_batch_settles_a_card_whose_album_is_gone(client, populated_archive, stub_download_item, stub_discovery, monkeypatch):
    """A 404'd album is recorded `failed` and the card stops re-arming over
    it. Before, it recorded nothing, so the card -- and every other gallery
    on it -- was re-resolved on every Localize all."""
    monkeypatch.setattr(
        v1_media.media_discovery,
        "collect_card_text_chunks",
        lambda data: (["gallery: https://imgchest.com/p/m9yx53zvq7q"], []),
    )
    monkeypatch.setattr(v1_media.media_discovery, "find_character_media_urls", lambda data: ([], []))
    resolved = []

    def gone(client_, url):
        resolved.append(url)
        raise media_extractors.GalleryGone("HTTP 404")

    monkeypatch.setattr(media_extractors, "resolve_gallery_url", gone)

    first = _poll_job(client, _submit_batch(client))
    calls_after_first = len(resolved)
    second = _poll_job(client, _submit_batch(client))

    assert first["cards_skipped"] == 0
    assert calls_after_first > 0
    entry = media_manifest.load_manifest(populated_archive["galleries"] / "Abbie_kzbYR2QbpncC")["sources"][
        "https://imgchest.com/p/m9yx53zvq7q"
    ]
    assert (entry["h"], entry["st"], entry["r"]) == ("imgchest", media_manifest.SOURCE_FAILED, "HTTP 404")
    assert len(resolved) == calls_after_first
    assert second["cards_skipped"] == first["cards_done"]


def test_batch_everything_visits_the_accounted_for_card_anyway(client, populated_archive, stub_download_item, stub_discovery):
    _account_for_abbies_media(populated_archive["galleries"])

    body = _poll_job(client, _submit_batch(client, mode="everything"))

    assert body["cards_skipped"] == 0
    assert body["cards_done"] == 3
    assert any("Abbie" in call["url"] for call in stub_download_item)


# --- Rescan everything / Rescan broken -------------------------------------

# Real `dead` entries, from a dev-archive card (Vespera's file.garden images).
_VESPERA_DEAD = {
    "https://file.garden/akviCfKE-zqjKa2t/ves_job.jpg": {"reason": "HTTP 404", "attempts": 1, "at": "2026-08-30T10:49:12Z"},
    "https://file.garden/akviCfKE-zqjKa2t/ves_medic.png": {"reason": "HTTP 404", "attempts": 1, "at": "2026-08-30T10:49:12Z"},
}
_FAILED_ALBUM = "https://imgchest.com/p/m9yx53zvq7q"
_DONE_ALBUM = "https://imgchest.com/p/ej7mjmm597d"
_UNSEEN_ALBUM = "https://catbox.moe/c/abc123"


def _broken_abbie(galleries, *, dead=_VESPERA_DEAD, failed_album=False) -> None:
    """Abbie after a real run: her own image saved, some links dead, the done
    album recorded, maybe an album gone."""
    manifest = media_manifest.empty_manifest()
    media_manifest.record_saved(manifest, "https://cdn.example.com/Abbie.png", "localized_media_0_Abbie.webp", "abc")
    manifest["dead"].update({url: dict(entry) for url, entry in dead.items()})
    media_manifest.record_source(manifest, _DONE_ALBUM, "imgchest", media_manifest.SOURCE_DONE, count=10)
    if failed_album:
        media_manifest.record_source(manifest, _FAILED_ALBUM, "imgchest", media_manifest.SOURCE_FAILED, reason="HTTP 404")
    media_manifest.append_run(manifest, {"at": media_manifest.now_iso(), "saved": 1, "skipped": 0, "errors": 0})
    media_manifest.save_manifest(galleries / "Abbie_kzbYR2QbpncC", manifest)


@pytest.fixture
def albums_on_every_card(monkeypatch):
    """Every card links the done album, the failed one and one no run has
    seen; resolving records which were fetched."""
    monkeypatch.setattr(
        v1_media.media_discovery,
        "collect_card_text_chunks",
        lambda data: ([f"{_DONE_ALBUM} {_FAILED_ALBUM} {_UNSEEN_ALBUM}"], []),
    )
    resolved: list[str] = []

    def resolve(client_, url):
        resolved.append(url)
        return [media_extractors.GalleryImage(f"{url}/img.png", "img.png")]

    monkeypatch.setattr(media_extractors, "resolve_gallery_url", resolve)
    return resolved


def test_broken_visits_only_cards_with_broken_entries(client, populated_archive, stub_download_item, stub_discovery, albums_on_every_card):
    _broken_abbie(populated_archive["galleries"])

    body = _poll_job(client, _submit_batch(client, mode="broken"))

    assert body["cards_done"] == 1
    assert body["cards_skipped"] == 2
    assert all(c["gallery_dir"].name == "Abbie_kzbYR2QbpncC" for c in stub_download_item)


def test_broken_retries_only_dead_links_and_failed_albums(client, populated_archive, stub_download_item, stub_discovery, albums_on_every_card):
    galleries = populated_archive["galleries"]
    _broken_abbie(galleries, failed_album=True)

    _poll_job(client, _submit_batch(client, mode="broken"))

    # The done album and the never-seen one are not fetched; only the failed one.
    assert albums_on_every_card == [_FAILED_ALBUM]
    assert {c["url"] for c in stub_download_item} == {*_VESPERA_DEAD, f"{_FAILED_ALBUM}/img.png"}
    assert all(c["retry_dead"] for c in stub_download_item)
    sources = media_manifest.load_manifest(galleries / "Abbie_kzbYR2QbpncC")["sources"]
    assert sources[_FAILED_ALBUM]["st"] == media_manifest.SOURCE_DONE
    # Left for Localize all.
    assert _UNSEEN_ALBUM not in sources


def test_broken_infers_each_dead_links_prefix(client, populated_archive, stub_download_item, monkeypatch):
    embedded = "https://cdn.example.com/Abbie.png"
    lore = "https://cdn.example.com/lore.png"
    chub_child = "https://avatars.charhub.io/avatars/uploads/images/gallery/file/1/x.webp"
    mega_child = "mega://6ymghrcr/abc?k=x&n=y&s=1"
    monkeypatch.setattr(
        v1_media.media_discovery,
        "find_character_media_urls",
        lambda data: ([embedded], [lore]) if data.get("name") == "Abbie" else ([], []),
    )
    _broken_abbie(
        populated_archive["galleries"],
        dead={url: {"reason": "HTTP 404", "attempts": 1, "at": "x"} for url in (embedded, lore, chub_child, mega_child)},
    )

    _poll_job(client, _submit_batch(client, mode="broken"))

    assert {c["url"]: c["prefix"] for c in stub_download_item} == {
        embedded: "localized_media",
        lore: "lorebook_media",
        chub_child: "chubgallery",
        mega_child: "extgallery",
    }


def test_broken_never_mints_a_folder(client, populated_archive, stub_download_item, stub_discovery):
    galleries = populated_archive["galleries"]
    before = sorted(p.name for p in galleries.iterdir())

    body = _poll_job(client, _submit_batch(client, mode="broken"))

    assert body["cards_skipped"] == 3
    assert stub_download_item == []
    assert sorted(p.name for p in galleries.iterdir()) == before


def test_everything_re_resolves_every_album_and_retries_every_dead_link(client, populated_archive, stub_download_item, stub_discovery, albums_on_every_card):
    _broken_abbie(populated_archive["galleries"], failed_album=True)

    _poll_job(client, _submit_batch(client, mode="everything"))

    abbie = {c["url"]: c["retry_dead"] for c in stub_download_item if c["gallery_dir"].name == "Abbie_kzbYR2QbpncC"}
    # Dead links the card's text no longer names are retried too.
    assert set(_VESPERA_DEAD) <= set(abbie)
    assert all(abbie.values())
    assert set(albums_on_every_card) == {_DONE_ALBUM, _FAILED_ALBUM, _UNSEEN_ALBUM}


def test_pending_never_retries_dead_links(client, populated_archive, stub_download_item, stub_discovery, albums_on_every_card):
    _broken_abbie(populated_archive["galleries"])

    _poll_job(client, _submit_batch(client))

    assert not any(c["retry_dead"] for c in stub_download_item)
    assert not set(_VESPERA_DEAD) & {c["url"] for c in stub_download_item}


def test_batch_creates_no_gallery_folder_for_a_card_with_no_media(client, populated_archive, stub_download_item, monkeypatch):
    """Trap 1: `_shared.gallery_dir_for_card` mints a gallery_id, rewrites the
    PNG and mkdirs. Calling it per card would leave an empty folder behind for
    every card in the archive, so the planner must resolve it only *after*
    discovery finds something."""
    monkeypatch.setattr(v1_media.media_discovery, "find_character_media_urls", lambda data: ([], []))
    galleries = populated_archive["galleries"]
    before = {p.name for p in galleries.iterdir()}

    body = _poll_job(client, _submit_batch(client))

    assert body["cards_skipped"] == 3
    assert body["cards_done"] == 0
    assert {p.name for p in galleries.iterdir()} == before
    assert stub_download_item == []


def test_batch_saves_the_dead_ledger_once_per_batch_not_once_per_card(
    client, populated_archive, stub_download_item, stub_discovery, monkeypatch
):
    """Trap 3: the dead ledger is one global file. Saving it per card would
    mean thousands of atomic rewrites of the same few thousand entries."""
    from aspect.media import jobs as media_jobs_module

    saves = []
    real_save = media_jobs_module.media_manifest.save_dead_ledger
    monkeypatch.setattr(
        media_jobs_module.media_manifest,
        "save_dead_ledger",
        lambda ledger: (saves.append(1), real_save(ledger))[1],
    )

    _poll_job(client, _submit_batch(client))

    # Three cards, but the ledger is owned by the batch: one save at the end.
    assert len(saves) == 1


def test_batch_cancel_stops_on_a_card_boundary(client, populated_archive, stub_discovery, monkeypatch):
    """Cancel is cooperative and checked between cards, so every card that did
    run keeps its manifest."""
    started: list[str] = []

    async def fake(client_, gallery_dir, folder_name, *, url, filename_hint, prefix, index, index_state, manifest, ledger, thumbnail_store, fetch_gate=None, retry_dead=False):
        started.append(url)
        outcome = media_writer.DownloadOutcome("saved", url, file=f"localized_media_{index}_x.webp", bytes=1)
        media_manifest.record_saved(manifest, url, outcome.file, "deadbeef")
        return outcome

    monkeypatch.setattr(v1_media.media_writer, "download_item", fake)

    job_id = _submit_batch(client)
    client.post(f"/api/v1/media/jobs/{job_id}/cancel")
    body = _poll_job(client, job_id)

    assert body["state"] in ("cancelled", "done")
    if body["state"] == "cancelled":
        assert body["cards_done"] + body["cards_skipped"] < body["cards_total"]


def test_scope_card_without_card_id_is_422(client, populated_archive):
    resp = client.post("/api/v1/media/jobs", json={"items": []})
    assert resp.status_code == 422


def test_single_card_job_still_counts_items_not_cards(client, populated_archive, stub_download_item):
    """The single-card path is unchanged by the batch work: it still reports
    `unit: items` and leaves the batch counters at zero."""
    resp = client.post(
        "/api/v1/media/jobs",
        json={"card_id": "Bella_11112222.png", "items": [{"url": "https://cdn.example.com/a.png"}]},
    )
    body = _poll_job(client, resp.json()["job_id"])

    assert body["scope"] == "card"
    assert body["unit"] == "items"
    assert body["total"] == 1
    assert body["done"] == 1
    assert body["cards_total"] == 0


def test_discover_resolves_external_album_pages_into_the_job(client, populated_archive, stub_download_item, monkeypatch):
    """Stage 6B C3: the extGallery phase. A card linking a catbox album should
    download the album's files, not the album page."""
    from aspect.media import extractors as media_extractors

    monkeypatch.setattr(
        v1_media.media_discovery,
        "collect_card_text_chunks",
        lambda data: (["gallery: https://catbox.moe/c/abc123"], []),
    )
    monkeypatch.setattr(
        v1_media.media_discovery, "find_character_media_urls", lambda data: ([], [])
    )
    monkeypatch.setattr(
        media_extractors,
        "resolve_gallery_url",
        lambda client_, url: [
            media_extractors.GalleryImage("https://files.catbox.moe/a.png", "a.png"),
            media_extractors.GalleryImage("https://files.catbox.moe/b.png", "b.png"),
        ],
    )

    resp = client.post(
        "/api/v1/media/jobs",
        json={"card_id": "Bella_11112222.png", "discover": True},
    )
    body = _poll_job(client, resp.json()["job_id"])

    assert body["state"] == "done"
    assert {call["url"] for call in stub_download_item} == {
        "https://files.catbox.moe/a.png",
        "https://files.catbox.moe/b.png",
    }


def test_discover_resolves_a_chub_sourced_cards_own_gallery(client, populated_archive, stub_download_item, monkeypatch):
    """Stage 6B: the `chub` extractor. Triggered by the card's own
    `extensions.chub.id`, not a link in its text -- unlike extGallery, this
    should still fire even when the card's own prose has no gallery URL."""
    from aspect.media import extractors as media_extractors

    monkeypatch.setattr(v1_media.media_discovery, "find_character_media_urls", lambda data: ([], []))
    monkeypatch.setattr(v1_media.media_discovery, "collect_card_text_chunks", lambda data: ([], []))
    monkeypatch.setattr(v1_media.chub_source, "card_id", lambda data: "9999")
    seen_ids = []
    monkeypatch.setattr(
        media_extractors,
        "resolve_chub_gallery",
        lambda client_, project_id: (
            seen_ids.append(project_id)
            or [media_extractors.GalleryImage("https://cdn.chub.ai/a.webp", "a.webp")]
        ),
    )

    resp = client.post(
        "/api/v1/media/jobs",
        json={"card_id": "Bella_11112222.png", "discover": True},
    )
    body = _poll_job(client, resp.json()["job_id"])

    assert body["state"] == "done"
    assert seen_ids == ["9999"]
    assert {call["url"] for call in stub_download_item} == {"https://cdn.chub.ai/a.webp"}


# --- dedupe -------------------------------------------------------------


def test_dedupe_trashes_a_stale_redownload_and_leaves_live_files_alone(
    client, populated_archive, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "trash_dir", tmp_path / "trash")
    gallery = populated_archive["galleries"] / "Abbie_kzbYR2QbpncC"
    live = gallery / "two.jpg"
    stale = gallery / "two_stale_copy.jpg"
    stale.write_bytes(live.read_bytes())
    stale_size = stale.stat().st_size

    manifest = media_manifest.load_manifest(gallery)
    media_manifest.record_saved(
        manifest, "https://cdn.example.com/two.jpg", live.name, hashlib.sha256(live.read_bytes()).hexdigest()
    )
    media_manifest.save_manifest(gallery, manifest)

    resp = client.post("/api/v1/media/dedupe")

    assert resp.status_code == 200
    body = resp.json()
    assert body["files_trashed"] == 1
    assert body["folders_touched"] == 1
    assert body["bytes_freed"] == stale_size
    # one.jpg is also untracked, but not a byte-for-byte match of anything the
    # manifest claims -- it's left alone rather than trashed, same as a
    # manually added image would be.
    assert body["unresolved"] == 1
    assert not stale.exists()
    assert live.exists()
    assert (gallery / "one.jpg").exists()


# --- the add-time download and the detail view's media map --------------------


def _add_card(archive_dirs, client, filename: str, **fields) -> None:
    from tests.conftest import card_png, jai_extensions

    fragment = filename.rsplit("_", 1)[1].removesuffix(".png")
    (archive_dirs["characters"] / filename).write_bytes(
        card_png(
            filename.split("_")[0],
            extensions=jai_extensions(f"{fragment}-0000-0000-0000-000000000000", gallery_id="EEEEEEEEEEEE"),
            **fields,
        )
    )
    client.post("/api/v1/refresh")


@pytest.mark.real_media_queue
def test_a_new_card_queues_its_direct_images_but_never_a_folder_link(client, archive_dirs, stub_download_item):
    _add_card(
        archive_dirs,
        client,
        "Added_99990000.png",
        creator_notes="![me](https://cdn.example.com/notes.png)\n\nmore at https://mega.nz/folder/abc#key",
        character_book={"entries": [{"keys": ["a"], "content": "https://cdn.example.com/lore.png"}]},
    )

    v1_media.queue_direct_images("Added_99990000.png")

    jobs = client.get("/api/v1/media/jobs", params={"card_id": "Added_99990000.png"}).json()
    assert len(jobs) == 1
    assert _poll_job(client, jobs[0]["job_id"])["state"] == "done"
    assert {(c["url"], c["prefix"]) for c in stub_download_item} == {
        ("https://cdn.example.com/notes.png", "localized_media"),
        ("https://cdn.example.com/lore.png", "lorebook_media"),
    }


@pytest.mark.real_media_queue
def test_a_new_card_with_no_images_queues_nothing_and_makes_no_folder(client, archive_dirs):
    _add_card(archive_dirs, client, "Plain_99991111.png", creator_notes="just words")

    v1_media.queue_direct_images("Plain_99991111.png")

    assert client.get("/api/v1/media/jobs", params={"card_id": "Plain_99991111.png"}).json() == []
    assert not list(archive_dirs["galleries"].glob("Plain_*"))


def test_detail_maps_each_downloaded_url_to_its_gallery_file(client, archive_dirs):
    _add_card(archive_dirs, client, "Mapped_99992222.png")
    folder = archive_dirs["galleries"] / "Mapped_EEEEEEEEEEEE"
    folder.mkdir()
    (folder / "localized_media_1_a.webp").write_bytes(b"x")
    manifest = media_manifest.empty_manifest()
    media_manifest.record_saved(manifest, "https://cdn.example.com/a.webp?x=1&amp;y=2", "localized_media_1_a.webp", "h")
    # Binned since: the manifest keeps the entry, but there is no file to show.
    media_manifest.record_saved(manifest, "https://cdn.example.com/binned.png", "localized_media_2_b.png", "h")
    media_manifest.record_dead(manifest, "https://cdn.example.com/gone.png", "404")
    media_manifest.save_manifest(folder, manifest)

    media = client.get("/api/v1/characters/Mapped_99992222.png").json()["media"]

    file_url = "/api/v1/galleries/Mapped_EEEEEEEEEEEE/files/localized_media_1_a.webp"
    assert media == {
        "https://cdn.example.com/a.webp?x=1&amp;y=2": file_url,
        # The renderer looks up the decoded `src` an <img> attribute yields.
        "https://cdn.example.com/a.webp?x=1&y=2": file_url,
        "https://cdn.example.com/gone.png": None,
    }
