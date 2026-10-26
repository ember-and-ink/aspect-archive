"""Discover's first-seen stamps: the store, and the API over it.

A stamp is never rewritten (see `aspect/state/seen.py`), so the failures that
matter are the ones that would make an old card "new" again: a stamp moved
forward on a second sighting, a bucket overwritten instead of merged, a damaged
file read as "nothing seen yet".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aspect.state import seen
from aspect.state.seen import SeenError, SeenStore


@pytest.fixture
def store(tmp_path: Path) -> SeenStore:
    return SeenStore(tmp_path / "discover_seen.json")


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


def test_missing_file_reads_as_empty(store: SeenStore) -> None:
    assert store.read() == {}


def test_record_stamps_new_ids_with_now(store: SeenStore) -> None:
    added, first = store.record("chub", ["412233", "891020"], now=1000)
    assert added == 2
    assert first == {"412233": 1000, "891020": 1000}
    assert store.read() == {"chub": {"412233": 1000, "891020": 1000}}


def test_a_second_sighting_keeps_the_first_stamp(store: SeenStore) -> None:
    """The whole feature: seeing a card again must not make it new again."""
    store.record("chub", ["a"], now=1000)
    added, first = store.record("chub", ["a", "b"], now=9000)
    assert added == 1
    assert first == {"a": 1000, "b": 9000}


def test_answers_only_the_ids_asked_about(store: SeenStore) -> None:
    store.record("chub", ["a", "b"], now=1000)
    _, first = store.record("chub", ["b"], now=2000)
    assert first == {"b": 1000}


def test_nothing_new_writes_nothing(store: SeenStore) -> None:
    store.record("chub", ["a"], now=1000)
    before = store.path.stat().st_mtime_ns
    added, _ = store.record("chub", ["a"], now=2000)
    assert added == 0
    assert store.path.stat().st_mtime_ns == before


def test_record_leaves_the_other_providers_alone(store: SeenStore) -> None:
    store.record("chub", ["a"], now=1000)
    store.record("datacat", ["a"], now=2000)
    assert store.read() == {"chub": {"a": 1000}, "datacat": {"a": 2000}}


def test_blanks_and_duplicates_are_dropped(store: SeenStore) -> None:
    added, first = store.record("chub", [" a ", "", "a", "b"], now=1)
    assert added == 2
    assert first == {"a": 1, "b": 1}


def test_an_overlong_id_is_refused_and_writes_nothing(store: SeenStore) -> None:
    with pytest.raises(SeenError):
        store.record("chub", ["ok", "x" * 500], now=1)
    assert store.read() == {}


def test_the_per_provider_ceiling_is_enforced(
    store: SeenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seen, "MAX_IDS_PER_PROVIDER", 2)
    store.record("chub", ["a", "b"], now=1)
    with pytest.raises(SeenError):
        store.record("chub", ["c"], now=2)
    assert store.read() == {"chub": {"a": 1, "b": 1}}


def test_damaged_json_raises_rather_than_reading_as_empty(store: SeenStore) -> None:
    """`{}` here would make every card "new", and the next write would replace
    the whole history with today's stamp."""
    store.path.write_text("{not json")
    with pytest.raises(SeenError):
        store.read()
    with pytest.raises(SeenError):
        store.record("chub", ["a"])
    assert store.path.read_text() == "{not json"


def test_junk_rows_are_dropped_rather_than_raising(store: SeenStore) -> None:
    store.path.write_text(
        json.dumps({"chub": {"a": 5, "b": "soon", "c": True, "": 1}, "datacat": ["x"]})
    )
    assert store.read() == {"chub": {"a": 5}}


def test_a_hand_edit_is_picked_up_without_a_restart(store: SeenStore) -> None:
    store.record("chub", ["a"], now=1)
    store.path.write_text("{}")
    added, first = store.record("chub", ["a"], now=7)
    assert added == 1
    assert first == {"a": 7}


def test_a_failed_write_leaves_the_previous_stamps_intact(
    store: SeenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.record("chub", ["a"], now=1)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(seen.os, "replace", boom)
    with pytest.raises(SeenError):
        store.record("chub", ["b"], now=2)
    assert store.read() == {"chub": {"a": 1}}
    assert [p.name for p in store.path.parent.iterdir()] == [store.path.name]


# --------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------


def test_post_stamps_and_answers(client, archive_dirs) -> None:
    resp = client.post("/api/v1/discover/seen", json={"provider": "chub", "ids": ["a", "b"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "chub"
    assert body["added"] == 2
    assert set(body["first_seen"]) == {"a", "b"}
    assert body["first_seen"]["a"] == body["first_seen"]["b"] > 0
    assert archive_dirs["seen"].is_file()


def test_post_keeps_the_original_stamp_across_requests(client, archive_dirs) -> None:
    first = client.post("/api/v1/discover/seen", json={"provider": "chub", "ids": ["a"]}).json()
    again = client.post("/api/v1/discover/seen", json={"provider": "chub", "ids": ["a"]}).json()
    assert again["added"] == 0
    assert again["first_seen"]["a"] == first["first_seen"]["a"]


def test_an_unknown_provider_is_refused(client, archive_dirs) -> None:
    resp = client.post("/api/v1/discover/seen", json={"provider": "janitor", "ids": ["a"]})
    assert resp.status_code == 422
    assert not archive_dirs["seen"].exists()


def test_too_many_ids_in_one_request_is_refused(client, archive_dirs, monkeypatch) -> None:
    from aspect.api.v1 import discover

    monkeypatch.setattr(discover, "MAX_IDS_PER_REQUEST", 3)
    resp = client.post(
        "/api/v1/discover/seen", json={"provider": "chub", "ids": ["a", "b", "c", "d"]}
    )
    assert resp.status_code == 422
    assert not archive_dirs["seen"].exists()


def test_a_damaged_file_is_a_500_not_everything_new(client, archive_dirs) -> None:
    archive_dirs["seen"].write_text("{not json")
    resp = client.post("/api/v1/discover/seen", json={"provider": "chub", "ids": ["a"]})
    assert resp.status_code == 500
    assert archive_dirs["seen"].read_text() == "{not json"
