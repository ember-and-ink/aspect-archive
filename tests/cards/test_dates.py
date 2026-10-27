"""`create_date`: deriving it, preserving it, and never dropping it on a rewrite.

The bug this covers was a silent one -- the field was simply absent, so the
frontend's "Date Created" sort compared 0 to 0 across the whole archive and the
Info panel read "(not available)" on every card. Nothing errored. So these tests
assert presence and provenance, not just shape.
"""

from __future__ import annotations

import io

from PIL import Image

from aspect.cards import dates, pngtools

EARLY = "2026-05-15T15:41:58.048Z"
LATE = "2026-07-30T16:47:21.656Z"


def _extensions(**blocks: str) -> dict:
    return {"extensions": {key: {"linkedAt": value} for key, value in blocks.items()}}


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (2, 2)).save(buf, "PNG")
    return buf.getvalue()


# --- derivation -------------------------------------------------------------


def test_earliest_linked_at_wins_over_jai():
    """The whole point. `jai.linkedAt` is restamped by every rewrite this tool
    does, so on a card acquired through Chub it is months later than the truth;
    the provider's own stamp is when the card actually arrived."""
    data = _extensions(jai=LATE, chub=EARLY)
    assert dates.earliest_linked_at(data) == EARLY


def test_jai_is_used_when_it_is_the_only_provenance():
    assert dates.earliest_linked_at(_extensions(jai=LATE)) == LATE


def test_no_provenance_derives_nothing():
    """Empty, not "now": a card with nothing to go on is undated, and inventing
    a date would make that permanently unrecoverable."""
    assert dates.earliest_linked_at({"extensions": {}}) == ""
    assert dates.earliest_linked_at({}) == ""


def test_unparseable_stamps_are_ignored_not_ordered_as_strings():
    data = {"extensions": {"a": {"linkedAt": "sometime"}, "b": {"linkedAt": LATE}}}
    assert dates.earliest_linked_at(data) == LATE


def test_non_dict_extension_blocks_do_not_break_the_scan():
    """`extensions` mixes provider objects with scalars -- `fav`, `gallery_id`,
    `talkativeness` all sit at the same level."""
    data = {"extensions": {"fav": True, "gallery_id": "abc", "jai": {"linkedAt": LATE}}}
    assert dates.earliest_linked_at(data) == LATE


def test_a_recorded_date_outranks_a_derived_one():
    """A card from SillyTavern brought a real `create_date`. Recomputing would
    replace a recorded fact with an inference."""
    envelope = {"create_date": "2023-01-02T03:04:05.000Z"}
    resolved = dates.resolve_create_date(envelope, _extensions(jai=LATE))
    assert resolved == "2023-01-02T03:04:05.000Z"


def test_stamp_leaves_an_underivable_card_unstamped():
    envelope: dict = {"data": {"extensions": {}}}
    dates.stamp(envelope)
    assert "create_date" not in envelope


# --- the write funnels ------------------------------------------------------


def test_embed_card_stamps_a_card_that_had_none():
    data = {"name": "Test", **_extensions(jai=LATE, chub=EARLY)}
    png = pngtools.embed_card(_png(), {}, data)
    envelope, _ = pngtools.read_envelope(png)
    assert envelope["create_date"] == EARLY


def test_embed_card_does_not_drop_an_existing_create_date():
    """The trap: `embed_card` rebuilds the envelope from the spec header plus
    `data`, and `create_date` lives at the root *outside* `data`. Without an
    explicit carry-over, every in-place patch would quietly erase it."""
    data = {"name": "Test", **_extensions(jai=LATE)}
    original = {"spec": "chara_card_v3", "spec_version": "3.0", "create_date": EARLY}
    png = pngtools.embed_card(_png(), original, data)
    envelope, _ = pngtools.read_envelope(png)
    assert envelope["create_date"] == EARLY


def test_create_date_stays_at_the_root_not_in_data():
    """SillyTavern reads it from the root. Inside `data` it would be invisible
    to ST's own Date Created sort, which is half of why this field exists."""
    data = {"name": "Test", **_extensions(jai=LATE)}
    envelope, embedded = pngtools.read_envelope(pngtools.embed_card(_png(), {}, data))
    assert envelope["create_date"] == LATE
    assert "create_date" not in embedded
