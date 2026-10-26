"""gallery_id generation and the read/ensure accessors -- the id
SillyTavern-CharacterLibrary keys a character's image gallery on.

The format is a contract with *their* code (12 chars from a 62-char alphanumeric
alphabet, in `data.extensions.gallery_id`), so it's pinned here. The other half
of the contract is that an existing id is never regenerated -- it's the link to a
gallery folder that may already hold images.
"""

import string

from aspect.cards import gallery

# ---------------------------------------------------------------------------
# generate_id -- CharacterLibrary's generateGalleryId, in Python
# ---------------------------------------------------------------------------


def test_generated_id_is_twelve_alphanumeric_chars():
    allowed = set(string.ascii_letters + string.digits)
    for _ in range(200):
        gid = gallery.generate_id()
        assert len(gid) == 12
        assert set(gid) <= allowed


def test_generated_ids_are_distinct():
    # Not a randomness test -- just that we're not handing every card the same
    # id (which would collapse every same-named character into one gallery).
    assert len({gallery.generate_id() for _ in range(500)}) == 500


# ---------------------------------------------------------------------------
# read_id
# ---------------------------------------------------------------------------


def test_read_id_present_absent_and_empty():
    assert gallery.read_id({"extensions": {"gallery_id": "f1AMBFO5oPUr"}}) == "f1AMBFO5oPUr"
    assert gallery.read_id({"extensions": {"chub": {"id": 1}}}) is None
    assert gallery.read_id({}) is None
    assert gallery.read_id({"extensions": {"gallery_id": ""}}) is None
    assert gallery.read_id({"extensions": None}) is None


# ---------------------------------------------------------------------------
# ensure_id -- mutates in place, returns the assigned id (None == unchanged)
# ---------------------------------------------------------------------------


def test_ensure_id_stamps_a_fresh_id_and_returns_it():
    data = {"name": "Lyra", "extensions": {"jai": {"id": "abc"}}}
    gid = gallery.ensure_id(data)

    assert gid is not None and len(gid) == 12
    assert data["extensions"]["gallery_id"] == gid
    assert data["extensions"]["jai"] == {"id": "abc"}  # siblings untouched


def test_ensure_id_leaves_an_existing_id_alone():
    data = {"extensions": {"gallery_id": "ORIGINAL0000"}}
    assert gallery.ensure_id(data) is None
    assert gallery.ensure_id(data, preferred="NEWER0000000") is None
    assert data["extensions"]["gallery_id"] == "ORIGINAL0000"


def test_ensure_id_adopts_the_preferred_id_when_given():
    data = {"extensions": {}}
    assert gallery.ensure_id(data, preferred="CARRIEDOVER1") == "CARRIEDOVER1"
    assert data["extensions"]["gallery_id"] == "CARRIEDOVER1"


def test_ensure_id_falls_back_to_a_fresh_id_for_an_empty_preferred():
    data = {}
    gid = gallery.ensure_id(data, preferred="")
    assert gid is not None and len(gid) == 12
    assert data["extensions"]["gallery_id"] == gid


def test_ensure_id_creates_extensions_when_missing_or_not_a_dict():
    for data in ({}, {"extensions": None}, {"extensions": "junk"}):
        gid = gallery.ensure_id(data)
        assert data["extensions"]["gallery_id"] == gid


# --- folder_name ------------------------------------------------------------
# The convention CharacterLibrary computes live, reimplemented so the archive can
# find the 3,804 folders that already exist. Verified against the live archive at
# 3,785 of 3,839 cards, the remainder being cards whose images were never pulled.


def test_folder_name_is_name_plus_id():
    assert gallery.folder_name("Abbie", "kzbYR2QbpncC") == "Abbie_kzbYR2QbpncC"


def test_folder_name_replaces_only_the_reserved_characters():
    """Deliberately *not* the card-filename sanitizer: a card named `A.D.A` is the
    file `A_D_A_<id8>.png` but the gallery folder `A.D.A_<gallery_id>`, and dots,
    spaces, commas and curly apostrophes all survive."""
    assert gallery.folder_name("A.D.A", "x" * 12) == "A.D.A_xxxxxxxxxxxx"
    assert gallery.folder_name("A Mother’s Claim, A Hunger", "y" * 12) == (
        "A Mother’s Claim, A Hunger_yyyyyyyyyyyy"
    )
    assert gallery.folder_name('Bad<>:"/\\|?*Name', "z" * 12) == "Bad_________Name_zzzzzzzzzzzz"


def test_folder_name_trims_surrounding_whitespace():
    assert gallery.folder_name("  Padded  ", "q" * 12) == "Padded_qqqqqqqqqqqq"


def test_no_gallery_id_means_no_folder():
    """Rather than a name every id-less card would collide on."""
    assert gallery.folder_name("Abbie", None) == ""
    assert gallery.folder_name("Abbie", "") == ""


# --- resolving a folder by its id -------------------------------------------
#
# Folder names are derived from the card's *current* name, so a rename used to
# orphan the images. Resolution matches on the `_<gallery_id>` tail instead --
# the same rule the archive already uses for its duplicate checks: the id alone,
# never the name.


def test_id_of_reads_the_tail(tmp_path):
    assert gallery.id_of("Abbie_kzbYR2QbpncC") == "kzbYR2QbpncC"
    # Underscores in the name are not a problem: only the last one counts.
    assert gallery.id_of("A_D_A_kzbYR2QbpncC") == "kzbYR2QbpncC"


def test_id_of_rejects_a_word_that_is_only_a_name():
    """`Marcus_Wright` must not register itself under the id 'Wright'."""
    assert gallery.id_of("Marcus_Wright") == ""
    assert gallery.id_of("Abbie") == ""
    assert gallery.id_of("") == ""


def test_resolve_prefers_an_exact_match(tmp_path):
    (tmp_path / "Abbie_kzbYR2QbpncC").mkdir()
    assert gallery.resolve_folder(tmp_path, "Abbie_kzbYR2QbpncC") == "Abbie_kzbYR2QbpncC"


def test_resolve_finds_a_folder_left_behind_by_a_rename(tmp_path):
    (tmp_path / "Abbie_kzbYR2QbpncC").mkdir()
    assert gallery.resolve_folder(tmp_path, "Abigail_kzbYR2QbpncC") == "Abbie_kzbYR2QbpncC"


def test_resolve_gives_up_on_an_id_nothing_carries(tmp_path):
    (tmp_path / "Abbie_kzbYR2QbpncC").mkdir()
    assert gallery.resolve_folder(tmp_path, "Cleo_CCCCCCCCCCCC") is None
    assert gallery.resolve_folder(tmp_path, "") is None
    assert gallery.resolve_folder(tmp_path, "NoIdHere") is None


def test_resolve_picks_up_a_folder_added_after_the_first_lookup(tmp_path):
    """The map is cached on the directory's mtime, so a gallery created between
    two requests has to invalidate it -- a stale miss here reads as 'this card
    has no images' right after its first image was downloaded."""
    assert gallery.resolve_folder(tmp_path, "Abbie_kzbYR2QbpncC") is None
    (tmp_path / "Abbie_kzbYR2QbpncC").mkdir()
    assert gallery.resolve_folder(tmp_path, "Abigail_kzbYR2QbpncC") == "Abbie_kzbYR2QbpncC"
