"""Low-level PNG helpers: lossy quantization via the bundled `pngquant` binary
and raw tEXt-chunk (re)injection.

The two are a pair. `pngquant` shrinks a card's avatar dramatically (a 1.8 MB
JanitorAI PNG drops to ~700 KB) but *strips every ancillary chunk*, including
the `chara`/`ccv3` tEXt chunks that carry the character card. So the pipeline is:
normalize the avatar to PNG -> quantize the pixels -> re-inject the card text
chunks into the compressed bytes. Injection rewrites the raw chunk stream rather
than re-encoding through Pillow, so it preserves pngquant's optimized IDAT
exactly (re-saving via Pillow would inflate it back). Mirrors the approach in
../SillyTavern-Character-Tools-Server/src/transforms.ts.
"""

from __future__ import annotations

import base64
import json
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Any

from aspect.cards import dates

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Every flavour of textual chunk. We strip all of them before re-injecting so a
# quantized-then-injected file never carries stale duplicates.
_TEXT_CHUNK_TYPES = frozenset({b"tEXt", b"zTXt", b"iTXt"})


def _iter_chunks(data: bytes):
    """Yield (type, data) for each PNG chunk after the 8-byte signature. CRCs are
    dropped (recomputed on write); malformed trailing bytes are ignored."""
    pos = 8
    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        cdata = data[pos + 8 : pos + 8 + length]
        yield ctype, cdata
        pos += 12 + length


def _encode_chunk(ctype: bytes, cdata: bytes) -> bytes:
    body = ctype + cdata
    return struct.pack(">I", len(cdata)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _text_chunk(keyword: str, text: str) -> bytes:
    # tEXt is Latin-1; keyword + NUL + text. Card payloads are base64 (ASCII), a
    # strict subset, so this never lossily encodes.
    payload = keyword.encode("latin-1") + b"\x00" + text.encode("latin-1")
    return _encode_chunk(b"tEXt", payload)


def read_text_chunks(png: bytes) -> dict[str, str]:
    """Return the `tEXt` chunks of `png` as {keyword: text}. The inverse of
    inject_text_chunks -- used to read an already-embedded character card back
    out of a PNG (e.g. a datacat export in the import pipeline). Only plain
    `tEXt` is decoded; the card writers here (and datacat's) use tEXt, so
    compressed zTXt/iTXt variants are not expected and are skipped. Later
    chunks win on a duplicate keyword."""
    if png[:8] != _PNG_SIGNATURE:
        raise ValueError("not a PNG stream")
    out: dict[str, str] = {}
    for ctype, cdata in _iter_chunks(png):
        if ctype == b"tEXt":
            keyword, _, text = cdata.partition(b"\x00")
            out[keyword.decode("latin-1")] = text.decode("latin-1")
    return out


def read_envelope(png: bytes) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return `(envelope, data)` for the character card embedded in a tavern
    PNG, or None if it carries no readable card. Prefers the V3 `ccv3` chunk,
    falls back to the V2 `chara` chunk (card writers here, datacat, and Chub all
    write both with identical content). The chunk payload is base64(JSON); the
    JSON nests the card fields under `data` (with a top-level V2 mirror), so
    `data` is that nested object -- or the whole envelope when there's no `data`
    key.

    Keeping the outer envelope is what a *patching* caller needs: mutate `data`,
    then hand both back to embed_card to rewrite the card with its spec header
    intact. Readers that only want the fields want extract_embedded_card."""
    try:
        chunks = read_text_chunks(png)
    except ValueError:
        return None
    payload = chunks.get("ccv3") or chunks.get("chara")
    if not payload:
        return None
    try:
        obj = json.loads(base64.b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    data = obj.get("data")
    return obj, data if isinstance(data, dict) else obj


def extract_embedded_card(png: bytes) -> dict[str, Any] | None:
    """Return just the character-card `data` object embedded in a tavern PNG, or
    None if the PNG carries no readable card.

    The single reader shared by every import source (sources.datacat, sources.chub)
    -- the inverse of the card `to_dict`/inject_text_chunks the writers produce."""
    parsed = read_envelope(png)
    return parsed[1] if parsed is not None else None


def inject_text_chunks(png: bytes, texts: dict[str, str]) -> bytes:
    """Return `png` with `texts` written as tEXt chunks immediately after IHDR,
    stripping any pre-existing text chunks first. Placement before IDAT means
    Pillow (and SillyTavern/JanitorAI importers) surface them on open without a
    full decode."""
    if png[:8] != _PNG_SIGNATURE:
        raise ValueError("not a PNG stream")

    out = [_PNG_SIGNATURE]
    injected = False
    for ctype, cdata in _iter_chunks(png):
        if ctype in _TEXT_CHUNK_TYPES:
            continue  # drop stale text; the caller's chunks are authoritative
        out.append(_encode_chunk(ctype, cdata))
        if ctype == b"IHDR" and not injected:
            out.extend(_text_chunk(k, v) for k, v in texts.items())
            injected = True
    return b"".join(out)


def embed_card(png: bytes, envelope: dict[str, Any], data: dict[str, Any]) -> bytes:
    """Return `png` with `data` re-embedded under `envelope`'s spec header,
    preserving every non-text (pixel) chunk exactly -- inject_text_chunks
    rewrites only the tEXt chunks, so a pngquant-compressed IDAT survives
    byte-for-byte. Rebuilds the canonical envelope (spec header + V2 top-level
    mirror) CharacterCardV3.to_dict emits, so a patched card is shaped exactly
    like a freshly built one. The write half of read_envelope -- used by the
    in-place card patchers in scripts/."""
    new_envelope = {
        "spec": envelope.get("spec", "chara_card_v3"),
        "spec_version": envelope.get("spec_version", "3.0"),
        "data": data,
    }
    new_envelope.update(data)  # V2-compat top-level mirror
    # After the mirror, because `create_date` is a root-only field: it lives
    # outside `data`, so rebuilding the envelope from the spec header plus data
    # drops it unless it is carried across explicitly. Resolved against the
    # *incoming* envelope, not the one being built -- the value being preserved
    # is the old card's, and reading the new dict would find nothing and
    # silently re-derive a different date on every patch.
    create_date = dates.resolve_create_date(envelope, data)
    if create_date:
        new_envelope[dates.CREATE_DATE_KEY] = create_date
    payload = base64.b64encode(json.dumps(new_envelope).encode("utf-8")).decode("ascii")
    return inject_text_chunks(png, {"chara": payload, "ccv3": payload})


def image_size(png: bytes) -> tuple[int, int]:
    """Width and height out of the IHDR, which is always the first chunk -- no
    decode, so the index can afford it on every card. `(0, 0)` for anything
    that is not a PNG."""
    if len(png) < 24 or png[:8] != _PNG_SIGNATURE or png[12:16] != b"IHDR":
        return (0, 0)
    width, height = struct.unpack(">II", png[16:24])
    return (width, height)


def non_text_chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    """Every chunk of `png` that isn't text -- i.e. the pixels and headers. An
    in-place card patch must leave this list byte-identical; comparing it before
    and after is how scripts/ prove a rewrite touched only the embedded card."""
    return [(t, d) for t, d in _iter_chunks(png) if t not in _TEXT_CHUNK_TYPES]


def quantize(png: bytes, pngquant_bin: Path, *, timeout: float = 60.0) -> bytes | None:
    """Lossily quantize `png` with pngquant, reading stdin and writing stdout.
    Returns the smaller PNG, or None when quantization was skipped or unavailable
    so the caller keeps the original:

      * exit 98  -- `--skip-if-larger`: the palette version wasn't smaller
      * exit 99  -- quality floor not met (only if a --quality is passed)
      * binary missing / not executable / timeout / any other non-zero exit
    """
    try:
        proc = subprocess.run(
            [str(pngquant_bin), "--skip-if-larger", "--strip", "-"],
            input=png,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None

    if proc.returncode == 0 and proc.stdout[:8] == _PNG_SIGNATURE:
        return proc.stdout
    return None
