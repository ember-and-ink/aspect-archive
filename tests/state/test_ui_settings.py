"""The archive's settings blob: the store, and the API over it.

This file holds the only copy of the browser UI's provider credentials once the
old browser-storage copy is gone, so the cases that matter here are the ones
where a naive implementation loses them quietly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aspect.state import ui_settings
from aspect.state.ui_settings import SettingsError, SettingsStore


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "settings.json")


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


def test_missing_file_reads_as_empty(store: SettingsStore) -> None:
    """A fresh archive has no settings; that is a first run, not a fault."""
    assert store.read() == {}


def test_write_then_read_round_trips(store: SettingsStore) -> None:
    blob = {"chubToken": "abc123", "datacatFollowedCreators": ["a", "b"], "uiScale": 3}
    store.write(blob)
    assert store.read() == blob


def test_write_creates_the_parent_directory(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "nested" / "deeper" / "settings.json")
    store.write({"k": "v"})
    assert store.read() == {"k": "v"}


def test_write_replaces_wholesale_so_deletes_propagate(store: SettingsStore) -> None:
    """Not a merge. The frontend's boot migrations delete keys, and a merge
    endpoint could never express that."""
    store.write({"old": 1, "keep": 2})
    store.write({"keep": 2})
    assert store.read() == {"keep": 2}


def test_unicode_survives_the_round_trip(store: SettingsStore) -> None:
    # Creator names in the followed list are arbitrary user text.
    blob = {"datacatFollowedCreators": ["Ravenborn", "A Mother’s Claim", "日本"]}
    store.write(blob)
    assert store.read() == blob


def test_damaged_json_raises_rather_than_reading_as_empty(store: SettingsStore) -> None:
    """The important one.

    Returning {} for a corrupt file would look like a fresh archive: the
    frontend would fill in its defaults and save them straight over the top,
    turning a recoverable problem into a lost token.
    """
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SettingsError, match="not valid JSON"):
        store.read()


def test_a_json_array_is_not_a_settings_blob(store: SettingsStore) -> None:
    store.path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SettingsError, match="expected a JSON object"):
        store.read()


def test_writing_a_non_object_is_refused(store: SettingsStore) -> None:
    with pytest.raises(SettingsError, match="expected a JSON object"):
        store.write(["not", "a", "blob"])  # type: ignore[arg-type]


def test_unserialisable_values_are_refused_before_the_file_is_touched(
    store: SettingsStore,
) -> None:
    store.write({"good": 1})
    with pytest.raises(SettingsError, match="not JSON-serialisable"):
        store.write({"bad": {1, 2, 3}})
    # The previous contents must survive a rejected write.
    assert store.read() == {"good": 1}


def test_oversized_writes_are_refused(store: SettingsStore) -> None:
    with pytest.raises(SettingsError, match="ceiling"):
        store.write({"customCSS": "x" * (ui_settings.MAX_BYTES + 1)})


def test_write_leaves_no_temp_files_behind(store: SettingsStore) -> None:
    store.write({"a": 1})
    store.write({"b": 2})
    assert sorted(p.name for p in store.path.parent.iterdir()) == ["settings.json"]


def test_a_failed_write_does_not_destroy_the_previous_file(
    store: SettingsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomicity, from the outside: the rename is what makes the new contents
    visible, so a crash before it must leave the old ones intact."""
    store.write({"chubToken": "original"})

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ui_settings.os, "replace", boom)
    with pytest.raises(SettingsError, match="could not write"):
        store.write({"chubToken": "replacement"})

    assert store.read() == {"chubToken": "original"}
    assert sorted(p.name for p in store.path.parent.iterdir()) == ["settings.json"]


def test_stored_file_is_readable_json(store: SettingsStore) -> None:
    """Hand-editing this file is a reasonable thing to do, so it is written
    indented and key-sorted rather than as one line."""
    store.write({"b": 2, "a": 1})
    text = store.path.read_text(encoding="utf-8")
    assert json.loads(text) == {"a": 1, "b": 2}
    assert text.index('"a"') < text.index('"b"')
    assert "\n" in text


# --------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------


def test_get_settings_is_empty_on_a_fresh_archive(client, archive_dirs) -> None:
    resp = client.get("/api/v1/settings")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_put_then_get_round_trips_through_the_api(client, archive_dirs) -> None:
    blob = {"chubToken": "abc", "datacatFollowedCreators": ["x"], "gridThumbnailSize": 512}
    put = client.put("/api/v1/settings", json=blob)
    assert put.status_code == 200
    assert put.json() == blob
    assert client.get("/api/v1/settings").json() == blob
    # and it is actually on disk, not just in memory
    assert json.loads(archive_dirs["settings"].read_text(encoding="utf-8")) == blob


def test_put_replaces_rather_than_merges(client, archive_dirs) -> None:
    client.put("/api/v1/settings", json={"a": 1, "b": 2})
    client.put("/api/v1/settings", json={"a": 9})
    assert client.get("/api/v1/settings").json() == {"a": 9}


def test_put_refuses_a_json_array(client, archive_dirs) -> None:
    assert client.put("/api/v1/settings", json=[1, 2]).status_code == 422


def test_get_reports_damaged_settings_instead_of_masking_them(
    client, archive_dirs
) -> None:
    """A 500 so the frontend keeps whatever it has and the user is told. An
    empty 200 would read as "fresh archive" and get saved over the damage."""
    archive_dirs["settings"].write_text("{broken", encoding="utf-8")
    resp = client.get("/api/v1/settings")
    assert resp.status_code == 500
    assert "not valid JSON" in resp.json()["detail"]


def test_settings_survive_a_hand_edit_between_requests(client, archive_dirs) -> None:
    """Reads are not cached, so editing the file by hand takes effect without a
    restart -- the whole point of it being a plain JSON file."""
    client.put("/api/v1/settings", json={"uiScale": 1})
    archive_dirs["settings"].write_text('{"uiScale": 9}', encoding="utf-8")
    assert client.get("/api/v1/settings").json() == {"uiScale": 9}
