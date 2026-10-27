"""The catch-up pass that gives already-built cards a gallery_id.

Cards written before gallery_id existed carry none, so CharacterLibrary would
fold every same-named character into one gallery folder. This script patches the
id in *in place* -- the contract being that it touches nothing else: pixels
byte-identical, every other card field untouched, and an id already on a card
left exactly as it is (it may already point at a folder full of images).
"""

import base64
import io
import json
import sys
from pathlib import Path

from PIL import Image

import scripts.backfill_gallery_ids as backfill
from aspect.cards import pngtools


def _png_bytes(colour: tuple[int, int, int, int] = (10, 20, 30, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), colour).save(buf, "PNG")
    return buf.getvalue()


def _card_png(data: dict) -> bytes:
    envelope = {"spec": "chara_card_v3", "spec_version": "3.0", "data": data}
    envelope.update(data)
    payload = base64.b64encode(json.dumps(envelope).encode()).decode("ascii")
    return pngtools.inject_text_chunks(_png_bytes(), {"chara": payload, "ccv3": payload})


def _write_card(cards_dir: Path, rel: str, data: dict) -> Path:
    path = cards_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_card_png(data))
    return path


def _read_data(path: Path) -> dict:
    return pngtools.extract_embedded_card(path.read_bytes())


def _run(cards_dir: Path, monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["backfill_gallery_ids", "--cards-dir", str(cards_dir), *args])
    return backfill.main()


def test_apply_stamps_missing_ids_and_leaves_existing_ones(tmp_path, monkeypatch):
    cards = tmp_path / "cards"
    missing = _write_card(cards, "dezea/Akane_bffaaf71.png", {"name": "Akane", "extensions": {}})
    has_id = _write_card(
        cards, "dezea/Tsuko_1a2b3c4d.png", {"name": "Tsuko", "extensions": {"gallery_id": "KEEPME01234x"}}
    )
    before_bytes = has_id.read_bytes()

    assert _run(cards, monkeypatch, "--apply") == 0

    gid = _read_data(missing)["extensions"]["gallery_id"]
    assert len(gid) == 12 and gid.isalnum()
    assert has_id.read_bytes() == before_bytes  # untouched, byte for byte


def test_apply_preserves_pixels_and_every_other_field(tmp_path, monkeypatch):
    cards = tmp_path / "cards"
    data = {
        "name": "Akane",
        "description": "d",
        "character_book": {"entries": [{"keys": ["k"], "content": "c", "position": 0}]},
        "extensions": {"jai": {"id": "abc"}, "depth_prompt": {"depth": 4}},
    }
    path = _write_card(cards, "dezea/Akane_bffaaf71.png", data)
    pixels_before = pngtools.non_text_chunks(path.read_bytes())

    assert _run(cards, monkeypatch, "--apply") == 0

    after = _read_data(path)
    assert pngtools.non_text_chunks(path.read_bytes()) == pixels_before
    assert after["extensions"].pop("gallery_id")
    assert after == data  # nothing else moved, lorebook int position included


def test_report_mode_writes_nothing(tmp_path, monkeypatch):
    cards = tmp_path / "cards"
    path = _write_card(cards, "dezea/Akane_bffaaf71.png", {"name": "Akane", "extensions": {}})
    before = path.read_bytes()

    assert _run(cards, monkeypatch) == 0
    assert path.read_bytes() == before


def test_rerun_is_a_no_op(tmp_path, monkeypatch):
    cards = tmp_path / "cards"
    path = _write_card(cards, "dezea/Akane_bffaaf71.png", {"name": "Akane", "extensions": {}})

    assert _run(cards, monkeypatch, "--apply") == 0
    first = path.read_bytes()
    assert _run(cards, monkeypatch, "--apply") == 0
    assert path.read_bytes() == first  # same id, same bytes


def test_unreadable_png_is_skipped_not_fatal(tmp_path, monkeypatch):
    cards = tmp_path / "cards"
    stray = cards / "dezea" / "not_a_card.png"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(_png_bytes())
    good = _write_card(cards, "dezea/Akane_bffaaf71.png", {"name": "Akane", "extensions": {}})

    assert _run(cards, monkeypatch, "--apply") == 0
    assert _read_data(good)["extensions"]["gallery_id"]
    assert pngtools.extract_embedded_card(stray.read_bytes()) is None


def test_missing_cards_dir_reports_failure(tmp_path, monkeypatch):
    assert _run(tmp_path / "nope", monkeypatch, "--apply") == 1
