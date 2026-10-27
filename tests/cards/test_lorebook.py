import json
from pathlib import Path

from aspect.cards.lorebook import LorebookMapper
from aspect.cards.models import LoreEntry

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# map() -- real /hampter/script capture: the full, real 20-entry "Kamii
# University: A Living Campus" script (Akane Kujo's lorebook). See
# tests/fixtures/README.md for provenance.
# ---------------------------------------------------------------------------


def test_map_real_kamii_university_script():
    raw = _load_json("hampter_script_kamii_university.json")

    book, warnings = LorebookMapper().map([raw], character_name="Akane Kujo")

    assert warnings == []
    assert book is not None
    assert book.name == "Kamii University: A Living Campus"
    assert book.description.startswith("Some places want you to stay.")
    assert book.scan_depth == 3  # from the settings JSON string, not top-level depth
    assert len(book.entries) == 20

    entry = book.entries[0]
    assert entry.keys == ["kamii university", "the university", "campus", "the campus", "kamii"]
    assert entry.secondary_keys == []
    # real entry's `comment` field is blank; mapLoreEntry falls back to
    # `name` (single book, so no title prefix).
    assert entry.comment == "Location - Kamii University"
    assert entry.name == "Location - Kamii University"
    assert entry.content.startswith("Kamii University: The Living Campus")
    assert "{{user}}" in entry.content  # already-canonical macro, untouched
    assert entry.constant is False
    assert entry.selective is False
    assert entry.insertion_order == 100
    assert entry.enabled is True
    assert entry.case_sensitive is False
    assert entry.extensions["probability"] == 85
    assert entry.extensions["useProbability"] is True
    assert entry.extensions["match_whole_words"] is True
    assert entry.extensions["group_weight"] == 100
    assert entry.extensions["jai"]["id"] == "location-kamiiuniversity-entry-001"
    assert entry.extensions["jai"]["category"] == "location"
    assert entry.extensions["jai"]["tags"] == ["location", "territory", "campus"]
    assert entry.extensions["jai"]["priority"] == 1

    assert book.extensions["jai_sources"] == [
        {
            "id": "9e345de7-1e25-4f1b-8aec-6ea0b10b8b6b",
            "title": "Kamii University: A Living Campus",
            "depth": 3,
            "entry_count": 20,
        }
    ]


def test_map_real_script_matches_sillytavern_reexport_field_for_field():
    """The strongest real-data validation available: akane_kujo_st_lorebook.json's
    `originalData.entries[0:20]` is SillyTavern's preserved copy of the exact
    V3 character_book a prior (compatible) tool produced for this same real
    20-entry script -- ground-truth OUTPUT, not just input. Comparing against
    it (field-for-field, ignoring `comment` which differs only by a
    multi-book prefix present in that original two-book merge but not in
    this single-book mapping) is a real end-to-end fidelity check on
    LorebookMapper's port, not an assumption about correctness."""
    raw = _load_json("hampter_script_kamii_university.json")
    st_export = _load_json("akane_kujo_st_lorebook.json")
    expected_entries = st_export["originalData"]["entries"][:20]

    book, warnings = LorebookMapper().map([raw])

    assert warnings == []
    assert len(book.entries) == len(expected_entries) == 20
    for i, (mine, expected) in enumerate(zip(book.entries, expected_entries)):
        mine_dict = mine.model_dump()
        expected_dict = dict(expected)
        assert expected_dict.pop("comment") == f"[Kamii University: A Living Campus] {mine_dict.pop('comment')}"
        assert mine_dict == expected_dict, f"entry {i} ({expected.get('name')!r}) field mismatch"


def test_map_content_runs_through_macro_sanitizer():
    raw = _load_json("hampter_script_kamii_university.json")
    entries = json.loads(raw["script"])
    entries[0]["content"] = "hi {obj}, love {{poss}} and {{unknownmacro}}"
    raw["script"] = json.dumps(entries)

    book, warnings = LorebookMapper().map([raw])

    assert book.entries[0].content == "hi {{user}}, love {{user}} and {{unknownmacro}}"
    assert any("unknownmacro" in w for w in warnings)


# ---------------------------------------------------------------------------
# map() -- structural edge cases (synthetic, supplementing the one real
# fixture -- see feedback memory on preferring real data as the default).
# ---------------------------------------------------------------------------


def test_map_returns_none_when_no_lorebook_type_scripts():
    raw_scripts = [{"type": "engine", "title": "Not a lorebook", "script": "console.log(1)"}]
    book, warnings = LorebookMapper().map(raw_scripts)
    assert book is None
    assert warnings == []


def test_map_returns_none_and_warns_when_script_field_is_not_json():
    raw_scripts = [{"type": "lorebook", "id": "abc", "title": "Broken", "script": "not json {{{"}]
    book, warnings = LorebookMapper().map(raw_scripts)
    assert book is None
    assert len(warnings) == 1
    assert "Broken" in warnings[0]
    assert "not JSON" in warnings[0]


def test_map_returns_none_and_warns_when_entries_dont_look_lore_shaped():
    # `script` parses as JSON but its items aren't entry-shaped -- e.g. a
    # true JanitorAI "engine" script whose `type` still happens to say
    # "lorebook" (JanitorAI does not guarantee type accuracy per the
    # existing userscript comment).
    raw_scripts = [
        {
            "type": "lorebook",
            "id": "def",
            "title": "Actually Code",
            "script": json.dumps([{"foo": "bar"}, {"baz": 1}]),
        }
    ]
    book, warnings = LorebookMapper().map(raw_scripts)
    assert book is None
    assert len(warnings) == 1
    assert "not lorebook-shaped" in warnings[0]


def test_map_skips_bad_script_but_keeps_good_one():
    good = _load_json("hampter_script_kamii_university.json")
    bad = {"type": "lorebook", "id": "bad-1", "title": "Broken", "script": "not json"}

    book, warnings = LorebookMapper().map([bad, good])

    assert book is not None
    assert len(book.entries) == 20
    assert len(warnings) == 1
    assert "Broken" in warnings[0]


def test_map_multi_book_prefixes_comment_with_title_and_unions_depth():
    # Small synthetic books here (rather than the full 20-entry real
    # fixture) -- this test is purely about structural multi-book behavior
    # (naming/prefixing/depth-union), already covered field-for-field with
    # real data in test_map_real_script_matches_sillytavern_reexport_field_for_field.
    first = {
        "type": "lorebook",
        "id": "first-book-id",
        "title": "Kamii University: A Living Campus",
        "description": "Some places want you to stay.",
        "settings": '{"depth":3}',
        "script": json.dumps([{"key": ["kamii"], "content": "kamii content", "constant": False}]),
    }
    second_entries = [
        {
            "key": ["second book key"],
            "content": "second book content",
            "constant": False,
        }
    ]
    second = {
        "type": "lorebook",
        "id": "second-book-id",
        "title": "Second Book",
        "description": "Second description",
        "depth": 7,
        "script": json.dumps(second_entries),
    }

    book, warnings = LorebookMapper().map([first, second], character_name="Akane Kujo")

    assert warnings == []
    assert book.name == "Akane Kujo — JanitorAI lorebooks"
    assert "Some places want you to stay." in book.description
    assert "Second description" in book.description
    assert book.scan_depth == 7  # max(3, 7)
    assert len(book.entries) == 2
    assert book.entries[0].comment == "[Kamii University: A Living Campus] "
    assert book.entries[1].comment == "[Second Book] "
    # ids/display_index are a single running counter across all books.
    assert book.entries[0].id == 0
    assert book.entries[1].id == 1


def test_map_falls_back_to_character_name_when_no_title():
    raw_scripts = [
        {
            "type": "lorebook",
            "id": "x",
            "title": "",
            "script": json.dumps([{"key": ["k"], "content": "c", "constant": False}]),
        }
    ]
    book, _ = LorebookMapper().map(raw_scripts, character_name="Some Character")
    assert book.name == "Some Character lorebook"


def test_map_entry_matchwholewords_absent_maps_to_none():
    raw_scripts = [
        {
            "type": "lorebook",
            "id": "x",
            "title": "T",
            "script": json.dumps([{"key": ["k"], "content": "c", "constant": False}]),
        }
    ]
    book, _ = LorebookMapper().map(raw_scripts)
    assert book.entries[0].extensions["match_whole_words"] is None


def test_map_selective_true_when_secondary_keys_present():
    raw_scripts = [
        {
            "type": "lorebook",
            "id": "x",
            "title": "T",
            "script": json.dumps(
                [{"key": ["k"], "keysecondary": ["sk"], "content": "c", "constant": False}]
            ),
        }
    ]
    book, _ = LorebookMapper().map(raw_scripts)
    assert book.entries[0].selective is True
    assert book.entries[0].secondary_keys == ["sk"]


# ---------------------------------------------------------------------------
# merge() -- union + dedupe of a book's entries with an "accumulated" list.
# CaptureStore never actually populates a non-empty accumulated list as of
# M5 (see capture_store.py's comment), but merge() itself is real, tested
# logic ready for whenever a real accumulation source exists.
# ---------------------------------------------------------------------------


def test_merge_returns_book_unchanged_when_accumulated_is_empty():
    raw = _load_json("hampter_script_kamii_university.json")
    book, _ = LorebookMapper().map([raw])
    merged = LorebookMapper().merge(book, [])
    assert merged is book


def test_merge_returns_none_when_both_are_empty():
    assert LorebookMapper().merge(None, []) is None


def test_merge_unions_new_entries_into_existing_book():
    raw = _load_json("hampter_script_kamii_university.json")
    book, _ = LorebookMapper().map([raw])
    extra = LoreEntry(keys=["extra key"], content="extra content")

    merged = LorebookMapper().merge(book, [extra])

    assert len(merged.entries) == len(book.entries) + 1
    assert merged.entries[-1] is extra


def test_merge_dedupes_by_keys_and_content():
    raw = _load_json("hampter_script_kamii_university.json")
    book, _ = LorebookMapper().map([raw])
    original_count = len(book.entries)
    duplicate = LoreEntry(keys=book.entries[0].keys, content=book.entries[0].content)

    merged = LorebookMapper().merge(book, [duplicate])

    assert len(merged.entries) == original_count


def test_merge_builds_a_book_from_accumulated_entries_when_book_is_none():
    entry = LoreEntry(keys=["k"], content="c")
    merged = LorebookMapper().merge(None, [entry])
    assert merged is not None
    assert merged.entries == [entry]
