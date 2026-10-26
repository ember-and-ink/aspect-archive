"""Executable spec for saucepan.ai's obfuscated lorebook wire format.

saucepan.ai is NOT JanitorAI -- nothing in `aspect/` reads this format today, and
`userscript/saucepan-export.user.js` (which this algorithm is ported from) is parked.
This file exists because the reassembly scheme was reverse-engineered out of saucepan's
minified app bundle (`T0`/`T0t`/`gW`), and the two fixtures here are the only real
captures of it that exist. Pin both while they're pinnable: if saucepan support ever
becomes real, lift `deobfuscate_fragments` into `aspect/` and keep these tests.

Wire shape, per chapter (`GET /api/v2/lorebooks/<id>/chapters/<index>`):

    {index, title, text_fragments: {version, mask, fragments: [{text, key, proof}, ...]}}

`fragments` is a shuffled bag of real prose fragments MIXED WITH DECOYS. Reassembly:
keep only fragments whose `proof` validates, order by `key XOR mask`, concatenate.
See tests/fixtures/README.md for provenance.
"""

import json
from pathlib import Path

# The algorithm under test now lives in aspect/ (lifted here once saucepan support
# became real). This file stays the executable spec, pinned against real captures.
from aspect.sources.saucepan_fragments import _is_real, deobfuscate_fragments

FIXTURES = Path(__file__).parent.parent / "fixtures" / "saucepan"


def _load_chapter(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _chapter0() -> dict:
    return _load_chapter("lorebook_chapter0.json")


def _chapter1() -> dict:
    return _load_chapter("lorebook_chapter1.json")


# ---------------------------------------------------------------------------
# Reassembly against the two real captures.
# ---------------------------------------------------------------------------


def test_deobfuscates_chapter0_to_coherent_prose():
    chapter = _chapter0()
    assert chapter["title"] == "Eve's Father"

    text = deobfuscate_fragments(chapter["text_fragments"])

    assert text.startswith("NAME: Doctor Charles Ackerman\nAGE: 63\n")
    assert "ROLE: Roboticist, father of Eve" in text
    assert text.endswith("burning his own life down to protect someone he loves.")
    assert len(text) == 1827


def test_deobfuscates_chapter1_to_coherent_prose():
    chapter = _chapter1()
    assert chapter["title"] == "The Bullies"

    text = deobfuscate_fragments(chapter["text_fragments"])

    assert text.startswith("NAME: Jake Thorne\nAGE: 18\n")
    assert text.endswith('"Who, me?"')
    assert len(text) == 4829


def test_decoys_are_a_large_fraction_of_every_payload():
    # Roughly a quarter of what the API returns is fake. A port that silently
    # stopped validating would still "work" -- see the tail test below -- so
    # assert the decoys are really there and really being dropped.
    for chapter, total, kept in ((_chapter0(), 82, 62), (_chapter1(), 208, 156)):
        fragments = chapter["text_fragments"]["fragments"]
        mask = chapter["text_fragments"]["mask"]
        assert len(fragments) == total
        assert sum(1 for f in fragments if _is_real(mask, f)) == kept


# ---------------------------------------------------------------------------
# Why `proof` validation is load-bearing -- the trap this format sets.
# ---------------------------------------------------------------------------


def test_real_fragment_ordinals_are_contiguous_from_zero():
    # `key XOR mask` yields a dense 0..n-1 sequence over the REAL fragments, which
    # is what makes concatenation-after-sort reconstruct the prose exactly.
    for chapter in (_chapter0(), _chapter1()):
        mask = chapter["text_fragments"]["mask"]
        ordinals = sorted(
            f["key"] ^ mask for f in chapter["text_fragments"]["fragments"] if _is_real(mask, f)
        )
        assert ordinals == list(range(len(ordinals)))


def test_skipping_proof_validation_corrupts_only_the_tail():
    # The trap: decoy ordinals are large and scattered (chapter0: 1077..31891) while
    # real ordinals are 0..61, so decoys all sort PAST the real prose instead of
    # interleaving with it. An implementation that forgot to validate `proof` would
    # emit the entire real text intact and then append word-salad -- i.e. it looks
    # perfect if you eyeball the opening paragraph. Only the tail reveals the bug.
    chapter = _chapter0()
    text_fragments = chapter["text_fragments"]
    mask = text_fragments["mask"]

    clean = deobfuscate_fragments(text_fragments)
    unvalidated = "".join(
        f["text"] for f in sorted(text_fragments["fragments"], key=lambda f: f["key"] ^ mask)
    )

    assert unvalidated.startswith(clean)  # the head is identical -- the trap
    assert len(unvalidated) > len(clean)  # the damage is entirely appended
    assert unvalidated[len(clean) :].startswith("weather window")

    real_ordinals = {f["key"] ^ mask for f in text_fragments["fragments"] if _is_real(mask, f)}
    decoy_ordinals = {f["key"] ^ mask for f in text_fragments["fragments"] if not _is_real(mask, f)}
    assert not (real_ordinals & decoy_ordinals)
    assert min(decoy_ordinals) > max(real_ordinals)


def test_proof_is_bound_to_both_the_text_and_its_ordinal():
    # The proof seeds from (mask, ordinal) and digests the text, so a decoy can't be
    # laundered into the output by renumbering it, and real prose can't be reordered.
    chapter = _chapter0()
    mask = chapter["text_fragments"]["mask"]
    real = next(f for f in chapter["text_fragments"]["fragments"] if _is_real(mask, f))

    tampered_text = dict(real, text=real["text"] + "!")
    assert not _is_real(mask, tampered_text)

    tampered_ordinal = dict(real, key=real["key"] ^ 1)
    assert not _is_real(mask, tampered_ordinal)


def test_mask_differs_per_chapter():
    # `mask` is per-payload, not a build constant -- it can't be hardcoded.
    assert _chapter0()["text_fragments"]["mask"] == 1977
    assert _chapter1()["text_fragments"]["mask"] == 25346
    assert _chapter0()["text_fragments"]["version"] == 1
