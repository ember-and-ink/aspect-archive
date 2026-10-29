  // ---------------------------------------------------------------------------
  // Character state — resolved from JanitorAI's JSON, not the DOM.
  //
  // A character page's URL carries the character UUID; the chat page's does
  // not. So on a character page we fetch the JSON once (cached per id) and
  // persist id + hidden (GM_setValue); the chat page — where the hidden capture
  // actually happens — reads those back. `showdefinition:false` is the
  // open/hidden flag. Reset by the pill's CLEAR affordance.
  //
  // The character NAME is deliberately NOT tracked or shown: the pill only needs
  // open-vs-hidden + capture state, and the server derives the real name from
  // the API JSON at build time. The one place a name is still required — the
  // per-card capture-status query for a hidden card — resolves it transiently
  // from the cached JSON (characterName), never persisting or displaying it.
  // ---------------------------------------------------------------------------
  const KEY_ID = "jai_last_char_id";
  const KEY_HIDDEN = "jai_last_card_hidden";

  const AVATAR_BASE = "https://ella.janitorai.com/bot-avatars/";
  const CHAR_BASE = "https://janitorai.com/characters/";

  function resetPluginState() {
    GM_setValue(KEY_ID, "");
    GM_setValue(KEY_HIDDEN, false);
  }

  // The real character name from a card JSON (chat_name; falls back to the
  // title blurb). Used only to key the hidden-card capture-status lookup.
  function characterName(json) {
    return ((json && (json.chat_name || json.name)) || "").trim();
  }

  // The character UUID from a /characters/<uuid>_<slug> path. Scoped to
  // /characters/ so a chat page's own id (a different UUID) can't be mistaken
  // for a character id.
  function currentCharacterId() {
    const m = location.pathname.match(/\/characters?\/([0-9a-f-]{36})/i);
    return m ? m[1].toLowerCase() : null;
  }

  function isChatView() {
    return !!document.querySelector("[class*='_messageBody_']");
  }

  // What kind of page this is, for which overlay sections apply:
  //   character — /characters/<uuid>_…        (Export)
  //   chat      — /chats/…, or a chat in view  (Export: where hidden captures land)
  //   profile   — /profiles/<uuid>_…          (Download all + the switches)
  //   listing   — anything else: home, search, tags (the switches)
  // Pure over its inputs so it is testable; currentPageKind() feeds it the live
  // URL and DOM.
  function pageKindFor(pathname, chatInView) {
    if (/\/characters?\/[0-9a-f-]{36}/i.test(pathname)) return "character";
    if (/^\/chats?\//i.test(pathname) || chatInView) return "chat";
    if (/\/profiles\/[0-9a-f-]{36}/i.test(pathname)) return "profile";
    return "listing";
  }

  function currentPageKind() {
    return pageKindFor(location.pathname, isChatView());
  }

  const CharacterState = {
    _id: null,
    _json: null,
    _fetching: false,

    // Ensure the JSON for `id` is cached, fetching it once if needed, and
    // persist the derived id + hidden flag so the chat page (no id in URL) can
    // read them back. Returns the JSON, or null while a fetch is in flight / on
    // error.
    async ensureJson(id) {
      if (!id) return null;
      if (this._id === id && this._json) return this._json;
      if (this._fetching) return null;
      this._fetching = true;
      try {
        const json = await JanitorClient.fetchCharacter(id);
        this._id = id;
        this._json = json;
        GM_setValue(KEY_ID, id);
        GM_setValue(KEY_HIDDEN, json.showdefinition === false);
        return json;
      } catch (err) {
        warn("character fetch failed", err);
        return null;
      } finally {
        this._fetching = false;
      }
    },

    // On a character page, prime the cache for the id in the URL. No-op on
    // chat / browse pages (no id in URL).
    async refresh() {
      const id = currentCharacterId();
      if (id) await this.ensureJson(id);
    },

    cachedJson(id) {
      return this._id === id ? this._json : null;
    },
  };

  // ---------------------------------------------------------------------------
  // Scheduler — one poll drives the overlay. Paused while the tab is hidden;
  // backs off to 15s while the server is unreachable. A single self-scheduling
  // timer runs at a time; returning to the tab, or an SPA navigation, wakes it.
  //
  // Connection vocabulary (the words match the saucepan bridge's pill):
  //   Aspect                — connected, no card in view
  //   Aspect · ready ✓      — open card, exportable right now
  //   Aspect · hidden ✓     — hidden card, definition + greeting captured
  //   Aspect · hidden ✗     — hidden card, nothing captured yet (amber dot)
  //   Aspect · server down  — server unreachable (red dot)
  // ---------------------------------------------------------------------------
  let serverDown = false;
  let pollTimer = null;

  async function tick() {
    await CharacterState.refresh();

    // Stamp and filter whatever tiles are rendered, folding in cards that
    // infinite-scroll has appended since the last tick.
    await TileFilters.refresh();

    const onChar = !!currentCharacterId();
    const onChat = isChatView();
    const id = currentCharacterId() || GM_getValue(KEY_ID, "");
    const hidden = GM_getValue(KEY_HIDDEN, false);

    try {
      let ready = false;
      let detail;
      if ((!onChar && !onChat) || !id) {
        await ServerClient.health();
        detail = "";
      } else if (!hidden) {
        // Open card: the JSON carries everything; nothing to capture.
        await ServerClient.health();
        detail = "ready ✓";
        ready = true;
      } else {
        // Hidden card: the definition + primary greeting come from the chat
        // relay capture. Resolve THIS card's name (transient, from the JSON by
        // id) purely to ask the server whether its captures are present.
        const json = await CharacterState.ensureJson(id);
        const status = await ServerClient.captureStatus(characterName(json));
        const has = !!(status.system && status.greetings);
        detail = `hidden ${has ? "✓" : "✗"}`;
        ready = has;
      }
      serverDown = false;
      Overlay.setConnection("ok", detail);
      ExportButton.setReady(ready);
    } catch {
      serverDown = true;
      Overlay.setConnection("down");
      ExportButton.setReady(false);
    }
  }

  // The SPA changed the URL (bootstrap's page hook). Sections follow the URL
  // right away, the tile filters bind to the next page's grid the moment it
  // renders, and the tick runs now instead of up to 5s from now.
  function onNavigate() {
    Overlay.render();
    TileFilters.onNavigate();
    scheduleTick(0);
  }

  function scheduleTick(delay) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(runLoop, delay);
  }

  async function runLoop() {
    if (!document.hidden) {
      try {
        await tick();
      } catch (err) {
        warn("tick failed", err);
      }
    }
    scheduleTick(serverDown ? 15000 : 5000);
  }
