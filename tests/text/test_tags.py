import pytest

from aspect.text.tags import clean_tag, normalize_tag, normalize_tags

# ---------------------------------------------------------------------------
# clean_tag -- real emoji-prefixed tag chips (SillyTavern can't render emoji
# in tags, so the JanitorAI tag names must be stripped down to plain words).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("👩‍🦰 Female", "Female"),
        ("👤 AnyPOV", "AnyPOV"),
        ("🧬 Demi-Human", "Demi-Human"),
        ("#ottergirl", "ottergirl"),
        ("  # slowburn", "slowburn"),
        ("TheValentine", "TheValentine"),
    ],
)
def test_clean_tag_strips_leading_emoji_and_hash(raw, expected):
    assert clean_tag(raw) == expected


# ---------------------------------------------------------------------------
# normalize_tag / normalize_tags -- Phase 5 §3's four rules, applied at
# intake ahead of the dictionary-driven merge (which lives elsewhere).
# ---------------------------------------------------------------------------


def test_normalize_tag_strips_leading_hash_emoji_punctuation():
    assert normalize_tag("#wildwest") == "wildwest"
    assert normalize_tag("👩‍🦰 Female") == "Female"


def test_normalize_tag_trims_and_collapses_internal_whitespace():
    assert normalize_tag("  slow   burn  ") == "slow burn"
    assert normalize_tag("a\t\tb\nc") == "a b c"


def test_normalize_tag_drops_to_empty_string():
    assert normalize_tag("   ") == ""
    assert normalize_tag("###") == ""


def test_normalize_tags_drops_empty_after_cleaning():
    assert normalize_tags(["Female", "   ", "###", "AnyPOV"]) == ["Female", "AnyPOV"]


def test_normalize_tags_dedupes_case_insensitively_keeping_first_casing_and_order():
    assert normalize_tags(["Femdom", "femdom", "Female", "FEMDOM"]) == ["Femdom", "Female"]


def test_normalize_tags_never_splits_the_comma_tag():
    # "Can Be Wholesome, Can Be Sexy" is one genuine JanitorAI tag on 515
    # cards, and a canonical in the shipped dictionary -- splitting it here
    # would damage every one of them. See docs/PHASE_5_TAGS_PLAN.md §3/§7.
    assert normalize_tags(["Can Be Wholesome, Can Be Sexy"]) == ["Can Be Wholesome, Can Be Sexy"]


def test_normalize_tags_does_not_touch_casing():
    # Femdom vs femdom is a merge decision (half b), not intake's job.
    assert normalize_tags(["Femdom"]) == ["Femdom"]


def test_normalize_tags_preserves_order_of_first_occurrence():
    assert normalize_tags(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


def test_normalize_tags_ignores_non_string_entries():
    assert normalize_tags(["Female", 42, None, "AnyPOV"]) == ["Female", "AnyPOV"]
