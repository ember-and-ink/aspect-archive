"""Creator-notes taming, checked against two real Chub blurbs.

`nikita` is the styling-heavy case (two <style> blocks, ~8.5KB of CSS,
fixed-position decoration, hard-coded two-column grids); `kyana` is the
palette-driven case (a :root variable sheet and class-based layout, no inline
styles at all). Between them they cover both ways a Chub card carries layout.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from aspect.text.notes_html import clean_creator_notes, has_layout_structure, tame_html

FIXTURES = Path(__file__).parent.parent / "fixtures" / "notes"


def load(name: str) -> str:
    return (FIXTURES / f"{name}_chub_notes.html").read_text()


def words(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for dead in soup.find_all(["style", "script"]):
        dead.decompose()
    return re.findall(r"[A-Za-z']{2,}", soup.get_text(" "))


@pytest.fixture(params=["nikita", "kyana"])
def card(request) -> str:
    return load(request.param)


# ---------------------------------------------------------------------------
# the SillyTavern contract
# ---------------------------------------------------------------------------


def test_style_blocks_are_gone(card):
    """The whole point: ST's "Creator's Notes contain CSS style tags" prompt
    keys off <style> elements and nothing else."""
    assert "<style" in card  # the source really does carry one
    assert "<style" not in tame_html(card)


def test_nothing_can_escape_the_drawer(card):
    """Inline styles are *not* scoped by ST's prompt, so a surviving
    position:fixed would paint over the whole app."""
    tamed = tame_html(card)
    for banned in ("position:", "z-index", "animation", "transition", "@keyframes"):
        assert banned not in tamed


def test_palette_is_dropped_in_favour_of_the_theme(card):
    tamed = tame_html(card)
    for banned in ("color:", "background", "box-shadow", "text-shadow", "filter:"):
        assert banned not in tamed


def test_images_are_bounded(card):
    """ST bounds images in .mes_text but not in #creator_notes_spoiler."""
    soup = BeautifulSoup(tame_html(card), "html.parser")
    for img in soup.find_all("img"):
        assert "max-width:100%" in img["style"]


# ---------------------------------------------------------------------------
# content and layout preservation
# ---------------------------------------------------------------------------


def test_no_prose_is_lost(card):
    assert words(tame_html(card)) == words(card)


def test_images_are_kept(card):
    assert card.count("<img") == tame_html(card).count("<img")


def test_layout_survives(card):
    """Structure is the thing worth keeping -- markdown flattening loses it."""
    tamed = tame_html(card)
    assert "<div" in tamed
    assert "display:grid" in tamed or "display:flex" in tamed


def test_class_based_layout_is_inlined():
    """Kyana declares its layout only in the stylesheet, so dropping <style>
    without inlining it first would lose that layout entirely."""
    kyana = load("kyana")
    assert "style=" not in kyana  # nothing inline in the source at all
    assert "display:flex" in tame_html(kyana)


def test_fixed_column_counts_become_responsive():
    """`1fr 1fr` can't reflow in a ~400px drawer; auto-fit can."""
    nikita = load("nikita")
    assert "grid-template-columns:1fr 1fr" in nikita.replace(" 1fr;", " 1fr;")
    tamed = tame_html(nikita)
    assert "1fr 1fr" not in tamed
    assert "auto-fit" in tamed


def test_bare_decoration_is_dropped_but_labelled_glyphs_stay():
    """The floating ⟐ runes and gradient orbs are positioned decoration -- in
    flow they are just noise at the top of the notes. The same glyph attached
    to words ("⟐ CLEARANCE: [CLASSIFIED]") is a label and stays."""
    nikita = load("nikita")
    tamed = tame_html(nikita)
    assert nikita.count("⟐") == 20
    assert tamed.count("⟐") == 11  # the 9 wordless, positioned ones are gone
    assert "CLEARANCE: [CLASSIFIED]" in tamed
    assert "<div>⟐</div>" not in tamed


def test_out_of_flow_prose_is_kept_not_dropped():
    """Positioned chrome that carries words (section tabs like
    `PSYCH_PROFILE.txt`) is content, not decoration."""
    tamed = tame_html(load("nikita"))
    assert "PSYCH_PROFILE.txt" in tamed
    assert "MISSION_PARAMETERS.log" in tamed


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def test_laid_out_notes_route_to_tame(card):
    assert has_layout_structure(card)
    assert clean_creator_notes(card) == tame_html(card)


def test_plain_notes_still_flatten_to_markdown():
    """A JanitorAI-style blurb -- shallow styled prose, no layout -- must keep
    the existing markdown treatment."""
    plain = '<p style="color:#ff0"><b>Hi</b></p><p>Second <i>para</i></p>'
    assert not has_layout_structure(plain)
    cleaned = clean_creator_notes(plain)
    assert cleaned == "**Hi**\n\nSecond *para*"


def test_empty_notes():
    assert clean_creator_notes("") == ""
    assert clean_creator_notes("   ") == ""


def test_tame_is_idempotent(card):
    once = tame_html(card)
    assert tame_html(once) == once


# ---------------------------------------------------------------------------
# background images (as distinct from background palette)
# ---------------------------------------------------------------------------


def test_background_image_survives_as_content():
    """`ravel` lays its header art in via `background: <gradient>, url(...)` on
    a wrapping div rather than an <img> tag. The gradient is a dark tint --
    palette, correctly dropped -- but the photo behind it is the actual
    content and must not disappear along with it."""
    ravel = load("ravel")
    assert "files.kammii.org/8215126991339kt4/LdUsxxJAS3cBI0DU8Y.webp" in ravel
    tamed = tame_html(ravel)
    assert "files.kammii.org/8215126991339kt4/LdUsxxJAS3cBI0DU8Y.webp" in tamed
    assert "background-image:url(" in tamed
    assert "linear-gradient" not in tamed  # the tint itself is still palette
    assert clean_creator_notes(ravel) == tamed
