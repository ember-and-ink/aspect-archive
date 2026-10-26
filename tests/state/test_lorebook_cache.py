from aspect.state.lorebook_cache import LorebookCache


def _cache(tmp_path) -> LorebookCache:
    return LorebookCache(cache_dir=tmp_path / ".lorecache")


def test_put_then_get_roundtrips_the_raw_blob(tmp_path):
    cache = _cache(tmp_path)
    blob = {"id": "abc", "list": {"chapters": [1, 2]}, "chapters": [{"index": 0}]}
    cache.put("saucepan", "abc", blob)

    assert cache.has("saucepan", "abc")
    assert cache.get("saucepan", "abc") == blob


def test_get_missing_returns_none(tmp_path):
    assert _cache(tmp_path).get("saucepan", "nope") is None


def test_split_partitions_preserving_order_and_deduping(tmp_path):
    cache = _cache(tmp_path)
    cache.put("saucepan", "a", {"id": "a"})
    cache.put("saucepan", "c", {"id": "c"})

    cached, missing = cache.split("saucepan", ["a", "b", "c", "d", "b", "", "a"])

    assert cached == ["a", "c"]
    assert missing == ["b", "d"]


def test_source_namespaces_the_id_space(tmp_path):
    cache = _cache(tmp_path)
    cache.put("saucepan", "shared-id", {"id": "shared-id"})

    assert cache.has("saucepan", "shared-id")
    assert not cache.has("janitor", "shared-id")
    assert cache.get("janitor", "shared-id") is None


def test_ids_with_filesystem_unsafe_chars_do_not_collide(tmp_path):
    # Slugging must not fold two distinct ids onto one file.
    cache = _cache(tmp_path)
    cache.put("saucepan", "a/b", {"which": "slash"})
    cache.put("saucepan", "a:b", {"which": "colon"})

    # Different ids -> different files -> no clobber.
    assert cache.count == 2


def test_put_empty_id_is_a_noop(tmp_path):
    cache = _cache(tmp_path)
    cache.put("saucepan", "", {"id": ""})
    assert cache.count == 0


def test_clear_removes_all_and_reports_count(tmp_path):
    cache = _cache(tmp_path)
    cache.put("saucepan", "a", {"id": "a"})
    cache.put("saucepan", "b", {"id": "b"})
    assert cache.count == 2

    assert cache.clear() == 2
    assert cache.count == 0
    assert not cache.has("saucepan", "a")
