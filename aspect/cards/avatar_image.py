"""Crop and resize a card's avatar before it's embedded in the PNG.

JanitorAI creators sometimes composite several portraits top-to-bottom into
one very tall image (e.g. Akane Kujo's 3-panel stack, or a bio-card stack
with 4+ sections of uneven height -- the panels are NOT always equal
thirds). Stacks render terribly in SillyTavern's fixed avatar frame -- by
the time it's shrunk to fit, it's a narrow sliver.

Rather than assume a fixed panel count, a tall-enough image is scanned for
the thin, near-solid-color divider row creators composite between panels,
and cropped to wherever the first panel actually ends. If no divider is
found (composited without one, or too subtle to detect), it falls back to
the old top-third guess.

Some creators butt panels directly together with no divider at all, so the
scan can walk straight past the true panel-1/panel-2 boundary and stop at
the next real divider it finds (e.g. panel-2/panel-3), silently swallowing
an entire extra panel. There's no cheap, reliable way to detect a boundary
that has no visual marker, so as a backstop the crop is also capped to a
plausible single-portrait height -- whichever is smaller between the
detected break and the cap wins. This bounds the damage from a missed
divider; it doesn't guarantee a pixel-perfect crop.

Everything is then downscaled (never upscaled) so its longest side is at
most _MAX_DIMENSION.
"""

from __future__ import annotations

from PIL import Image

# height / width at or above this is treated as a stack of composited
# portraits rather than a single one. A single portrait tops out around
# 1.78 (9:16); stacked portraits land well above that.
_STACK_RATIO = 2.0
_MAX_DIMENSION = 1920

# Backstop cap on the cropped panel's own height/width, independent of
# whatever the seam scan finds -- a touch above the normal single-portrait
# ceiling noted above.
_MAX_PANEL_RATIO = 1.8

# Divider rows between panels are near-solid color (almost always black) and
# essentially flat across the full width. A tight std cutoff means a merely
# dark/low-contrast patch of real content (dark hair, a shadow) won't be
# mistaken for one -- those still vary pixel-to-pixel far more than a
# deliberate divider does.
_SEAM_SAMPLE_WIDTH = 48
_SEAM_MAX_MEAN = 25
_SEAM_MAX_STD = 5
_SEAM_MIN_THICKNESS = 4
_MIN_PANEL_FRACTION = 0.05


def _find_first_panel_break(image: Image.Image) -> int | None:
    """Return the row where the first composited panel ends, if detectable."""
    width, height = image.size
    sample_width = min(_SEAM_SAMPLE_WIDTH, width)
    gray = image.convert("L").resize((sample_width, height), Image.BILINEAR)
    pixels = gray.load()

    def is_seam_row(y: int) -> bool:
        values = [pixels[x, y] for x in range(sample_width)]
        mean = sum(values) / sample_width
        if mean >= _SEAM_MAX_MEAN:
            return False
        variance = sum((v - mean) ** 2 for v in values) / sample_width
        return variance**0.5 <= _SEAM_MAX_STD

    # A seam touching row 0 is the card's outer frame, not a divider between
    # panels -- skip past it before looking for the first real break.
    y = 0
    while y < height and is_seam_row(y):
        y += 1

    while y < height:
        if not is_seam_row(y):
            y += 1
            continue
        band_start = band_end = y
        while band_end + 1 < height and is_seam_row(band_end + 1):
            band_end += 1
        if band_end - band_start + 1 >= _SEAM_MIN_THICKNESS:
            return band_start
        y = band_end + 1

    return None


def normalize_avatar(image: Image.Image) -> Image.Image:
    width, height = image.size
    if width > 0 and height / width >= _STACK_RATIO:
        break_row = _find_first_panel_break(image)
        if break_row is None or break_row < height * _MIN_PANEL_FRACTION:
            break_row = height // 3
        break_row = min(break_row, round(width * _MAX_PANEL_RATIO))
        image = image.crop((0, 0, width, break_row))
        width, height = image.size

    longest = max(width, height)
    if longest > _MAX_DIMENSION:
        scale = _MAX_DIMENSION / longest
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        image = image.resize(new_size, Image.LANCZOS)

    return image
