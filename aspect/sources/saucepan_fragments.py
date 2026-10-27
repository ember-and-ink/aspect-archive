"""Reassembly of saucepan.ai's obfuscated text wire format.

saucepan.ai does NOT ship prose as plain strings. Every long text field --
companion description, each starting scenario, each definition section, each
lorebook chapter -- arrives as a `text_fragments` bag:

    {version, mask, fragments: [{text, key, proof}, ...]}

`fragments` is a shuffled pile of the real prose fragments MIXED WITH DECOYS
(~a quarter of every payload is fake). Reassembly:

    1. keep only fragments whose `proof` validates against (mask, key ^ mask, text)
    2. order the survivors by `key XOR mask` (a dense 0..n-1 sequence)
    3. concatenate their `text`

The proof is an FNV-1a variant seeded from (mask, ordinal) and digesting the
UTF-8 text (the bundle's `T0t`). It binds each fragment to both its position and
its content, so a decoy can't be laundered in by renumbering and real prose
can't be reordered. `mask` is per-payload -- it can't be hardcoded.

Reverse-engineered out of saucepan's minified app bundle (`T0`/`T0t`/`gW`); the
executable spec lives in tests/test_saucepan_lorebook.py, pinned against real
captures.
"""

from __future__ import annotations

from typing import Any

MASK32 = 0xFFFFFFFF
FNV32_OFFSET = 2166136261
FNV32_PRIME = 16777619


def _rotl32(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (32 - bits))) & MASK32


def fragment_proof(mask: int, ordinal: int, text: str) -> int:
    """FNV-1a variant seeded from (mask, ordinal). Mirrors the bundle's `T0t`."""
    acc = (FNV32_OFFSET ^ _rotl32(mask, 7) ^ _rotl32(ordinal, 13)) & MASK32
    for byte in text.encode("utf-8"):
        acc = ((acc ^ byte) * FNV32_PRIME) & MASK32
    return acc


def _is_real(mask: int, fragment: dict[str, Any]) -> bool:
    ordinal = fragment["key"] ^ mask
    return fragment_proof(mask, ordinal, fragment["text"]) == fragment["proof"]


def deobfuscate_fragments(text_fragments: dict[str, Any] | None) -> str:
    """Validate, order, and concatenate a `text_fragments` bag into plain prose.

    Returns "" for a missing/empty bag or one whose fragments are all decoys
    (saucepan's "Blank"/placeholder scenarios are exactly this -- a bag of pure
    decoys that reassembles to nothing)."""
    if not text_fragments:
        return ""
    fragments = text_fragments.get("fragments")
    if not fragments:
        return ""
    mask = text_fragments["mask"]
    real = [f for f in fragments if _is_real(mask, f)]
    real.sort(key=lambda f: f["key"] ^ mask)
    return "".join(f["text"] for f in real)
