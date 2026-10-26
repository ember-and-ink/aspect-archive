"""Same-creator near-duplicate detection: a bounded heuristic, not a perfect
clustering system.

Two cards are ever compared only if they share a creator -- the caller
partitions by creator before anything here runs, and nothing in this module
accepts cards from different creators as a pair. Within one creator, prose
comparison (`difflib.SequenceMatcher` over description/first_mes/
creator_notes) is the expensive step, so it only ever runs on a pair that
already cleared a cheap gate: a near-identical avatar (bitwise hamming
distance on a 256-bit average-hash) or a near-identical name (a short-string
`SequenceMatcher` ratio). Real archive creators run up to ~250 cards, which
is ~31k possible pairs for that one creator alone -- the gate is what keeps
that from becoming 31k full-text comparisons.

No I/O here: avatar bytes and card prose are read by the caller and handed
in, which is what makes this testable without touching a filesystem.
"""

from __future__ import annotations

import io
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Iterable, Literal

from PIL import Image

# --- avatar perceptual hash -------------------------------------------------

_HASH_SIZE = 16  # 16x16 -> a 256-bit hash


def avatar_hash(image_bytes: bytes) -> int:
    """A 256-bit average-hash of an image's pixels: two pictures that are
    visually the same (even re-encoded, re-compressed, differently cropped
    by a thumbnailer) land at a small hamming distance. Validated against a
    real pair of re-uploaded cards sharing one source image: hamming
    distance 0."""
    image = Image.open(io.BytesIO(image_bytes)).convert("L").resize((_HASH_SIZE, _HASH_SIZE))
    pixels = image.tobytes()  # mode "L" -- one byte per pixel
    average = sum(pixels) / len(pixels)
    bits = 0
    for pixel in pixels:
        bits = (bits << 1) | (1 if pixel > average else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# --- text / name similarity -------------------------------------------------


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def name_similarity(a: str, b: str) -> float:
    return _ratio(a.strip().casefold(), b.strip().casefold())


def text_similarity(a: str, b: str) -> float:
    return _ratio(a, b)


# --- thresholds --------------------------------------------------------------

# Tuned against the real archive: an identical re-upload lands at avatar
# distance 0; StormSight's two "Olivia" cards (same picture, expanded
# description) are the target case for AVATAR_GATE_DISTANCE. NAME_GATE_SCORE
# started at 0.82 but that caught "amelia"/"caelia"/"daelia"-style themed-pack
# naming (a shared 4-letter suffix on a 6-letter name is a 0.83 ratio) as
# false positives on a real creator's catalogue; 0.90 clears that case while
# still catching an exact match (1.0) and single-character variants.
AVATAR_GATE_DISTANCE = 12  # out of 256 bits
NAME_GATE_SCORE = 0.90
TEXT_STRONG_SCORE = 0.55  # description/first_mes/creator_notes overlap


@dataclass(frozen=True)
class CardSignals:
    """One card's precomputed cheap signals -- enough to gate a pair without
    a second disk read."""

    filename: str
    name: str
    creator: str
    avatar_hash: int | None


@dataclass(frozen=True)
class PairMatch:
    a: str
    b: str
    avatar_distance: int | None
    name_score: float
    text_score: float
    strength: Literal["strong", "weak"]
    reasons: tuple[str, ...]


def group_by_creator(signals: Iterable[CardSignals]) -> dict[str, list[CardSignals]]:
    """Bucket by creator, dropping creators with only one card -- a lone card
    has nothing to be a duplicate of, and this is what keeps the archive's
    ~3,800 cards from ever being compared outside their own creator."""
    buckets: dict[str, list[CardSignals]] = defaultdict(list)
    for signal in signals:
        if signal.creator:
            buckets[signal.creator].append(signal)
    return {creator: cards for creator, cards in buckets.items() if len(cards) > 1}


def _gate(a: CardSignals, b: CardSignals) -> tuple[int | None, float] | None:
    """The cheap pre-filter. Returns `(avatar_distance, name_score)` when
    either signal alone is strong enough to justify reading this pair's
    prose, else None. Bitwise ops and short-string ratios, so running this
    over every same-creator pair -- even a 253-card creator's ~32k of them --
    costs milliseconds, not the minutes a blind SequenceMatcher sweep would."""
    distance = None
    if a.avatar_hash is not None and b.avatar_hash is not None:
        distance = hamming(a.avatar_hash, b.avatar_hash)
    name_score = name_similarity(a.name, b.name)
    if (distance is not None and distance <= AVATAR_GATE_DISTANCE) or name_score >= NAME_GATE_SCORE:
        return distance, name_score
    return None


def _reasons(distance: int | None, name_score: float, text_score: float) -> tuple[str, ...]:
    reasons: list[str] = []
    if distance is not None and distance <= AVATAR_GATE_DISTANCE:
        reasons.append("identical avatar" if distance <= 4 else "near-identical avatar")
    if name_score >= NAME_GATE_SCORE:
        reasons.append("exact name match" if name_score >= 0.999 else "near-identical name")
    if text_score >= TEXT_STRONG_SCORE:
        reasons.append(f"{round(text_score * 100)}% text overlap")
    return tuple(reasons)


def build_groups(
    creator_cards: list[CardSignals],
    read_text: Callable[[str], tuple[str, str, str]],
) -> list[list[PairMatch]]:
    """Union-find candidate duplicate groups within ONE creator's cards.
    `read_text(filename)` returns `(description, first_mes, creator_notes)`
    and is only ever called for a card on the strong side of a gated pair,
    memoized here so a card in several pairs is read once.

    A pair's `strength` is "strong" when the avatar alone proves it (a
    near-identical picture needs no corroboration) or the name match is
    backed by real text overlap; "weak" when only the name matches --
    two different characters that happen to share a name (e.g. a creator's
    recurring OC) look exactly like this, and a human reviewing the group is
    the intended way to catch that, not a higher threshold that would also
    drop real duplicates with a rewritten name.
    """
    text_cache: dict[str, tuple[str, str, str]] = {}

    def text_of(filename: str) -> tuple[str, str, str]:
        if filename not in text_cache:
            text_cache[filename] = read_text(filename)
        return text_cache[filename]

    parent = {card.filename: card.filename for card in creator_cards}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    matches: list[PairMatch] = []
    for i, a in enumerate(creator_cards):
        for b in creator_cards[i + 1 :]:
            gated = _gate(a, b)
            if gated is None:
                continue
            distance, name_score = gated

            avatar_strong = distance is not None and distance <= AVATAR_GATE_DISTANCE
            desc_a, mes_a, notes_a = text_of(a.filename)
            desc_b, mes_b, notes_b = text_of(b.filename)
            text_score = max(
                text_similarity(desc_a, desc_b),
                text_similarity(mes_a, mes_b),
                text_similarity(notes_a, notes_b),
            )
            strength: Literal["strong", "weak"] = (
                "strong" if avatar_strong or text_score >= TEXT_STRONG_SCORE else "weak"
            )
            matches.append(
                PairMatch(
                    a=a.filename,
                    b=b.filename,
                    avatar_distance=distance,
                    name_score=name_score,
                    text_score=text_score,
                    strength=strength,
                    reasons=_reasons(distance, name_score, text_score),
                )
            )
            union(a.filename, b.filename)

    groups: dict[str, list[PairMatch]] = defaultdict(list)
    for match in matches:
        groups[find(match.a)].append(match)
    return list(groups.values())


def find_duplicate_groups(
    signals: Iterable[CardSignals],
    read_text: Callable[[str], tuple[str, str, str]],
) -> list[list[PairMatch]]:
    """Every candidate duplicate group across the archive, scoped creator by
    creator -- the entry point a route calls."""
    groups: list[list[PairMatch]] = []
    for cards in group_by_creator(signals).values():
        groups.extend(build_groups(cards, read_text))
    return groups
