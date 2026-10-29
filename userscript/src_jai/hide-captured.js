  // ---------------------------------------------------------------------------
  // TileFilters — the two listing-page switches, "Hide cards I have" and
  // "Recent only". Both work on EVERY surface that renders the shared card tile
  // — the logged-in homepage (/), search, tag browse, creator profiles —
  // because everything they judge is read off the rendered tile itself rather
  // than from any page-specific list endpoint:
  //   • the character UUID comes from the tile's link href
  //     (/characters/<uuid>_<slug>), and
  //   • the tag set comes from the tile's own tag chips, which carry every
  //     official tag AND custom tag verbatim — verified chip-for-chip against
  //     the homepage's own /hampter/characters JSON (34/34 cards, nothing
  //     truncated, `#custom` and "👩‍🦰 Female" both normalizing cleanly).
  // So we can only judge cards that are actually rendered — but the rendered
  // DOM already carries everything needed to judge them, with no extra request
  // and no assumption about which query produced the listing. The "Limitless"
  // quality badge shares the chip container but carries neither the -regular nor
  // the -custom class, so it is deliberately not read as a tag.
  //
  // HIDE CARDS I HAVE folds away a card when EITHER:
  //   • it's already exported to disk, OR
  //   • the include/exclude tag filter (BULK_TAG_FILTER) would skip it — the
  //     same filter the bulk sweep uses to pick cards.
  // Disk membership goes through the SAME `POST /existing` id-fragment check the
  // bulk sweep uses (it matches the `_<id8>` filename fragment). A tag-filtered
  // card is hidden up front and never touches it. Only the /existing verdicts
  // are memoized (they cost a round trip); the tag verdict is re-derived every
  // scan, so a wrapper React recycles for a different card can never keep a
  // stale marker.
  //
  // RECENT ONLY keeps the cards first seen inside RECENT_WINDOW_MS. Every tile
  // id is stamped through `/discover/seen` (the `janitor` bucket) the first time
  // it renders, WHETHER OR NOT the switch is on — stamping only while it was on
  // would make everything "new" the first time you turned it on. The stamp is
  // when the card first reached your screen, not anything JanitorAI reports:
  // its feed sorts cards that were drafted days before they were published in
  // under ones published since, and a sighting stamp is immune to that. A tile
  // with no stamp yet (or none to be had, server down) stays visible: this
  // fails open, like Discover.
  //
  // Hiding is class-only and fully reversible: a folded card's wrapper gets
  // `jai-captured` (on disk), `jai-filtered` (tag-filtered) or `jai-stale` (not
  // recent), and an <html> flag per switch drives the real `display:none` via
  // CSS — JAI's own inline styles are never touched. The two switches combine
  // as AND. Re-scanning on the scheduler tick folds in cards infinite-scroll
  // adds; a grid observer and a navigation watcher make page swaps immediate.
  //
  // Switch state lives in memory: it survives the SPA's own navigation and
  // resets on a real page load.
  // ---------------------------------------------------------------------------
  const CARD_LINK_SEL = "a.profile-character-card-stack-link-component";
  const CARD_WRAP_SEL = ".profile-character-card-wrapper";
  const CARD_LIST_SEL = ".pp-cc-list-container"; // direct parent of the wrappers
  const CARD_TAGS_SEL = ".profile-character-card-tags"; // chip container on a tile
  const CARD_TAG_SEL =
    ".profile-character-card-tags-wrap-regular, .profile-character-card-tags-wrap-custom";
  const HIDE_ACTIVE_CLASS = "jai-hide-captured"; // on <html> while Hide is on
  const RECENT_ACTIVE_CLASS = "jai-recent-only"; // on <html> while Recent only is on
  const CARD_SAVED_CLASS = "jai-captured";       // on a wrapper whose card is on disk
  const CARD_FILTERED_CLASS = "jai-filtered";    // on a wrapper the tag filter skips
  const CARD_STALE_CLASS = "jai-stale";          // on a wrapper first seen outside the window
  const CARD_ID_ATTR = "data-jai-id";            // resolved id, stamped for debugging

  // How long after a URL change to keep watching for the new page's grid. A
  // page with no grid (a character page) just times out.
  const NAV_SETTLE_MS = 10000;

  // The character UUID from a card link's href
  // (https://janitorai.com/characters/<uuid>_<slug>). Mirrors the scheduler's
  // currentCharacterId(), but reads an arbitrary href instead of the URL bar.
  function cardIdFromHref(href) {
    const m = String(href || "").match(/\/characters?\/([0-9a-f-]{36})/i);
    return m ? m[1].toLowerCase() : null;
  }

  // A rendered tile's tags, shaped as a list row so passesTagFilter() (from
  // bulk.js) can judge it unchanged — one filter implementation for both the
  // bulk sweep and this toggle. rowTagSet() normalizes `tags[].name` and
  // `custom_tags` identically (normTag drops the leading emoji / "#"), so every
  // chip can go in one bucket regardless of which kind it is.
  //
  // Returns null when the tile has no chip container at all, meaning its tags
  // aren't rendered yet — the caller then leaves the card visible rather than
  // guessing. An EMPTY container is a real answer (a card genuinely without
  // tags) and yields an empty row, which an `include` filter correctly rejects.
  function tagRowFromWrapper(wrap) {
    const box = wrap.querySelector(CARD_TAGS_SEL);
    if (!box) return null;
    return {
      custom_tags: [...box.querySelectorAll(CARD_TAG_SEL)].map((el) => el.textContent),
    };
  }

  // Outside the window, by a stamp the server actually answered. No stamp is
  // never stale: an unstamped tile stays visible (fails open).
  function isStale(stamp, now, windowMs) {
    return typeof stamp === "number" && now - stamp >= windowMs;
  }

  const TileFilters = {
    hide: false,
    recent: false,
    _checked: new Map(),   // id -> bool (true = already on disk); memoized /existing
    _stamps: new Map(),    // id -> first-seen epoch ms, as the server answered
    _busy: false,          // an /existing call is in flight
    _stamping: false,      // a /discover/seen call is in flight
    _stampFailed: false,   // the last stamp call failed; Recent only is failing open
    _counts: { tiles: 0, hidden: 0, recent: 0 },
    _observer: null,       // watches the card grid for page changes
    _observed: null,       // the list container currently observed
    _debounce: null,       // coalesces a burst of grid mutations into one _sync
    _navObserver: null,    // waits for the next page's grid after a URL change
    _navTimer: null,

    // Map every rendered card to its wrapper, id and tag verdict. The id is
    // stamped on the wrapper purely so a card's identity is visible in devtools;
    // both it and the verdict are re-derived here every scan so nothing can go
    // stale. Returns [{ id, wrap, filtered }].
    _scan() {
      const out = [];
      for (const link of document.querySelectorAll(CARD_LINK_SEL)) {
        const wrap = link.closest(CARD_WRAP_SEL);
        if (!wrap) continue;
        const id = cardIdFromHref(link.getAttribute("href"));
        if (!id) continue;
        wrap.setAttribute(CARD_ID_ATTR, id);
        const row = tagRowFromWrapper(wrap);
        out.push({ id, wrap, filtered: row ? !passesTagFilter(row) : false });
      }
      return out;
    },

    // Record every tile not stamped yet in this tab. Ids left unstamped (a
    // call in flight, or a failure) are simply retried on the next scan.
    async _stamp(cards) {
      const unknown = [...new Set(cards.map((c) => c.id).filter((id) => !this._stamps.has(id)))];
      if (!unknown.length || this._stamping) return;
      this._stamping = true;
      try {
        const firstSeen = await ServerClient.seen(unknown);
        for (const [id, stamp] of Object.entries(firstSeen)) this._stamps.set(id, stamp);
        this._stampFailed = false;
      } catch (err) {
        warn("tile-filters: seen stamp failed", err);
        this._stampFailed = true;
      } finally {
        this._stamping = false;
      }
    },

    // Disk-check only cards the tag filter KEEPS; a filtered-out card is
    // hidden by its chips alone and never costs a round trip.
    async _classifySaved(cards) {
      const unknown = [
        ...new Set(
          cards.filter((c) => !c.filtered && !this._checked.has(c.id)).map((c) => c.id)
        ),
      ];
      if (!unknown.length || this._busy) return;
      this._busy = true;
      try {
        const saved = new Set(await ServerClient.existing(unknown));
        for (const id of unknown) this._checked.set(id, saved.has(id));
      } catch (err) {
        warn("tile-filters: existing check failed", err); // retried on the next scan
      } finally {
        this._busy = false;
      }
    },

    // Stamp, classify (only while Hide is on — nothing else needs it), then
    // mark every rendered wrapper. Safe to call every tick; the in-flight
    // guards keep overlapping ticks from double-firing.
    async _sync() {
      const cards = this._scan();
      await Promise.all([this._stamp(cards), this.hide ? this._classifySaved(cards) : null]);

      const now = Date.now();
      let hidden = 0;
      let recent = 0;
      for (const { id, wrap, filtered } of cards) {
        const saved = !filtered && this._checked.get(id) === true;
        const stamp = this._stamps.get(id);
        const stale = isStale(stamp, now, RECENT_WINDOW_MS);
        wrap.classList.toggle(CARD_FILTERED_CLASS, filtered);
        wrap.classList.toggle(CARD_SAVED_CLASS, saved);
        wrap.classList.toggle(CARD_STALE_CLASS, stale);
        if (filtered || saved) hidden += 1;
        if (stamp !== undefined && !stale) recent += 1;
      }
      this._counts = { tiles: cards.length, hidden, recent };
      Overlay.render();
    },

    // Watch the card grid so a pagination click (prev/next or a page button)
    // reapplies the filters immediately instead of waiting for the next 5s
    // tick. The wrappers are direct children of CARD_LIST_SEL, so childList
    // there fires exactly when JAI swaps in a new page's cards — after React has
    // rendered them, sidestepping any fetch-vs-render race. Bound whenever a
    // grid exists, switches on or off, because stamping never stops. A short
    // debounce coalesces the remove-old/add-new burst into a single re-sync.
    _watch() {
      const container = document.querySelector(CARD_LIST_SEL);
      if (this._observed === container) return; // no change (covers both null)
      if (this._observer) {
        this._observer.disconnect();
        this._observer = null;
      }
      this._observed = container;
      if (!container) return;
      this._observer = new MutationObserver(() => {
        clearTimeout(this._debounce);
        this._debounce = setTimeout(() => this._sync(), 150);
      });
      this._observer.observe(container, { childList: true });
    },

    _stopNavWatch() {
      if (this._navObserver) this._navObserver.disconnect();
      this._navObserver = null;
      clearTimeout(this._navTimer);
    },

    // The URL just changed (bootstrap's page hook saw pushState/popstate), but
    // React hasn't rendered the next page yet, and the grid it will render is a
    // NEW container the old observer isn't on. Watch the document for that
    // grid, bind to it and sync the moment it lands — this is what used to
    // wait for the next tick, up to 5s with the old cards showing. Bounded by
    // NAV_SETTLE_MS, so the subtree observer never outlives a navigation (and
    // never sits on a chat page's streaming tokens).
    onNavigate() {
      this._stopNavWatch();
      const body = document.body;
      if (!body) return;
      this._navObserver = new MutationObserver(() => {
        const list = document.querySelector(CARD_LIST_SEL);
        if (!list || list === this._observed) return;
        this._stopNavWatch();
        this._watch();
        this._sync();
      });
      this._navObserver.observe(body, { childList: true, subtree: true });
      this._navTimer = setTimeout(() => this._stopNavWatch(), NAV_SETTLE_MS);
    },

    // Scheduler hook: rebind the grid watcher as the SPA swaps grids in/out, and
    // fold in (and stamp) cards that infinite-scroll has appended since last tick.
    async refresh() {
      this._watch();
      if (document.querySelector(CARD_LINK_SEL)) {
        await this._sync();
      } else if (this._counts.tiles) {
        this._counts = { tiles: 0, hidden: 0, recent: 0 };
        Overlay.render();
      }
    },

    async toggle(which) {
      if (which === "recent") {
        this.recent = !this.recent;
        document.documentElement.classList.toggle(RECENT_ACTIVE_CLASS, this.recent);
      } else {
        this.hide = !this.hide;
        document.documentElement.classList.toggle(HIDE_ACTIVE_CLASS, this.hide);
      }
      Overlay.render();
      await this._sync();
    },

    // What the overlay shows: each switch's one-line subtext, and the short
    // parts the collapsed pill appends ("12 hidden", "recent").
    describe() {
      const { tiles, hidden, recent } = this._counts;
      const hours = Math.round(RECENT_WINDOW_MS / 3600000);
      let hideSub;
      let recentSub;
      if (!tiles) {
        hideSub = recentSub = "No cards on this page";
      } else {
        hideSub = this.hide ? `${hidden} hidden on this page` : "Saved cards, plus tag-filter skips";
        if (this._stampFailed) recentSub = "Couldn't check recent cards";
        else if (this.recent) recentSub = `${recent} of ${tiles} first seen in ${hours}h`;
        else recentSub = `First seen in the last ${hours}h`;
      }
      const summary = [];
      if (this.hide) summary.push(`${hidden} hidden`);
      if (this.recent) summary.push(this._stampFailed ? "recent ⚠" : "recent");
      return { hideSub, recentSub, summary };
    },
  };
