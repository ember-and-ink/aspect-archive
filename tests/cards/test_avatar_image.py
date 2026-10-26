from PIL import Image

from aspect.cards.avatar_image import normalize_avatar


def _image(width: int, height: int, color=(200, 100, 50, 255)) -> Image.Image:
    return Image.new("RGBA", (width, height), color)


def test_normalize_avatar_crops_detected_stack_to_top_third():
    # height/width == 3.0, well past the 2.0 stack threshold. Fill each third
    # with a distinct color so we can confirm the top panel survives.
    width, panel = 300, 300
    stacked = Image.new("RGBA", (width, panel * 3), (0, 0, 0, 255))
    stacked.paste(_image(width, panel, (10, 20, 30, 255)), (0, 0))
    stacked.paste(_image(width, panel, (40, 50, 60, 255)), (0, panel))
    stacked.paste(_image(width, panel, (70, 80, 90, 255)), (0, panel * 2))

    result = normalize_avatar(stacked)

    assert result.size == (width, panel)
    assert result.getpixel((0, 0)) == (10, 20, 30, 255)


def test_normalize_avatar_leaves_normal_portrait_uncropped():
    # 9:16 portrait -- ratio 1.78, under the 2.0 stack threshold.
    portrait = _image(900, 1600)
    result = normalize_avatar(portrait)
    assert result.size == (900, 1600)


def test_normalize_avatar_downscales_when_over_max_dimension():
    oversized = _image(4000, 2000)
    result = normalize_avatar(oversized)
    assert max(result.size) == 1920
    assert result.size == (1920, 960)


def test_normalize_avatar_does_not_upscale_small_images():
    small = _image(256, 256)
    result = normalize_avatar(small)
    assert result.size == (256, 256)


def test_normalize_avatar_crops_then_resizes_oversized_stack():
    width, panel = 1200, 1200
    stacked = _image(width, panel * 3)
    result = normalize_avatar(stacked)
    # Cropped to (1200, 1200) first, already within the 1920 cap.
    assert result.size == (1200, 1200)


def test_normalize_avatar_crop_result_still_downscaled_when_over_cap():
    width, panel = 2500, 2500
    stacked = _image(width, panel * 3)
    result = normalize_avatar(stacked)
    # Cropped to (2500, 2500), then downscaled so the longest side is 1920.
    assert result.size == (1920, 1920)


def test_normalize_avatar_crops_to_true_panel_break_with_uneven_panels():
    # A real-world bio-card stack: panels are NOT equal thirds, and a thin
    # black divider separates them. The old height//3 guess would slice into
    # panel two; seam detection should find the real, larger first panel.
    width = 300
    panel_heights = [520, 220, 210, 260]  # uneven, unlike a simple 3-stack
    divider = 6
    # Bright enough that no panel itself reads as a seam -- only the flat
    # black divider rows should.
    colors = [(60, 70, 80, 255), (90, 100, 110, 255), (120, 130, 140, 255), (150, 160, 170, 255)]

    total = sum(panel_heights) + divider * (len(panel_heights) - 1)
    stacked = Image.new("RGBA", (width, total), (0, 0, 0, 255))
    y = 0
    for i, (h, color) in enumerate(zip(panel_heights, colors)):
        stacked.paste(_image(width, h, color), (0, y))
        y += h
        if i < len(panel_heights) - 1:
            y += divider

    result = normalize_avatar(stacked)

    assert result.size == (width, panel_heights[0])
    assert result.getpixel((0, 0)) == colors[0]
    # The naive height // 3 guess would have landed inside panel one here too
    # by coincidence-free construction -- assert it actually differs so this
    # test would catch a regression to the old fixed-thirds behavior.
    assert panel_heights[0] != total // 3


def test_normalize_avatar_ignores_dark_content_as_a_false_seam():
    # A single panel with a dark (but not flat) band partway down should not
    # be mistaken for a divider -- only a near-solid-color band counts.
    width, height = 300, 900
    portrait = Image.new("RGBA", (width, height), (200, 100, 50, 255))
    dark_band = Image.new("RGBA", (width, 30), (20, 20, 20, 255))
    for y in range(30):
        for x in range(width):
            dark_band.putpixel((x, y), (20 + (x % 40), 20, 20, 255))
    portrait.paste(dark_band, (0, 400))

    result = normalize_avatar(portrait)

    # No real divider exists, so this should fall back to the old top-third
    # guess rather than cropping at the dark band.
    assert result.size == (width, height // 3)


def test_normalize_avatar_caps_break_row_when_no_divider_between_first_panels():
    # Some creators butt panels directly together with no divider at all --
    # the seam scan then walks straight past that true boundary and latches
    # onto the next real divider it finds, one whole panel too late. The
    # panel-height cap should catch this and keep the crop from ballooning.
    width = 300
    panel_one_height = 800  # no divider follows -- panel two starts right here
    panel_two_height = 900
    divider = 6

    stacked = Image.new("RGBA", (width, panel_one_height + panel_two_height + divider), (0, 0, 0, 255))
    stacked.paste(_image(width, panel_one_height, (60, 70, 80, 255)), (0, 0))
    stacked.paste(_image(width, panel_two_height, (90, 100, 110, 255)), (0, panel_one_height))
    # Real divider only between panel two and a (theoretical) panel three.
    stacked.paste(
        _image(width, panel_one_height, (120, 130, 140, 255)),
        (0, panel_one_height + panel_two_height + divider),
    )

    result = normalize_avatar(stacked)

    cap = round(width * 1.8)
    assert result.size[1] == cap
    assert result.size[1] < panel_one_height + panel_two_height
