"""`proxy.cards.lorebook_sync.rebuild` -- rewriting one card's book."""

from __future__ import annotations

from proxy.cards import lorebook_split, lorebook_sync


def _entry(title: str, content: str, **extra) -> dict:
    return {"comment": title, "content": content, "keys": [title.lower()], **extra}


def _rebuild(book, **changes):
    return lorebook_sync.rebuild(book, lorebook_split.segments(book), **changes)


def test_a_replacement_takes_the_slot_and_brings_the_rest_of_the_version():
    old = _entry("Town", "old", id=4, insertion_order=10, extensions={"display_index": 4, "probability": 100})
    river = _entry("River", "a river", id=5)
    book = {"name": "World", "entries": [old, river], "extensions": {"keep": True}}
    new = _entry("The Town", "new", id=99, insertion_order=50, extensions={"display_index": 0, "probability": 40})

    rebuilt, outcome = _rebuild(book, replace={0: {0: new}})
    assert (outcome.upgraded, outcome.changed) == (1, True)
    assert rebuilt["entries"][0] == {
        **new,
        "id": 4,
        "extensions": {"display_index": 4, "probability": 40},
    }
    # Untouched entries are the same objects; the inputs are not mutated.
    assert rebuilt["entries"][1] is river
    assert new["id"] == 99 and book["entries"][0] is old
    assert rebuilt["extensions"]["keep"] is True


def test_the_stamp_carries_the_identity_the_part_had_before():
    book = {"name": "World", "entries": [_entry("Town", "old"), _entry("River", "a river")]}
    before = lorebook_split.split_book(book)[0]
    assert before.refs == ()
    fingerprint = lorebook_split.fingerprint([lorebook_split.entry_hash(e) for e in before.entries])

    rebuilt, _ = _rebuild(book, replace={0: {0: _entry("Town", "new")}})
    after = lorebook_split.split_book(rebuilt)[0]
    assert after.refs == (f"fp:{fingerprint}",) and after.title == "World"
    # And a second rewrite keeps it rather than re-deriving it from new content.
    again, _ = _rebuild(rebuilt, replace={0: {0: _entry("Town", "newer")}})
    assert lorebook_split.split_book(again)[0].refs == (f"fp:{fingerprint}",)


def test_two_versions_of_one_entry_on_a_card_collapse_to_the_chosen_one():
    a, b = _entry("Town", "one"), _entry("The Town", "two")
    book = {"entries": [a, b, _entry("River", "a river")]}
    # `b` is the chosen version and the card already holds it.
    rebuilt, outcome = _rebuild(book, replace={0: {0: b}})
    assert [e["content"] for e in rebuilt["entries"]] == ["two", "a river"]
    assert (outcome.upgraded, outcome.removed) == (0, 1)
    assert rebuilt["extensions"]["jai_lorebooks"][0]["entry_count"] == 2


def test_attach_appends_with_fresh_ids_and_detach_removes_a_whole_part():
    book = {
        "name": "Cleo — JanitorAI lorebooks",
        "extensions": {
            "jai_sources": [{"id": "w", "title": "World", "entry_count": 1}, {"id": "p", "title": "People", "entry_count": 2}]
        },
        "entries": [
            _entry("[World] Town", "a town", id=0),
            _entry("[People] Mayor", "the mayor", id=1),
            {"comment": "", "content": "", "keys": [], "id": 2},
        ],
    }
    addition = lorebook_sync.Attachment(
        title="Places", kind="chub", refs=("chub:lorebooks/x/places",), entries=(_entry("Inn", "an inn", id=0),)
    )
    rebuilt, outcome = _rebuild(book, drop={0}, attach=[addition])
    assert (outcome.attached, outcome.detached) == (1, 1)
    # The empty row belongs to People's count and stays where it was.
    assert [(e["comment"], e["id"]) for e in rebuilt["entries"]] == [("[People] Mayor", 1), ("", 2), ("Inn", 3)]
    assert rebuilt["entries"][2]["extensions"] == {"display_index": 2}
    parts = lorebook_split.split_book(rebuilt)
    assert [(p.title, p.kind, p.refs, len(p.entries), p.comment_prefix) for p in parts] == [
        ("People", "janitor", ("jai:p",), 1, "[People] "),
        ("Places", "chub", ("chub:lorebooks/x/places",), 1, ""),
    ]


def test_a_card_with_no_book_gets_one_and_an_emptied_book_is_none():
    addition = lorebook_sync.Attachment(title="Places", kind="embedded", refs=("fp:abc",), entries=(_entry("Inn", "an inn"),))
    rebuilt, _ = lorebook_sync.rebuild(None, [], attach=[addition])
    assert rebuilt["name"] == "Places" and len(rebuilt["entries"]) == 1
    assert lorebook_sync.rebuild(rebuilt, lorebook_split.segments(rebuilt), drop={0}) == (
        None,
        lorebook_sync.Outcome(detached=1),
    )


def test_a_stamp_that_no_longer_tiles_is_ignored():
    book = {"name": "World", "entries": [_entry("Town", "a town")]}
    rebuilt, _ = _rebuild(book, attach=[lorebook_sync.Attachment("Places", "embedded", ("fp:abc",), (_entry("Inn", "an inn"),))])
    assert len(lorebook_split.split_book(rebuilt)) == 2
    rebuilt["entries"].pop()
    assert [p.refs for p in lorebook_split.split_book(rebuilt)] == [()]


def test_clean_entry_strips_only_its_own_prefix():
    entry = _entry("[World] Town", "a town")
    assert lorebook_sync.clean_entry(entry, "[World] ")["comment"] == "Town"
    assert lorebook_sync.clean_entry(entry, "[People] ")["comment"] == "[World] Town"
    assert entry["comment"] == "[World] Town"
