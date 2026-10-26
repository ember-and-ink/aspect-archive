"""aspect/media/manifest.py: per-gallery `.media.json` and the global
cross-character dead-URL ledger (docs/PHASE_3C_PLAN.md §3), the state a media
download run reads before starting and writes when it finishes."""

from __future__ import annotations

from pathlib import Path

import pytest

from aspect.media import manifest as media_manifest


@pytest.fixture
def gallery_dir(tmp_path: Path) -> Path:
    d = tmp_path / "SomeChar_ab12cd34ef56"
    d.mkdir()
    return d


def test_load_manifest_missing_file_returns_empty_skeleton(gallery_dir: Path):
    manifest = media_manifest.load_manifest(gallery_dir)
    assert manifest == {"version": 1, "updated": None, "files": {}, "dead": {}, "runs": []}


def test_load_manifest_corrupt_json_returns_empty_skeleton(gallery_dir: Path):
    (gallery_dir / media_manifest.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    manifest = media_manifest.load_manifest(gallery_dir)
    assert manifest["files"] == {}
    assert manifest["dead"] == {}


def test_save_then_load_round_trips(gallery_dir: Path):
    manifest = media_manifest.load_manifest(gallery_dir)
    media_manifest.record_saved(manifest, "https://x/img.png", "localized_media_1_img.webp", "deadbeef")
    media_manifest.record_dead(manifest, "https://x/gone.png", "HTTP 404")
    media_manifest.append_run(manifest, {"at": "now", "phase": "embedded", "saved": 1, "skipped": 1, "errors": 0})
    media_manifest.save_manifest(gallery_dir, manifest)

    reloaded = media_manifest.load_manifest(gallery_dir)
    assert reloaded["files"]["https://x/img.png"]["file"] == "localized_media_1_img.webp"
    assert reloaded["dead"]["https://x/gone.png"]["reason"] == "HTTP 404"
    assert reloaded["runs"][0]["saved"] == 1
    assert reloaded["updated"] is not None


def test_record_saved_clears_a_prior_dead_entry(gallery_dir: Path):
    manifest = media_manifest.load_manifest(gallery_dir)
    media_manifest.record_dead(manifest, "https://x/flaky.png", "timeout")
    assert "https://x/flaky.png" in manifest["dead"]
    media_manifest.record_saved(manifest, "https://x/flaky.png", "localized_media_1_flaky.webp", "abc123")
    assert "https://x/flaky.png" not in manifest["dead"]


def test_append_run_bounds_history(gallery_dir: Path):
    manifest = media_manifest.load_manifest(gallery_dir)
    for i in range(250):
        media_manifest.append_run(manifest, {"at": str(i), "phase": "embedded", "saved": 0, "skipped": 0, "errors": 0})
    assert len(manifest["runs"]) == 200
    assert manifest["runs"][0]["at"] == "50"
    assert manifest["runs"][-1]["at"] == "249"


def test_manifest_write_is_atomic_no_partial_file_left(gallery_dir: Path):
    manifest = media_manifest.load_manifest(gallery_dir)
    media_manifest.save_manifest(gallery_dir, manifest)
    leftovers = list(gallery_dir.glob(".*.tmp"))
    assert leftovers == []
    assert (gallery_dir / media_manifest.MANIFEST_NAME).is_file()


# --------------------------------------------------------------------------
# Global dead-URL ledger
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_ledger_file(tmp_path, monkeypatch):
    monkeypatch.setattr(media_manifest.settings, "dead_urls_file", tmp_path / "dead_urls.json")


def test_load_dead_ledger_missing_file_is_empty():
    assert media_manifest.load_dead_ledger() == {}


def test_record_failure_then_is_dead_after_max_transient_attempts():
    ledger: dict = {}
    for _ in range(media_manifest.MAX_TRANSIENT_ATTEMPTS - 1):
        result = media_manifest.record_failure(ledger, "https://x/flaky.png", permanent=False, status=None, message="timeout")
        assert result["dead"] is False
    result = media_manifest.record_failure(ledger, "https://x/flaky.png", permanent=False, status=None, message="timeout")
    assert result["dead"] is True
    assert result["permanent"] is False
    assert media_manifest.is_dead(ledger, "https://x/flaky.png") is True


def test_record_failure_permanent_is_dead_on_first_attempt():
    ledger: dict = {}
    result = media_manifest.record_failure(ledger, "https://x/gone.png", permanent=True, status=404, message="HTTP 404")
    assert result == {"dead": True, "permanent": True, "attempts": 1}
    assert media_manifest.dead_reason(ledger, "https://x/gone.png") == "HTTP 404"


def test_record_success_clears_the_entry():
    ledger: dict = {}
    media_manifest.record_failure(ledger, "https://x/flaky.png", permanent=False, status=None, message="timeout")
    media_manifest.record_success(ledger, "https://x/flaky.png")
    assert media_manifest.is_dead(ledger, "https://x/flaky.png") is False
    assert "https://x/flaky.png" not in ledger


def test_is_dead_false_for_unknown_url():
    assert media_manifest.is_dead({}, "https://x/never-seen.png") is False


def test_dead_reason_transient_mentions_attempt_count():
    ledger: dict = {}
    media_manifest.record_failure(ledger, "https://x/flaky.png", permanent=False, status=None, message="timeout")
    reason = media_manifest.dead_reason(ledger, "https://x/flaky.png")
    assert "failed 1 times" in reason
    assert "timeout" in reason


def test_ledger_eviction_drops_least_recently_touched():
    ledger: dict = {}
    for i in range(media_manifest.MAX_LEDGER_ENTRIES + 10):
        ledger[f"https://x/{i}.png"] = {"n": 1, "f": i, "l": i}
    media_manifest._evict_if_needed(ledger)
    assert len(ledger) == media_manifest.MAX_LEDGER_ENTRIES
    # The oldest (lowest `l`) ten are gone; the newest survive.
    assert "https://x/0.png" not in ledger
    assert f"https://x/{media_manifest.MAX_LEDGER_ENTRIES + 9}.png" in ledger


def test_save_and_load_dead_ledger_round_trips():
    ledger: dict = {}
    media_manifest.record_failure(ledger, "https://x/gone.png", permanent=True, status=404, message="HTTP 404")
    media_manifest.save_dead_ledger(ledger)
    reloaded = media_manifest.load_dead_ledger()
    assert reloaded["https://x/gone.png"]["p"] == 1
    assert reloaded["https://x/gone.png"]["s"] == 404
