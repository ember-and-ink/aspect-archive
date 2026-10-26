from __future__ import annotations

import base64
import io
import json
import shutil
from collections.abc import Container, Iterable
from pathlib import Path
from typing import Any

from PIL import Image

from aspect.cards import dates, gallery, pngtools
from aspect.cards.naming import id_fragment, safe_filename
from aspect.cards.avatar_image import normalize_avatar
from aspect.config import settings
from aspect.text.macros import MacroSanitizer
from aspect.text.tags import normalize_tags
from aspect.cards.models import CaptureRecord, CharacterBook, CharacterCardV3, ProfileFields

def _gallery_id_on_disk(path: Path) -> Any | None:
    """The gallery_id of the card already written at `path`, if there is one.
    Re-exporting a character overwrites its card, and a fresh id there would
    orphan the gallery folder SillyTavern-CharacterLibrary already keyed to the
    old one -- so the id is carried across the overwrite."""
    if not path.exists():
        return None
    try:
        data = pngtools.extract_embedded_card(path.read_bytes())
    except OSError:
        return None
    return gallery.read_id(data) if data else None


def _stamp_gallery_id(card_payload: dict, path: Path) -> None:
    """Ensure the card carries an `extensions.gallery_id` before it's embedded,
    so it lands in its own gallery folder in SillyTavern-CharacterLibrary (see
    aspect/cards/gallery.py). Precedence: an id the payload already has (an import
    passing its source's extensions through) > the id on the card being
    overwritten > a freshly minted one. Mutates the payload in place, keeping
    the envelope's V2 top-level mirror in step with `data`."""
    data = card_payload.get("data")
    if not isinstance(data, dict):
        data = card_payload
    if gallery.read_id(data) is not None:
        return
    gallery.ensure_id(data, preferred=_gallery_id_on_disk(path))
    if data is not card_payload:
        card_payload["extensions"] = data["extensions"]


def _pick(visible: str, hidden: str) -> str:
    """Visible DOM value wins when non-empty; the captured hidden-definition
    value fills the gap. That's the whole point for hidden cards."""
    return visible if visible.strip() else hidden


class CardBuilder:
    """Assembles a CharacterCardV3 from a parsed profile (DOM), converted
    greetings, an optional hidden-definition capture, and an optional
    lorebook. Runs every text field through MacroSanitizer and collects the
    resulting warnings."""

    def __init__(self, sanitizer: MacroSanitizer | None = None) -> None:
        self._sanitizer = sanitizer or MacroSanitizer(user_names=settings.user_names)

    def build(
        self,
        profile: ProfileFields,
        greetings: list[str],
        capture: CaptureRecord | None,
        book: CharacterBook | None,
        avatar_url: str | None = None,
    ) -> tuple[CharacterCardV3, list[str]]:
        warnings: list[str] = []

        def sanitize(text: str) -> str:
            cleaned, unknown = self._sanitizer.sanitize(text)
            for macro_name in unknown:
                w = f"unresolved macro: {{{{{macro_name}}}}}"
                if w not in warnings:
                    warnings.append(w)
            return cleaned

        def desub(text: str) -> str:
            return self._sanitizer.reverse_names(text)

        description = sanitize(_pick(profile.description, desub(capture.personality) if capture else ""))
        scenario = sanitize(_pick(profile.scenario, desub(capture.scenario) if capture else ""))
        mes_example = sanitize(_pick(profile.mes_example, desub(capture.mes_example) if capture else ""))
        creator_notes = sanitize(profile.creator_notes)

        first_mes = sanitize(desub(greetings[0])) if greetings else ""
        alternate_greetings = [sanitize(desub(g)) for g in greetings[1:]]

        name = profile.name or (capture.name if capture else "") or "Unknown"

        if avatar_url:
            # Leads creator_notes with the original (uncropped, unresized)
            # avatar, echoing the character sidebar JanitorAI shows next to
            # these notes -- and keeps a clean reference to it before the
            # embedded avatar gets cropped/downscaled for SillyTavern.
            creator_notes = f"![{name}]({avatar_url})\n\n{creator_notes}"

        if not description and not scenario and not mes_example:
            warnings.append("no description/scenario/example dialogs found")
        if not first_mes:
            warnings.append("no first_mes / greetings found")

        card = CharacterCardV3(
            name=name,
            description=description,
            personality="",
            scenario=scenario,
            mes_example=mes_example,
            first_mes=first_mes,
            alternate_greetings=alternate_greetings,
            creator=profile.creator,
            creator_notes=creator_notes,
            tags=normalize_tags(profile.tags),
            character_book=book,
        )
        return card, warnings


class PngWriter:
    """Embeds a CharacterCardV3 as base64(JSON) into the `chara` (V2) and
    `ccv3` (V3) tEXt chunks of the avatar PNG -- replacing the userscript's
    hand-rolled JS CRC32/canvas/base64 machinery entirely.

    The avatar is first normalized to PNG and (when enabled) lossily quantized
    with pngquant to keep card files small, then the card chunks are injected
    into the compressed bytes. See aspect/cards/pngtools.py for the mechanics."""

    def __init__(
        self,
        output_dir: Path | None = None,
        compress: bool | None = None,
        pngquant_bin: Path | None = None,
        layout: str | None = None,
    ) -> None:
        self._output_dir = output_dir or settings.archive_dir
        self._compress = settings.compress if compress is None else compress
        self._pngquant_bin = self._resolve_pngquant(pngquant_bin or settings.pngquant_bin)
        self._layout = layout or settings.card_layout

    @staticmethod
    def _resolve_pngquant(configured: Path) -> Path | None:
        """The vendored binary if present, else one on PATH, else None (which
        disables compression -- writing falls back to the unquantized PNG)."""
        if configured.exists():
            return configured
        found = shutil.which("pngquant")
        return Path(found) if found else None

    def write(
        self,
        card: CharacterCardV3,
        avatar_png: bytes,
        out_dir: Path | None = None,
        card_id: str | None = None,
    ) -> Path:
        return self.write_payload(
            card.to_dict(),
            avatar_png,
            creator=card.creator,
            name=card.name,
            out_dir=out_dir,
            card_id=card_id,
        )

    def write_payload(
        self,
        card_payload: dict,
        avatar_png: bytes,
        *,
        creator: str,
        name: str,
        out_dir: Path | None = None,
        card_id: str | None = None,
        filename: str | None = None,
        normalize: bool = True,
    ) -> Path:
        """Embed an already-assembled card envelope (the {spec, spec_version,
        data, ...V2 mirror} dict a CharacterCardV3.to_dict produces) into the
        avatar and write it. `write` routes a CharacterCardV3 through here; the
        import pipeline uses it directly for a Chub card that's passed through as
        a raw dict (so its lorebook extras and int positions survive untouched).

        `filename` overrides the derived `<name>_<id8>.png`. Only an overwrite
        has any business passing it: a card renamed on disk (`make names`) still
        answers to its fragment, and writing it back under the *derived* name
        would leave two files sharing one id -- which is the archive's dedupe key.

        `normalize` runs the intake image pipeline (normalize, crop, cap,
        quantize) over `avatar_png`. Pass False when those bytes are already a
        card of ours being re-adopted: the crop and the quantizer are both
        one-way, so a second pass degrades an image that is already correct. The
        card chunks are then injected into the given PNG stream unchanged.
        """
        # Every card is named <name>_<id8>.png -- the id fragment disambiguates
        # two cards sharing a name, so the whole archive fits in one directory
        # without collisions. That flat layout is the default because
        # SillyTavern reads its characters folder non-recursively; `nested` adds
        # back the original <creator>/ level. Re-exporting the same card yields
        # the same path and overwrites -- note this is the low-level write: the
        # build endpoints skip a card that's already on disk before they ever get
        # here (see server._assemble_and_write), so reaching this with an
        # existing file means a caller that means it (import backfill, repair).
        out_dir = out_dir or self._output_dir
        target_dir = out_dir
        if self._layout == "nested":
            target_dir = out_dir / (safe_filename(creator) if creator.strip() else "unknown_creator")
        target_dir.mkdir(parents=True, exist_ok=True)

        if filename is None:
            fragment = id_fragment(card_id)
            stem = safe_filename(name)
            filename = f"{stem}_{fragment}.png" if fragment else f"{stem}.png"
        path = target_dir / filename

        # Every card leaves here with a gallery_id -- resolved against the card
        # this write may be overwriting, hence after the path is known.
        _stamp_gallery_id(card_payload, path)

        # ...and with a root-level `create_date`, derived from the provenance the
        # importer just stamped. SillyTavern writes this field on every card and
        # sorts on it; so does CharacterLibrary. See aspect/cards/dates.py.
        dates.stamp(card_payload)

        # Normalize whatever the avatar is (webp/jpg/png) to PNG bytes, crop a
        # detected 3-image stack down to its primary portrait and cap the
        # longest side, then optionally quantize. pngquant strips text chunks,
        # so the card is injected last -- directly into the (compressed) byte
        # stream.
        if normalize:
            image = Image.open(io.BytesIO(avatar_png)).convert("RGBA")
            image = normalize_avatar(image)
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
            image_bytes = buffer.getvalue()

            if self._compress and self._pngquant_bin is not None:
                quantized = pngtools.quantize(image_bytes, self._pngquant_bin)
                if quantized is not None:
                    image_bytes = quantized
        else:
            image_bytes = avatar_png

        payload = base64.b64encode(json.dumps(card_payload).encode("utf-8")).decode("ascii")
        image_bytes = pngtools.inject_text_chunks(image_bytes, {"chara": payload, "ccv3": payload})

        path.write_bytes(image_bytes)
        return path

    def existing(
        self,
        card_ids: Iterable[str],
        out_dir: Path | None = None,
        ignore: Container[Path] = (),
    ) -> set[str]:
        """The subset of card_ids whose card PNG is already on disk.

        Matches on the id fragment -- the `_<id8>` disambiguator every card
        filename carries -- because that fragment is knowable from a list row
        before the real character name is (the name comes from a per-card
        fetch, the id doesn't). So a bulk export can drop cards already saved
        without fetching them. Ids with no usable fragment never match (we
        can't key on a name we don't have here), so they're re-exported.

        `ignore` excludes paths from counting as a match -- for the orphan pass
        in scripts/import_cards.py, whose inputs *live in* out_dir: an orphan
        that already happens to be named `<name>_<id8>.png` would otherwise
        match itself and be skipped as "already imported" forever."""
        out_dir = out_dir or self._output_dir
        found: set[str] = set()
        for card_id in card_ids:
            fragment = id_fragment(card_id)
            if not fragment:
                continue
            if any(p not in ignore for p in out_dir.glob(f"**/*_{fragment}.png")):
                found.add(card_id)
        return found

    def find_by_id(self, card_id: str | None, out_dir: Path | None = None) -> list[Path]:
        """On-disk card PNGs carrying this card's `_<id8>` fragment, whatever
        name they were written (or renamed) under. `existing` answers the same
        question in bulk as a set of ids; this hands back the actual file, which
        is what a skipped build needs to report back."""
        fragment = id_fragment(card_id)
        if not fragment:
            return []
        out_dir = out_dir or self._output_dir
        return sorted(out_dir.glob(f"**/*_{fragment}.png"))

    # There is deliberately no name+fragment lookup here. One existed, to pin a
    # gallery_id backfill to a card matching both the name and the id. It was a
    # trap: `make names` renames a card in place (`Narrator_04355852.png` ->
    # `Angelica_04355852.png`) while the staged export it is matched against
    # still carries the old name, so the extra name term made the lookup miss
    # the exact card `existing` had just matched by id. Match on the fragment --
    # it is unique on its own across the archive. See `find_by_id`.
