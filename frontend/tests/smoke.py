#!/usr/bin/env python
"""Boot-and-browse smoke test for the React archive client.

The successor to `web/tests/smoke.py`, which drove the vendored frontend and
died with it at the cut-over (the file is on the `legacy-web` branch). Kept
as a real browser gate rather than folded into vitest because that is what it is
for: vitest runs components against MSW, and every regression this file has
actually caught -- a missing route, a thumbnail path that 404s, a static asset
served stale -- lived in the space between the app and the real server, where a
mock would have answered happily.

    python frontend/tests/smoke.py [http://127.0.0.1:8000] [label]

Exits non-zero on any console error, any failed request to our own origin, or
any assertion below.
"""
import json
import re
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
LABEL = sys.argv[2] if len(sys.argv) > 2 else "run"

# Where the app is mounted during the overlap. One line to change at cut-over.
APP = f"{BASE}/"

IGNORE = [re.compile(r"favicon")]


def ignored(text: str) -> bool:
    return any(p.search(text) for p in IGNORE)


def aborted(failure: str | None) -> bool:
    """ERR_ABORTED is the browser cancelling a request, not the server failing
    one -- overwhelmingly, an image still loading when the page navigates away.
    A card's creator notes pull a dozen remote images through /proxy/, so
    clicking onward from a detail page reliably produces a handful of these, and
    counting them as failures made a healthy run look broken. Real server
    errors still land: they arrive as responses, not failures (see below)."""
    return bool(failure and "ERR_ABORTED" in failure)


def foreign(url: str) -> bool:
    """True for a request to somewhere other than the server under test. A card
    portrait is served by us; a creator-notes image on a remote CDN is not, and
    whether that CDN answers is not what this gate is about."""
    return not url.startswith(BASE)


#: What the Source pill calls each platform -- mirrors PLATFORM_LABELS in
#: src/lib/card.ts, which is where the UI's own copy lives.
SOURCE_LABELS = {
    "janitor": "JanitorAI",
    "chub": "Chub",
    "datacat": "DataCat",
    "saucepan": "Saucepan",
    "jannyai": "JannyAI",
    "card": "Imported file",
}


def shown_count(page) -> int:
    """The "N of M" count in the toolbar, as a number.

    The first span in the sticky bar, read rather than searched for across the
    whole body: a card name that happens to contain a digit would otherwise
    satisfy a loose regex and make the comparison pass on the wrong text."""
    text = page.inner_text(".sticky span")
    return int(re.match(r"([\d,]+)", text).group(1).replace(",", ""))


def main() -> int:
    errors: list[str] = []
    failed: list[str] = []
    result: dict = {"label": LABEL}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        page.on("console", lambda m: (
            errors.append(f"{m.type}: {m.text}")
            if m.type in ("error", "warning") and not ignored(m.text) else None))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: (
            failed.append(f"[{page.url.replace(BASE, '')}] {r.url} {r.failure}")
            if not ignored(r.url) and not foreign(r.url)
            and not aborted(r.failure) else None))
        page.on("response", lambda r: (
            failed.append(f"HTTP {r.status} {r.url}")
            if r.status >= 400 and not ignored(r.url) and not foreign(r.url) else None))

        try:
            drive(page, result)
        except Exception as exc:  # keep the console log -- it names the cause
            result["ABORTED"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

        browser.close()

    result["console_errors"] = errors
    result["failed_requests"] = sorted(set(failed))
    print(json.dumps(result, indent=2))
    return 1 if errors or failed or result.get("ABORTED") else 0


def tiles(page) -> int:
    return page.eval_on_selector_all("a[href*='/characters/']", "els => els.length")


def api(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=15) as resp:
        return json.load(resp)


def find_card_with_notes_image(checked_limit: int = 400) -> str | None:
    """A real card whose creator notes carry an image -- markdown `![alt](url)`
    (what most sources flatten to, aspect/text/html_md.py) or a literal `<img`
    (Chub's laid-out HTML). Found live rather than hardcoded, so the check
    keeps working as the archive changes. Bounded: this used to be exactly the
    kind of note that silently rendered as text-only (a stray `!` beside a
    mangled link), so it is worth a real assertion, not just an iframe-mounted
    check."""
    offset = 0
    checked = 0
    while checked < checked_limit:
        page = api(f"/api/v1/characters?limit=100&offset={offset}")
        items = page["items"]
        if not items:
            return None
        for item in items:
            if not item.get("has_creator_notes"):
                continue
            checked += 1
            detail = api(f"/api/v1/characters/{urllib.parse.quote(item['id'])}")
            notes = detail.get("card", {}).get("creator_notes") or ""
            if "![" in notes or "<img" in notes:
                return item["id"]
            if checked >= checked_limit:
                return None
        offset += 100
    return None


def drive(page, result: dict) -> None:
    page.goto(APP, wait_until="networkidle", timeout=60_000)
    page.wait_for_selector("a[href*='/characters/']", timeout=60_000)
    page.wait_for_timeout(1200)

    result["shown"] = page.inner_text(".sticky span")
    result["tiles_first_paint"] = tiles(page)

    # Every tile is a link, so a broken portrait is a broken thumbnail route --
    # the one class of failure that is invisible in a screenshot of a dark UI.
    result["thumbs_broken"] = page.evaluate(
        "() => [...document.images].filter(i => i.complete && i.naturalWidth === 0"
        " && i.src && !i.src.startsWith('data:')).length")

    # --- infinite scroll: the second page must arrive on its own ---
    before = tiles(page)
    # The app scrolls its own container, not the document, so the wheel has to
    # land over the grid -- the pointer starts at (0,0), which is the fixed top
    # bar and scrolls nothing.
    page.mouse.move(800, 600)
    page.mouse.wheel(0, 40_000)
    page.wait_for_timeout(2500)
    result["tiles_after_scroll"] = tiles(page)
    result["paged"] = result["tiles_after_scroll"] > before

    # --- a chip filters, and narrows ---
    page.mouse.move(800, 600)
    page.mouse.wheel(0, -60_000)
    page.wait_for_timeout(400)
    page.click("button:text-is('Lorebook')")
    page.wait_for_timeout(1200)
    result["filtered_count"] = page.inner_text(".sticky span")
    result["filtered_url"] = page.url
    page.click("button:text-is('All')")
    page.wait_for_timeout(800)

    # --- sort ---
    page.click("button:has-text('Sort')")
    page.wait_for_timeout(300)
    page.click("button:text-is('Recently added')")
    page.wait_for_timeout(1200)
    result["sorted_url"] = page.url

    # --- the tag catalogue behind the Tags pill ---
    # The whole vocabulary, not a top-N slice: the popover has its own search
    # box, so a capped list makes a rare tag look nonexistent rather than merely
    # unlisted. Compared against /facets so a re-introduced cap fails here.
    page.click("button[aria-label='Filter by tag']")
    page.wait_for_timeout(900)
    result["tag_options"] = page.eval_on_selector_all(
        "[role=dialog] button, [data-radix-popper-content-wrapper] button",
        "els => els.length")
    every_tag = len(api("/api/v1/facets?limit=0")["tags"])
    result["tag_catalogue_complete"] = result["tag_options"] >= every_tag
    if not result["tag_catalogue_complete"]:
        raise AssertionError(
            f"Tags pill lists {result['tag_options']} entries for {every_tag} tags -- truncated")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    # --- Source: one platform, both of its importer kinds ---
    # The pill folds `chub_import` and `chub_core` into a single "Chub" row, so
    # the count it produces must equal the API's for both kinds ORed. A regression
    # to single-kind matching shows up here as a number that is short by the
    # smaller kind, which is easy to miss by eye and impossible to miss here.
    facets = api("/api/v1/facets?limit=0")
    platforms: dict[str, list[str]] = {}
    for source in facets["sources"]:
        platforms.setdefault(re.sub(r"_(core|import)$", "", source["value"]), []).append(
            source["value"])
    # A two-kind platform by preference -- folding those into one row is the
    # whole point of the pill, and a single-kind platform would pass this check
    # even if the fold were broken.
    platform = max(platforms, key=lambda p: len(platforms[p]))
    kinds = platforms[platform]
    page.click("button[aria-label='Filter by source']")
    page.wait_for_timeout(700)
    page.click("[data-radix-popper-content-wrapper] button:has-text('%s')"
               % SOURCE_LABELS.get(platform, platform))
    page.wait_for_timeout(1500)
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    expected = api("/api/v1/characters?limit=0&"
                   + "&".join(f"source={k}" for k in kinds))["total"]
    result["source_kinds_ored"] = kinds
    result["source_pill_count"] = shown_count(page)
    if result["source_pill_count"] != expected:
        raise AssertionError(
            f"Source pill shows {result['source_pill_count']} for {kinds}, API says {expected}")
    page.click("button:text-is('All')")
    page.wait_for_timeout(800)

    # --- Creator ---
    top_creator = max(facets["creators"], key=lambda c: c["count"])
    page.click("button[aria-label='Filter by creator']")
    page.wait_for_timeout(700)
    page.click("[data-radix-popper-content-wrapper] button:has-text('%s')" % top_creator["value"])
    page.wait_for_timeout(1500)
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    result["creator_filtered"] = top_creator["value"]
    result["creator_pill_count"] = shown_count(page)
    if result["creator_pill_count"] != top_creator["count"]:
        raise AssertionError(
            f"Creator pill shows {result['creator_pill_count']} for {top_creator['value']}, "
            f"facets say {top_creator['count']}")
    page.click("button:text-is('All')")
    page.wait_for_timeout(800)

    # The "Needs media" filter (§3.3): a real manifest-backed count, matched
    # against the same question asked of the API directly.
    page.click("button:text-is('＋ Filter')")
    page.wait_for_timeout(700)
    page.click("text=Needs media")
    page.wait_for_timeout(1500)
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    needs_media = api("/api/v1/characters?needs_media=true&limit=0")["total"]
    result["needs_media_cards"] = needs_media
    result["needs_media_chip_agrees"] = shown_count(page) == needs_media
    if not result["needs_media_chip_agrees"]:
        raise AssertionError(
            f"Needs media chip shows {shown_count(page)}, API says {needs_media}")
    page.click("button:text-is('All')")
    page.wait_for_timeout(800)

    # --- search overlay ---
    page.keyboard.press("Meta+k")
    page.wait_for_timeout(700)
    page.fill("input[aria-label='Search query']", "abbie")
    page.wait_for_timeout(1500)
    result["search_matches"] = page.inner_text("[role=dialog] >> text=/matches/")
    result["search_results"] = page.eval_on_selector_all(
        "[role=dialog] a[href*='/characters/']", "els => els.length")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    # --- favourites tab ---
    page.click("a:text-is('Favorites')")
    page.wait_for_timeout(1500)
    result["favorites_url"] = page.url
    result["favorites_count"] = page.inner_text(".sticky span")

    # --- deep link: the SPA fallback the old UI never had ---
    page.goto(f"{APP}favorites?sort=-added", wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(1200)
    result["deep_link_ok"] = "Favorites" in page.inner_text("h1")

    # --- card detail: open the first tile, walk the tabs, page next ---
    page.goto(APP, wait_until="networkidle", timeout=60_000)
    page.wait_for_selector("a[href*='/characters/']", timeout=60_000)
    page.wait_for_timeout(800)
    first = page.get_attribute("main a[href*='/characters/'], a[href*='/characters/']", "href")
    page.click("a[href*='/characters/']")
    page.wait_for_selector("h1", timeout=60_000)
    page.wait_for_timeout(1000)
    result["detail_url"] = page.url
    result["detail_name"] = page.inner_text("h1")
    result["detail_tabs"] = page.eval_on_selector_all(
        "nav button", "els => els.map(e => e.textContent.replace(/\\d+$/,'').trim())")

    # Each content tab must render without a console error (caught globally).
    for tab in ("Greetings", "Dialogue", "Lorebook", "Gallery", "Expressions",
                "Related", "Notes", "Info"):
        btn = page.query_selector(f"nav button:has-text('{tab}')")
        if btn:
            btn.click()
            page.wait_for_timeout(500)
    result["info_has_json"] = "chara_card" in page.inner_text("body")

    # The Info tab's card.json and the creator-notes iframe are the two panes a
    # screenshot cannot vouch for; assert the iframe mounted.
    page.query_selector("nav button:has-text('Notes')").click()
    page.wait_for_timeout(800)
    result["notes_iframe"] = page.eval_on_selector_all(
        "iframe[title='Creator notes']", "els => els.length")

    # A real image actually decodes inside the notes iframe -- not just that
    # the frame mounted. Markdown-flattened notes (`![alt](url)`, the common
    # case) once rendered as a stray "!" beside a mangled link instead of a
    # picture; this is the regression check for that.
    notes_card = find_card_with_notes_image()
    if notes_card:
        page.goto(f"{BASE}/characters/{notes_card}?tab=notes",
                   wait_until="networkidle", timeout=30_000)
        page.wait_for_selector("iframe[title='Creator notes']", timeout=15_000)
        page.wait_for_timeout(1200)
        frame = page.query_selector("iframe[title='Creator notes']").content_frame()
        loaded = frame.eval_on_selector_all(
            "img", "els => els.filter(e => e.complete && e.naturalWidth > 0).length")
        total = frame.eval_on_selector_all("img", "els => els.length")
        result["notes_image_card"] = notes_card
        result["notes_images_found"] = total
        result["notes_images_loaded"] = loaded

    # prev/next: J steps to the next card in the browse set.
    page.query_selector("nav button:has-text('Overview')").click()
    page.wait_for_timeout(300)
    before_url = page.url
    page.keyboard.press("j")
    page.wait_for_timeout(1200)
    result["pager_moved"] = page.url != before_url

    # --- writes, all reversible so the real archive is left as it was ---
    # Favourite is the one end-to-end write cheap enough to prove live: star the
    # card, confirm the button flips, then unstar it back. A round trip through
    # POST /favorite that rewrites the PNG and leaves it exactly as found.
    starred = page.query_selector("button:has-text('Favourite')")
    if starred:
        starred.click()
        page.wait_for_selector("button:has-text('Favourited')", timeout=15_000)
        result["favourite_toggled"] = True
        page.click("button:text-is('Favourited')")
        page.wait_for_selector("button:text-is('Favourite')", timeout=15_000)

    # The inline editor opens and cancels without writing: click Edit on a
    # section, confirm a textarea appears, Cancel, confirm it is gone.
    edit = page.query_selector("section:has-text('Description') button:has-text('Edit')")
    if edit:
        edit.click()
        page.wait_for_selector("textarea", timeout=10_000)
        result["editor_opens"] = True

        # J/K page between cards, and they are bare letters, so they collide
        # with typing. Typing a word carrying one into an open editor used to
        # navigate away and take the unsaved draft with it -- still no write
        # here, the draft is abandoned by Cancel below.
        editing_url = page.url
        page.focus("textarea")
        grew_from = len(page.input_value("textarea"))
        page.keyboard.type("jack and kate")
        page.wait_for_timeout(800)
        result["editor_survives_jk"] = (
            page.url == editing_url
            and page.query_selector("textarea") is not None
            and len(page.input_value("textarea")) == grew_from + 13)
        if not result["editor_survives_jk"]:
            raise AssertionError(
                f"typing in the editor navigated: {editing_url} -> {page.url}")

        page.click("button:has-text('Cancel')")
        page.wait_for_timeout(400)
        result["editor_closes"] = page.query_selector("textarea") is None

    # The Import menu opens and offers its live doors (no write).
    page.click("button[title='Import']")
    page.wait_for_timeout(500)
    result["import_menu"] = page.eval_on_selector_all(
        "[data-radix-popper-content-wrapper] button", "els => els.length")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # deep link straight to a detail route + tab. `first` may already carry the
    # shelf's ?sort=, so drop its query before adding our own -- appending a
    # second ? would fuse them into one malformed sort value.
    if first:
        path = f"{BASE}{first}".split("?", 1)[0]
        page.goto(f"{path}?tab=info", wait_until="networkidle", timeout=60_000)
        page.wait_for_selector("h1", timeout=60_000)
        page.wait_for_timeout(800)
        result["detail_deep_link_ok"] = "chara_card" in page.inner_text("body")

    # --- Tags: the consolidation editor (Stage 4) ---
    # Non-mutating: it surveys the whole archive and reads the stored dictionary
    # delta, but every interaction here (menus, dialogs) is cancelled, so the
    # archive and the settings blob are left exactly as found. The one write path
    # the page owns -- the §3.7 read-modify-write of settings -- is proven
    # deterministically in vitest (use-settings.test.ts) rather than against the
    # live token store.
    page.goto(APP, wait_until="networkidle", timeout=60_000)
    page.wait_for_selector("a[href*='/characters/']", timeout=60_000)
    page.click("a:text-is('Tags')")
    # The survey is the whole archive in one request; give it room.
    page.wait_for_selector("text=renames staged", timeout=60_000)
    page.wait_for_timeout(800)
    result["tags_categories"] = page.eval_on_selector_all("section h3", "els => els.length")
    result["tags_has_stats"] = "cards affected" in page.inner_text("body")
    result["tags_has_buckets"] = (
        "Unassigned" in page.inner_text("body") and "Removed" in page.inner_text("body"))

    # Find narrows the categories shown.
    page.fill("input[placeholder=\"Find a tag or variant…\"]", "romance")
    page.wait_for_timeout(700)
    result["tags_find_categories"] = page.eval_on_selector_all("section h3", "els => els.length")
    page.fill("input[placeholder=\"Find a tag or variant…\"]", "")
    page.wait_for_timeout(500)

    # A variant chip opens its move menu (no move made).
    chip = page.query_selector("section .flex-wrap button:has(span.font-mono)")
    if chip:
        chip.click()
        page.wait_for_timeout(400)
        result["tags_chip_menu"] = page.query_selector(
            "[data-radix-popper-content-wrapper]") is not None
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    # The Apply dialog opens and cancels without touching a card.
    apply_btn = page.query_selector("button:has-text('Apply tags')")
    if apply_btn and not apply_btn.is_disabled():
        apply_btn.click()
        page.wait_for_selector("text=Apply tag consolidation?", timeout=10_000)
        result["tags_apply_dialog"] = True
        page.click("button:text-is('Cancel')")
        page.wait_for_timeout(400)

    # --- Discover: provider browse + card preview ---
    # Non-mutating on purpose -- "Get" is a real write, and /build-chub and
    # /build-datacat already have pytest coverage (tests/api/test_acquisition.py,
    # tests/api/test_discover_preview.py). This gate proves the page's own
    # mechanics: it reaches a real provider, the have-guard round-trips through
    # our own /api/v1/characters/have, the tile grid renders, and a provider
    # card can actually be *read* -- the last of which is the thing that was
    # missing entirely when Discover was first built.
    page.goto(APP, wait_until="networkidle", timeout=60_000)
    page.wait_for_selector("a[href*='/characters/']", timeout=60_000)
    page.click("a:text-is('Discover')")
    page.wait_for_selector("text=add straight to the archive", timeout=15_000)
    page.wait_for_selector("a[href*='/discover/chub/']", timeout=60_000)
    page.wait_for_timeout(1500)
    result["discover_chub_results"] = len(
        page.query_selector_all("a[href*='/discover/chub/']"))

    # The sort control is present for every provider and every feed (the mock's
    # #discSort); it used to be a Chub-browse-only select.
    result["discover_sort_label"] = page.inner_text(
        "button:has-text('Sort')").replace("\n", " ")

    page.fill("input[placeholder='Search Chub…']", "elf")
    page.wait_for_timeout(2500)
    result["discover_chub_search_count"] = page.inner_text("text=/results/")

    page.click("button:text-is('DataCat')")
    page.wait_for_selector("a[href*='/discover/datacat/']", timeout=60_000)
    result["discover_datacat_results"] = len(
        page.query_selector_all("a[href*='/discover/datacat/']"))
    # DataCat had no sort control at all until the Discover rebuild.
    result["discover_datacat_sort_label"] = page.inner_text(
        "button:has-text('Sort')").replace("\n", " ")

    page.click("button:text-is('Hide cards I have')")
    page.wait_for_timeout(1000)
    result["discover_hide_have_toggled"] = "border-sage-line" in (
        page.get_attribute("button:text-is('Hide cards I have')", "class") or "")

    # Open a provider card. This exercises the whole read path in one click:
    # the browser's capture (full node / detail + lorebook), the server's
    # POST /api/v1/discover/preview, and the archive's own detail panes
    # rendering a card that is not in the archive.
    page.click("button:text-is('Chub')")
    page.wait_for_selector("a[href*='/discover/chub/']", timeout=60_000)
    page.wait_for_timeout(1500)
    page.query_selector_all("a[href*='/discover/chub/']")[0].click()
    page.wait_for_selector("h1", timeout=60_000)
    page.wait_for_timeout(1000)
    result["preview_url"] = page.url.replace(BASE, "")
    result["preview_title"] = page.inner_text("h1")
    result["preview_tabs"] = page.eval_on_selector_all(
        "nav button", "els => els.map(e => e.textContent.trim())")
    # Read-only: the archive's Edit affordances must not appear on a card that
    # has nothing to write to yet.
    result["preview_edit_buttons"] = len(
        page.query_selector_all("button:has-text('Edit')"))
    result["preview_can_add"] = page.query_selector(
        "button:has-text('Add to archive'), button:has-text('Add again')") is not None
    if result["preview_edit_buttons"]:
        errors.append("preview offered Edit on a card not in the archive")
    if len(result["preview_tabs"]) != 4:
        errors.append(f"preview tabs: {result['preview_tabs']}")
    page.go_back()
    page.wait_for_timeout(1500)

    # --- Settings (Stage 6) ---
    # Deliberately careful about what actually gets clicked: Library's Default
    # sort and Providers' proxy Save both write to the one settings blob that
    # also holds the old UI's Chub/DataCat tokens (§3.7), and this gate may run
    # against a real archive -- so this only ever reads them, plus the two
    # writes that are genuinely safe to fire for real: a userscript Generate
    # (writes nothing, just compiles text) and a Maintenance Rescan (re-reads
    # the archive off disk, changes no card).
    page.goto(f"{APP}settings/library", wait_until="networkidle", timeout=30_000)
    page.wait_for_selector("h2", timeout=15_000)
    page.wait_for_timeout(500)
    result["settings_sections"] = page.eval_on_selector_all(
        "nav a", "els => els.map(e => e.textContent.trim())")[-5:]

    page.click("nav a:has-text('Providers')")
    page.wait_for_timeout(500)
    # Scoped to the proxy row: DataCat's session row also has a "Test" button,
    # and a bare has-text('Test') was picking that one -- so this step had been
    # exercising the DataCat token check and then timing out waiting for a proxy
    # verdict that was never requested.
    proxy_row = page.locator(
        "div:has(> input[placeholder^='http://host:port'])").first
    proxy_row.locator("button:has-text('Test')").click()
    page.wait_for_selector("text=/not configured|active|failed|IP did not change/",
                            timeout=15_000)
    result["settings_proxy_test_ran"] = True

    page.click("nav a:has-text('Userscripts')")
    page.wait_for_timeout(800)
    gen = page.query_selector("button:has-text('Generate')")
    if gen:
        gen.click()
        page.wait_for_selector("pre", timeout=15_000)
        result["settings_userscript_generated"] = len(page.inner_text("pre")) > 0

    page.click("nav a:has-text('Maintenance')")
    page.wait_for_timeout(500)
    page.click("button:has-text('Rescan')")
    page.wait_for_selector("text=/Rescanned:/", timeout=10_000)
    result["settings_rescan_ran"] = True


if __name__ == "__main__":
    sys.exit(main())
