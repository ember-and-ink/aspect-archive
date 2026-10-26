"""Guard rails around the interactive rename prompt.

The prompt takes free text, sits one keystroke away from `y`/`n`/`q`, and
writes straight to a real card. During the first archive pass `yy` and `u` were
both typed by accident and applied, and the cards had to be deleted and copied
back by hand. Two things stop that now: implausible typed names ask for
confirmation, and every rename can be reverted from the decision log.
"""

import base64
import io
import json

from PIL import Image

import scripts.fix_names as fix_names
from aspect.cards import pngtools


def card(name: str, description: str = "") -> dict:
    return {"name": name, "description": description}


def _png_with_card(data: dict) -> bytes:
    """A 4x4 avatar carrying `data` as a ccv3/chara card."""
    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), (10, 20, 30, 255)).save(buf, "PNG")
    envelope = {"spec": "chara_card_v3", "spec_version": "3.0", "data": dict(data)}
    payload = base64.b64encode(json.dumps(envelope).encode()).decode("ascii")
    return pngtools.inject_text_chunks(buf.getvalue(), {"chara": payload, "ccv3": payload})


# --------------------------------------------------------------------------
# _implausible -- catching a slipped keystroke before it lands on a card
# --------------------------------------------------------------------------


def test_repeated_character_is_implausible():
    assert fix_names._implausible("yy", card("X", "Ayame is a kitsune.")) is not None
    assert fix_names._implausible("uu", card("X", "Ayame is a kitsune.")) is not None


def test_single_character_is_implausible():
    assert fix_names._implausible("u", card("X", "Ayame is a kitsune.")) is not None


def test_short_name_absent_from_the_definition_is_implausible():
    assert fix_names._implausible("qw", card("X", "Ayame is a kitsune.")) is not None


def test_short_name_present_in_the_definition_is_allowed():
    """Io, Vi, Ru and V are real card names -- grounded ones must not nag."""
    assert fix_names._implausible("Io", card("X", "Io drifts through the ring.")) is None
    assert fix_names._implausible("Vi", card("X", "Vi throws the first punch.")) is None


def test_ordinary_name_is_allowed():
    assert fix_names._implausible("Ayame", card("X", "somebody else entirely")) is None


# --------------------------------------------------------------------------
# _card_id -- the same card must not count twice across copies
# --------------------------------------------------------------------------


def test_card_id_prefers_gallery_id(tmp_path):
    data = {"name": "A", "extensions": {"gallery_id": "zK8jzmiZXt00", "jai": {"id": "999"}}}
    assert fix_names._card_id(tmp_path / "A_deadbeef.png", data) == "zK8jzmiZXt00"


def test_card_id_falls_back_to_the_filename_fragment(tmp_path):
    assert fix_names._card_id(tmp_path / "A_deadbeef.png", {"name": "A"}) == "deadbeef"


def test_card_id_matches_across_a_staging_copy_and_its_archive_twin(tmp_path):
    """./import and the archive hold the same card under different names."""
    data = {"name": "Narrator", "extensions": {"gallery_id": "abc123"}}
    staged = fix_names._card_id(tmp_path / "import" / "Narrator_04355852.png", data)
    archived = fix_names._card_id(tmp_path / "cards" / "Angelica_04355852.png", data)
    assert staged == archived


# --------------------------------------------------------------------------
# --stats -- a reverted rename is a mistake, not a judgement
# --------------------------------------------------------------------------


def _write_log(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _decision(ts, file, action, name, chosen, card_id):
    return {
        "ts": ts, "session": "s1", "card_id": card_id, "dir": "/cards", "file": file,
        "new_file": f"{chosen}.png", "verdict": "junk", "reason": "", "ensemble": False,
        "name": name, "suggestion": chosen, "kept": [], "dropped": [], "candidates": [],
        "action": action, "chosen": chosen, "top1_hit": True, "chosen_rank": None,
        "error": None, "excerpt": "",
    }


def test_stats_drops_a_reverted_decision(tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    _write_log(log, [
        _decision("2026-08-11T01:00:00+00:00", "A.png", "accept", "A Tagline | Ana", "Ana", "id-a"),
        _decision("2026-08-11T01:22:23+00:00", "B.png", "typed", "Ayame - Feral", "yy", "id-b"),
        {"ts": "2026-08-11T02:00:00+00:00", "session": "s2", "card_id": "id-b", "dir": "/cards",
         "file": "yy.png", "new_file": "B.png", "verdict": "junk", "action": "undo",
         "undid": "2026-08-11T01:22:23+00:00|B.png", "name": "yy", "chosen": "Ayame - Feral",
         "suggestion": "Ayame", "top1_hit": False, "chosen_rank": None, "candidates": [],
         "error": None},
    ])
    assert fix_names._stats(log) == 0
    out = capsys.readouterr().out
    assert "1 decision(s)" in out  # the slip and its undo both vanish
    assert "yy" not in out


def test_stats_scores_only_the_latest_judgement_of_a_card(tmp_path, capsys):
    """Judging a staging copy and then its archive twin is one card, not two."""
    log = tmp_path / "log.jsonl"
    _write_log(log, [
        _decision("2026-08-11T00:15:00+00:00", "Narrator_04355852.png", "accept", "Narrator", "Brigid", "id-x"),
        _decision("2026-08-11T00:23:00+00:00", "Narrator_04355852.png", "accept", "Narrator", "Chief", "id-x"),
    ])
    assert fix_names._stats(log) == 0
    out = capsys.readouterr().out
    assert "1 re-judged card(s) collapsed" in out
    assert "n=   1" in out


def test_rejudge_scores_the_current_rules_not_the_logged_ones(tmp_path, capsys):
    """`--stats` is frozen history; `--rejudge` re-runs `diagnose` on the card."""
    cards = tmp_path / "cards"
    cards.mkdir()
    body = "Ana Volkova runs the casino. Ana's temper is short. Ana counts the take."
    (cards / "Ana_Volkova_1a2b3c4d.png").write_bytes(
        _png_with_card({"name": "Ana Volkova", "description": body})
    )
    log = tmp_path / "log.jsonl"
    # Logged as a miss ("Bianca"); the rules now say "Ana Volkova", which is
    # what the human chose, so re-judging must score it as a hit.
    row = _decision("2026-08-11T01:00:00+00:00", "Tagline_Ana_1a2b3c4d.png", "typed",
                    "At Your Mercy | Ana Volkova", "Ana Volkova", "id-a")
    row["new_file"] = "Ana_Volkova_1a2b3c4d.png"
    row["suggestion"] = "Bianca"
    _write_log(log, [row])

    assert fix_names._rejudge(log, cards) == 0
    out = capsys.readouterr().out
    assert "re-judged 1 logged decision(s)" in out
    assert "top-1 1/1" in out
    assert "TRUE FINDINGS LOST 0" in out


def test_rejudge_skips_a_card_that_no_longer_exists(tmp_path, capsys):
    """Same reason `--prune` exists: a deleted card was never winnable."""
    cards = tmp_path / "cards"
    cards.mkdir()
    log = tmp_path / "log.jsonl"
    _write_log(log, [
        _decision("2026-08-11T01:00:00+00:00", "Gone_deadbeef.png", "accept", "Narrator", "Ana", "id-z"),
    ])
    assert fix_names._rejudge(log, cards) == 0
    assert "1 skipped -- card deleted" in capsys.readouterr().out


# --------------------------------------------------------------------------
# _apply -- renaming on a case-insensitive filesystem
# --------------------------------------------------------------------------


def test_apply_handles_a_case_only_rename(tmp_path):
    """`kate` -> `Kate` is the same file on APFS, not a collision."""
    src = tmp_path / "kate_d3f402fa.png"
    src.write_bytes(_png_with_card({"name": "kate", "description": "Kate waits by the door."}))

    raw = src.read_bytes()
    envelope, data = pngtools.read_envelope(raw)
    assert fix_names._apply(src, raw, envelope, data, "Kate") is None

    target = tmp_path / "Kate_d3f402fa.png"
    assert target.exists()
    assert pngtools.read_envelope(target.read_bytes())[1]["name"] == "Kate"


def test_apply_still_refuses_a_real_collision(tmp_path):
    other = tmp_path / "Kate_aaaaaaaa.png"
    other.write_bytes(_png_with_card({"name": "Kate", "description": "someone else"}))
    src = tmp_path / "Katie_aaaaaaaa.png"
    src.write_bytes(_png_with_card({"name": "Katie", "description": "Kate waits."}))

    raw = src.read_bytes()
    envelope, data = pngtools.read_envelope(raw)
    err = fix_names._apply(src, raw, envelope, data, "Kate")
    assert err is not None and "already exists" in err
    assert src.exists()  # left where it was


def test_apply_keeps_a_short_chub_id_fragment(tmp_path):
    """A rename must never cost the card its id fragment -- that fragment is the
    only key `/existing` (the hide-saved toggle) and the import both match on.

    The filename regex demands 6-8 characters, but a Chub id can be as short as
    three (`Aria_1911.png`, 41 of them in the archive), so deriving the fragment
    from the filename silently dropped it and cut the card loose from every
    id-based check. The card's own `extensions.jai.id` is authoritative.
    """
    src = tmp_path / "Narrator_1911.png"
    src.write_bytes(
        _png_with_card(
            {
                "name": "Narrator",
                "description": "Aria waits by the door.",
                "extensions": {"jai": {"id": "1911", "sourceKind": "chub"}},
            }
        )
    )

    raw = src.read_bytes()
    envelope, data = pngtools.read_envelope(raw)
    assert fix_names._apply(src, raw, envelope, data, "Aria") is None
    assert (tmp_path / "Aria_1911.png").exists()
    assert not (tmp_path / "Aria.png").exists()


def test_apply_recovers_a_fragment_the_filename_lost(tmp_path):
    """SillyTavern's own Rename drops the `_<id8>` fragment. Renaming such a card
    re-derives it from the stamp, filing it back under an id-matchable name."""
    src = tmp_path / "Narrator.png"
    src.write_bytes(
        _png_with_card(
            {
                "name": "Narrator",
                "description": "Angelica waits.",
                "extensions": {"jai": {"id": "04355852-6c33-4e82-8cf2-3699f2ce4d92"}},
            }
        )
    )

    raw = src.read_bytes()
    envelope, data = pngtools.read_envelope(raw)
    assert fix_names._apply(src, raw, envelope, data, "Angelica") is None
    assert (tmp_path / "Angelica_04355852.png").exists()


# --------------------------------------------------------------------------
# ensemble suppression -- which questions it is allowed to answer
# --------------------------------------------------------------------------


def test_ensemble_excuses_a_generic_name_but_not_a_title(tmp_path, capsys, monkeypatch):
    """`Narrator` on a world card is fine; a scenario title never is."""
    world = (
        "Bot type: GM/narrator. The narrator voices the ship, the crew, and the "
        "world -- not a single person. It plays all the background characters. "
        "Sylthiel walks the deck. Azerai holds the wheel. Sylthiel's eyes narrow."
    )
    (tmp_path / "Narrator_aaaaaaaa.png").write_bytes(
        _png_with_card({"name": "Narrator", "description": world})
    )
    (tmp_path / "A_Mothers_Claim_bbbbbbbb.png").write_bytes(
        _png_with_card({"name": "A Mother's Claim, A Daughter's Hunger", "description": world})
    )

    monkeypatch.setattr("sys.argv", ["fix_names", "--dir", str(tmp_path), "--limit", "0"])
    fix_names.main()
    out = capsys.readouterr().out

    assert "1 card(s) to review" in out
    assert "A_Mothers_Claim_bbbbbbbb.png" in out  # title name still reviewed
    assert "Narrator_aaaaaaaa.png" not in out  # generic name excused
    assert "1 hidden -- 1 GENERIC" in out


# --------------------------------------------------------------------------
# --prune -- a deleted card was never winnable, so it must not be scored
# --------------------------------------------------------------------------


def test_prune_moves_decisions_about_deleted_cards_to_a_sidecar(tmp_path, monkeypatch, capsys):
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "Ana_aaaaaaaa.png").write_bytes(_png_with_card({"name": "Ana"}))

    log = tmp_path / "log.jsonl"
    _write_log(log, [
        _decision("2026-08-11T01:00:00+00:00", "Tagline_Ana_aaaaaaaa.png", "accept", "T | Ana", "Ana", "id-a"),
        _decision("2026-08-11T01:05:00+00:00", "Tomboy_GF_CUCKS_You_bbbbbbbb.png", "fine", "Tomboy GF CUCKS You", None, "id-b"),
    ])
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert fix_names._prune(log, cards) == 0

    kept = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
    pruned = [json.loads(x) for x in (tmp_path / "log.pruned.jsonl").read_text().splitlines() if x.strip()]
    assert [r["card_id"] for r in kept] == ["id-a"]
    assert [r["card_id"] for r in pruned] == ["id-b"]  # preserved, not destroyed


def test_prune_keeps_a_card_that_was_only_renamed(tmp_path, monkeypatch, capsys):
    """The id fragment survives renames, so a rename is never a deletion."""
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "Ana_aaaaaaaa.png").write_bytes(_png_with_card({"name": "Ana"}))

    log = tmp_path / "log.jsonl"
    _write_log(log, [
        _decision("2026-08-11T01:00:00+00:00", "Some_Long_Tagline_aaaaaaaa.png", "accept", "T | Ana", "Ana", "id-a"),
    ])
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert fix_names._prune(log, cards) == 0
    assert "nothing to prune" in capsys.readouterr().out
    assert len(log.read_text().strip().splitlines()) == 1
