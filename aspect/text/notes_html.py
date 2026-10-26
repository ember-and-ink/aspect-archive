"""Creator-notes cleanup that keeps a card's *layout* without keeping its CSS.

Most sources (JanitorAI, saucepan, datacat, JannyAI) author notes as a shallow
run of styled `<p>`/`<span>`, so flattening them to markdown via `html_to_md`
loses nothing and gains a lot. Chub is different: its notes are page-sized
documents with a `<style>` sheet, CSS grids and absolutely-positioned
decoration. Flattening one of those drops every column and panel boundary and
leaves an unreadable run of text.

Keeping the CSS isn't an option either. SillyTavern warns on any `<style>` in
creator notes ("Creator's Notes contain CSS style tags...") and, worse, a
`position:fixed` decoration inside the notes escapes the drawer and paints over
the whole app -- inline styles are not scoped by that prompt at all.

So `tame_html` keeps the *structure* and discards the *palette*:

  * the `<style>` sheet is resolved into inline `style` attributes and deleted,
    so the ST prompt can never fire (it only ever inspects `<style>` elements --
    see SillyTavern/public/scripts/chats.js `getStyleContentsFromMarkdown`);
  * only layout/spacing declarations survive, so grids and panels hold their
    shape while colours come from the reader's ST theme. That is also why this
    is the safer choice: these blurbs hard-code dark backgrounds that would
    render as dark slabs inside a light theme;
  * anything that can't lay out in a ~400px drawer -- fixed positioning, page
    sized widths, hard-coded column counts -- is dropped or made responsive.

`clean_creator_notes` is the entry point every mapper uses: it routes notes
with real layout here and everything else to the markdown flattener, so the
existing sources keep their current (good) output.
"""

from __future__ import annotations

import re

import tinycss2
from bs4 import BeautifulSoup
from bs4.element import Tag
from soupsieve import SelectorSyntaxError

from aspect.text.html_md import html_to_md

# Declarations that hold a layout together. Everything else -- colour,
# background, shadow, font family, filter -- is dropped: the card's palette is
# not worth the risk of fighting the reader's theme.
_STRUCTURE_PROPS = frozenset(
    {
        "display", "grid-template-columns", "grid-template-rows", "grid-column",
        "grid-row", "grid-area", "gap", "row-gap", "column-gap", "flex",
        "flex-direction", "flex-wrap", "justify-content", "align-items",
        "align-content", "align-self", "order",
        "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
        "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
        "text-align", "list-style", "list-style-type", "font-weight", "font-style",
        "white-space", "word-break", "overflow-wrap", "box-sizing",
    }
)

# Whatever made an element read as its own block (a fill, a frame) is replaced
# by one theme-neutral rule so the boundary survives the loss of the palette;
# mid-grey reads correctly against both light and dark themes. A single-sided
# border stays single-sided -- an underlined heading must not become a box.
_PANEL_FILLS = ("background", "background-color", "background-image", "border", "box-shadow")
_PANEL_SIDES = ("border-top", "border-right", "border-bottom", "border-left")
_NEUTRAL_LINE = "1px solid rgba(127,127,127,0.35)"
_PANEL_STYLE = f"border:{_NEUTRAL_LINE};border-radius:6px"

# Positioning that pulls an element out of normal flow. Its presence also marks
# the element as decoration or chrome rather than prose -- see _is_decorative.
_OUT_OF_FLOW = frozenset({"absolute", "fixed", "sticky"})

# Widths survive only when relative: a blurb authored for a 1100px page column
# must not blow out the drawer. Heights are dropped outright -- each was written
# against an ancestor height that is now gone, and a dangling `height:100%`
# resolves against an indefinite box, which is what makes an <img> spill over
# the block after it.
_WIDTH_PROPS = frozenset({"width", "max-width", "min-width"})
_HEIGHT_PROPS = frozenset({"height", "max-height", "min-height"})
_RELATIVE_SIZE = re.compile(r"^(100%|auto|fit-content|max-content|min-content|none)$", re.I)

# `repeat(auto-fit, minmax(400px, 1fr))` reads fine on a wide page and overflows
# a narrow drawer; min(Npx, 100%) keeps the intent and bounds it. A hard-coded
# track list can't reflow at all, so it becomes auto-fit -- multi-column where
# there is room, single column where there isn't.
_MINMAX_RE = re.compile(r"minmax\(\s*(\d+(?:\.\d+)?)(px|rem|em)\s*,", re.IGNORECASE)
_FIXED_REPEAT_RE = re.compile(r"^repeat\(\s*(\d+)\s*,\s*1fr\s*\)$", re.IGNORECASE)
_FIXED_TRACKS_RE = re.compile(r"^\s*1fr(\s+1fr)+\s*$", re.IGNORECASE)
_MIN_TRACK_FOR_COLS = {2: 220, 3: 150}

# Selectors with no inline equivalent (pseudo-elements, interaction states) or
# that target markup which doesn't exist inside the drawer -- `body`/`html` and
# the host site's own chrome, since Chub renders notes inside an Ant Design grid.
_UNINLINABLE = re.compile(
    r"::|:hover|:focus|:active|:visited|:target|:checked|:first-line|:before|:after"
    r"|(^|[\s,>+~])(body|html)\b|:root|\.ant-|\.char-card-class",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"url\(", re.IGNORECASE)
_BG_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.IGNORECASE)
_BG_AUX_PROPS = ("background-size", "background-position", "background-repeat", "background-attachment")
_WORD_RE = re.compile(r"\w")
_LAYOUT_HINT_RE = re.compile(r"display\s*:\s*(grid|flex)|grid-template", re.IGNORECASE)

_DROP_TAGS = ("style", "script", "noscript", "template", "head", "link", "meta",
              "iframe", "object", "embed")
_DROP_ATTRS = ("class", "id", "srcset", "sizes", "loading", "data-src")


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def has_layout_structure(html: str) -> bool:
    """True when `html` carries layout worth preserving -- a stylesheet or an
    explicit grid/flex container. Verified against the fixture corpus: every
    JanitorAI / datacat / JannyAI blurb scores False (they are shallow styled
    prose, better off as markdown) and the Chub ones score True."""
    if not html:
        return False
    if _LAYOUT_HINT_RE.search(html):
        return True
    return any(
        s.get_text().strip()
        for s in BeautifulSoup(html, "html.parser").find_all("style")
    )


def clean_creator_notes(html: str) -> str:
    """Normalize a creator-notes blurb for SillyTavern: laid-out HTML is tamed
    in place, everything else is flattened to markdown."""
    if not html or not html.strip():
        return ""
    return tame_html(html) if has_layout_structure(html) else html_to_md(html)


# ---------------------------------------------------------------------------
# stylesheet -> inline
# ---------------------------------------------------------------------------


def _specificity(selector: str) -> tuple[int, int, int]:
    return (
        len(re.findall(r"#[\w-]+", selector)),
        len(re.findall(r"[.\[:][\w-]+", selector)),
        len(re.findall(r"(^|[\s>+~])([a-zA-Z][\w-]*)", selector)),
    )


def _declarations(content) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for decl in tinycss2.parse_blocks_contents(content):
        if decl.type != "declaration":
            continue
        value = tinycss2.serialize(decl.value).strip()
        if value:
            out.append((decl.lower_name, value))
    return out


def _collect_rules(css: str) -> list[tuple[tuple[int, int, int], int, str, list[tuple[str, str]]]]:
    """Flatten a stylesheet into (specificity, source order, selector, decls).
    At-rules (@keyframes/@media/@font-face/@import) have no inline form and are
    skipped wholesale."""
    rules = []
    for order, node in enumerate(
        tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    ):
        if node.type != "qualified-rule":
            continue
        decls = _declarations(node.content)
        if not decls:
            continue
        for selector in tinycss2.serialize(node.prelude).strip().split(","):
            selector = selector.strip()
            if selector and not _UNINLINABLE.search(selector):
                rules.append((_specificity(selector), order, selector, decls))
    return rules


def _inline_stylesheets(soup: BeautifulSoup) -> None:
    """Resolve every `<style>` block against the tree, writing the winning
    declarations onto each element's `style` attribute (an authored inline style
    still wins, as in the real cascade). The blocks themselves are removed by
    the caller. Without this, class-based layout -- which is where Chub puts its
    grids -- would vanish along with the stylesheet."""
    css = "\n".join(s.get_text() for s in soup.find_all("style"))
    if not css.strip():
        return

    computed: dict[int, dict[str, str]] = {}
    for _spec, _order, selector, decls in sorted(_collect_rules(css), key=lambda r: (r[0], r[1])):
        try:
            matches = soup.select(selector)
        except (SelectorSyntaxError, NotImplementedError, ValueError):
            continue  # a selector soupsieve can't parse simply doesn't apply
        for el in matches:
            computed.setdefault(id(el), {}).update(decls)

    for el in soup.find_all(True):
        bucket = computed.get(id(el))
        if not bucket:
            continue
        merged = dict(bucket)
        merged.update(_parse_decls(el.get("style") or ""))
        el["style"] = ";".join(f"{k}:{v}" for k, v in merged.items() if not k.startswith("--"))


# ---------------------------------------------------------------------------
# declaration filtering
# ---------------------------------------------------------------------------


def _parse_decls(style: str) -> list[tuple[str, str]]:
    out = []
    for decl in style.split(";"):
        name, sep, value = decl.partition(":")
        if not sep:
            continue
        name = name.strip().lower()
        # `!important` inline would beat SillyTavern's own theme rules.
        value = re.sub(r"\s*!\s*important\s*$", "", value.strip(), flags=re.I)
        if name and value:
            out.append((name, value))
    return out


def _background_image_url(decls: list[tuple[str, str]]) -> str | None:
    """The literal image behind a `background`/`background-image` declaration,
    if any. A gradient or flat colour is palette and gets dropped, but Chub
    blurbs also use this shorthand to lay in an actual character image (see
    the "FULL WIDTH WRAPPER WITH BACKGROUND IMAGE" pattern) -- that is content,
    not decoration, and must survive alongside the panel border."""
    url = None
    for name, value in decls:
        if name in ("background", "background-image"):
            if m := _BG_URL_RE.search(value):
                url = m.group(2)
    return url


def _panel_style(decls: list[tuple[str, str]]) -> str:
    """The theme-neutral stand-in for whatever made this element a distinct
    block, or "" when nothing did."""
    if any(name in _PANEL_FILLS for name, _ in decls):
        return _PANEL_STYLE
    sides = [name for name, _ in decls if name in _PANEL_SIDES]
    return ";".join(f"{side}:{_NEUTRAL_LINE}" for side in dict.fromkeys(sides))


def _responsive_grid(value: str) -> str:
    value = value.strip()
    if m := _FIXED_REPEAT_RE.match(value):
        cols = int(m.group(1))
    elif _FIXED_TRACKS_RE.match(value):
        cols = len(value.split())
    else:
        return _MINMAX_RE.sub(r"minmax(min(\1\2, 100%),", value)
    if cols < 2:
        return value
    return f"repeat(auto-fit,minmax(min({_MIN_TRACK_FOR_COLS.get(cols, 120)}px, 100%),1fr))"


def _filter_style(style: str, *, allow_media: bool) -> tuple[str, bool]:
    """Return (filtered declarations, whether the element was out of flow)."""
    decls = _parse_decls(style)
    kept: list[str] = []
    out_of_flow = False

    if panel := _panel_style(decls):
        kept.append(panel)

    if allow_media and (bg_url := _background_image_url(decls)):
        kept.append(f"background-image:url({bg_url})")
        for name, value in decls:
            if name in _BG_AUX_PROPS:
                kept.append(f"{name}:{value}")

    for name, value in decls:
        if name == "position":
            out_of_flow = value.lower().split()[0] in _OUT_OF_FLOW
            continue  # `relative` is pointless once its positioned children are gone
        if name == "opacity":
            continue  # a hover-reveal overlay must not stay invisible in flow
        if name in _HEIGHT_PROPS:
            continue
        if name in _WIDTH_PROPS:
            if _RELATIVE_SIZE.match(value):
                kept.append(f"{name}:{value}")
            continue
        if name not in _STRUCTURE_PROPS:
            continue
        if not allow_media and _URL_RE.search(value):
            continue
        if name.startswith("grid-template"):
            value = _responsive_grid(value)
        kept.append(f"{name}:{value}")

    return ";".join(kept), out_of_flow


def _is_decorative(tag: Tag, *, out_of_flow: bool = False) -> bool:
    """A subtree carrying no words and no image is pure CSS decoration -- an
    empty `<div>` that only held a gradient, or (when it was positioned out of
    flow) a lone glyph like a floating rune. An in-flow glyph run may be an
    author's divider, so only out-of-flow ones are judged on words."""
    if tag.name in ("img", "br", "hr"):
        return False
    if tag.find("img") is not None:
        return False
    if _background_image_url(_parse_decls(tag.get("style") or "")):
        return False
    text = tag.get_text(strip=True)
    return not _WORD_RE.search(text) if out_of_flow else not text


def tame_html(html: str, *, allow_media: bool = True) -> str:
    """Return `html` with its stylesheet inlined, its palette dropped and its
    layout bounded to a narrow drawer. See the module docstring."""
    soup = BeautifulSoup(html, "html.parser")

    _inline_stylesheets(soup)
    for bad in soup.find_all(_DROP_TAGS):
        bad.decompose()

    for tag in list(soup.find_all(True)):
        if tag.parent is None:
            continue  # already removed along with an ancestor
        if style := tag.get("style"):
            filtered, out_of_flow = _filter_style(style, allow_media=allow_media)
            if out_of_flow and _is_decorative(tag, out_of_flow=True):
                tag.decompose()
                continue
            if filtered:
                tag["style"] = filtered
            else:
                del tag["style"]
        for attr in list(tag.attrs):
            # class survives nowhere useful -- SillyTavern rewrites every class
            # to `custom-*` (chats.js) and the stylesheet that named them is gone.
            if attr in _DROP_ATTRS or attr.startswith("on"):
                del tag[attr]

    for tag in list(soup.find_all(True)):
        if tag.parent is not None and _is_decorative(tag):
            tag.decompose()

    # SillyTavern bounds images in `.mes_text` but *not* in #creator_notes_spoiler
    # (public/style.css), so an unconstrained Chub image overflows the drawer.
    # Rebuilt from the parsed declarations rather than appended, so re-taming an
    # already-tamed blurb doesn't stack duplicate bounds.
    for img in soup.find_all("img"):
        decls = dict(_parse_decls(img.get("style") or ""))
        decls["max-width"] = "100%"
        decls["height"] = "auto"
        img["style"] = ";".join(f"{k}:{v}" for k, v in decls.items())

    return re.sub(r"\n{3,}", "\n\n", str(soup)).strip()
