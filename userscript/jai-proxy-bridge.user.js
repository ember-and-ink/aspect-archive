// ==UserScript==
// @name         jai-proxy bridge
// @namespace    https://github.com/mjnitz02/jai-proxy
// @version      0.10.1
// @description  Thin bridge: relays JanitorAI chat completions through a local Aspect Archive server (which answers them locally), shows a connection pill, and exports a character as a V3 card PNG via JanitorAI's clean JSON API (no DOM scraping). Card assembly lives server-side.
// @match        https://janitorai.com/*
// @match        https://www.janitorai.com/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      127.0.0.1
// @connect      localhost
// @connect      *
// @connect      janitorai.com
// @connect      ella.janitorai.com
// ==/UserScript==
//
// `@connect *` is there because the server no longer has to be on this machine
// (see docs/DEPLOY.md): its URL is read from Tampermonkey storage at runtime,
// so the host it points at cannot be declared here at compile time, and without
// a matching @connect Tampermonkey blocks the request outright. The loopback
// entries above stay for the default case.
//
// SOURCE LAYOUT — this file is COMPILED. Do not edit jai-proxy-bridge.user.js by
// hand; edit userscript/src_jai/*.js and run `make compile` (see
// scripts/compile_userscript_jai.py). The modules are concatenated, in order,
// inside a single IIFE beneath this banner.

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Where the Aspect Archive server is.
  //
  // Persisted in Tampermonkey storage rather than compiled in, because the
  // server may now be a container on another machine (docs/DEPLOY.md) and the
  // address of that machine is not knowable here. To repoint the bridge, run
  // this once in the console on janitorai.com — no recompile, no reinstall:
  //
  //   GM_setValue("serverUrl", "http://192.168.1.50:8000")
  //
  // and reload. Clearing it falls back to the local default below.
  //
  // Plain http to a LAN address works from HTTPS JanitorAI: every call goes out
  // through GM_xmlhttpRequest (see client-server.js), which runs in the
  // extension context and is exempt from the page's mixed-content and CSP
  // rules. Nothing here needs TLS.
  //
  // ⚠ Whatever you set here must ALSO be the base URL you type into
  // JanitorAI's custom-provider settings: looksLikeModelsProbe (bootstrap.js)
  // recognises the /models probe with `url.startsWith(SERVER)`, so a mismatch
  // silently stops that probe being intercepted.
  const DEFAULT_SERVER = "http://127.0.0.1:8000";
  // Trailing slashes stripped: every call is SERVER + "/some/path".
  const SERVER = String(GM_getValue("serverUrl", "") || DEFAULT_SERVER).replace(/\/+$/, "");

  // If the server's login gate is on (Settings → Security), every request has
  // to carry its API token. Nothing to set by hand: generate a token under
  // Settings → Security, then generate this script under Settings → Userscripts
  // and the token is filled in below. Regenerating the token means reinstalling
  // the script.
  //
  // Empty, no credentials are sent, which is right for a server with the gate
  // off (the default). With the gate on and this empty or stale, the pill stays
  // green — /health is open — and every other call fails with HTTP 401.
  const DEFAULT_TOKEN = "";
  const SERVER_AUTH = DEFAULT_TOKEN ? "Bearer " + DEFAULT_TOKEN : "";

  // ---------------------------------------------------------------------------
  // User config — hand-edited, not persisted. Controls which cards the BULK
  // "download all open cards" run (creator /profiles/ page) exports. The single
  // ⬇ Export button is NEVER filtered; this only narrows a bulk sweep.
  //
  // Matching is case-insensitive and ignores a tag's leading emoji / "#" prefix,
  // so "female" matches JanitorAI's "👩‍🦰 Female". Both a card's official tags
  // and its free-form custom_tags are checked. Tag names are matched WHOLE (after
  // the emoji prefix): "futa" does NOT match "futanari" — list every variant you
  // want to catch.
  //
  // A card is exported when it has at least one `include` tag (or `include` is
  // empty = no include filter) AND none of the `exclude` tags. The filter runs on
  // the cheap list rows, so excluded cards are skipped before any per-card fetch.
  //
  //   include: []                      → download every open card
  //   include: ["female"]              → only cards tagged Female
  //   exclude: ["futa", "futanari"]    → drop either tag
  // ---------------------------------------------------------------------------
  const BULK_TAG_FILTER = {
    include: ["female"],
    exclude: ["futa", "futanari", "wlw", "demihumanuser"],
  };

  // SERVER lives in config.js (persisted, so the server can be remote).

  const TAG = "[aspect]";
  const log = (...a) => console.log(TAG, ...a);
  const warn = (...a) => console.warn(TAG, ...a);

  // ---------------------------------------------------------------------------
  // ServerClient — all traffic to the local Aspect Archive server goes via
  // GM_xmlhttpRequest so it's exempt from the page's CSP / mixed-content rules.
  // ---------------------------------------------------------------------------
  const ServerClient = {
    _request(opts) {
      return new Promise((resolve, reject) => {
        GM_xmlhttpRequest({
          method: opts.method || "GET",
          url: SERVER + opts.path,
          headers: SERVER_AUTH
            ? { "Content-Type": "application/json", Authorization: SERVER_AUTH }
            : { "Content-Type": "application/json" },
          data: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
          timeout: opts.timeout || 15000,
          onload: (r) => {
            if (r.status >= 200 && r.status < 300) {
              resolve({ status: r.status, text: r.responseText });
            } else {
              reject(new Error(`HTTP ${r.status}: ${r.responseText}`));
            }
          },
          onerror: () => reject(new Error("network error")),
          ontimeout: () => reject(new Error("timeout")),
        });
      });
    },

    async health() {
      const { text } = await this._request({ path: "/health" });
      return JSON.parse(text);
    },

    async relay(body) {
      const { text } = await this._request({
        method: "POST",
        path: "/v1/chat/completions",
        body,
        timeout: 120000,
      });
      return text;
    },

    async models() {
      const { text } = await this._request({ path: "/v1/models" });
      return text;
    },

    async build(payload) {
      const { text } = await this._request({
        method: "POST",
        path: "/build-jai",
        body: payload,
        timeout: 60000,
      });
      return JSON.parse(text);
    },

    // Which of these card ids are already saved on disk. Lets a bulk run skip
    // cards we already have before the slow per-card fetch loop.
    async existing(ids) {
      const { text } = await this._request({
        method: "POST",
        path: "/existing",
        body: { ids },
        timeout: 30000,
      });
      return JSON.parse(text).existing || [];
    },

    async captureStatus(name) {
      const { text } = await this._request({
        path: "/capture-status?name=" + encodeURIComponent(name),
      });
      return JSON.parse(text);
    },

    async clearCaptures() {
      const { text } = await this._request({
        method: "POST",
        path: "/clear-captures",
        timeout: 15000,
      });
      return JSON.parse(text);
    },
  };

  // ---------------------------------------------------------------------------
  // JanitorClient — reads a character (and its lorebooks) straight from
  // JanitorAI's own JSON API, replacing all DOM scraping.
  //
  //   GET /hampter/characters/<id>  → full card JSON (open cards carry the
  //                                   definition; hidden cards omit it)
  //   GET /hampter/script/<id>      → a public lorebook's entries
  //
  // Both need `Authorization: Bearer <supabase-JWT>` (cookies alone 401) and
  // must run IN THE PAGE: Cloudflare gates on the browser's TLS fingerprint +
  // cf_clearance cookie, and the token lives in the page's cookies/localStorage.
  // So a self-contained fetch is injected as a <script> (page context) and its
  // result crosses back via a one-shot CustomEvent (a JSON string — primitives
  // only, to sidestep Firefox Xray). Same mechanism the old lorebook scrape
  // used. findToken() is ported from ~/workspaces/JAR/src/autotrigger.js.
  // ---------------------------------------------------------------------------

  // Stringified + injected into the page — must be fully self-contained (no
  // closure over the userscript sandbox scope).
  function jaiAuthedFetchPage(eventName, url) {
    function findToken() {
      var b64 = function (s) {
        try { return atob(s); } catch (e) { /* */ }
        try { return atob(s.replace(/-/g, "+").replace(/_/g, "/")); } catch (e) { /* */ }
        return null;
      };
      var extract = function (rawIn) {
        var raw = rawIn;
        if (!raw) return null;
        try { raw = decodeURIComponent(raw); } catch (e) { /* */ }
        if (raw.indexOf("base64-") === 0) raw = raw.slice(7);
        if (raw.indexOf("eyJ") === 0 && raw.split(".").length === 3) return raw;
        var candidates = [b64(raw), raw];
        for (var ci = 0; ci < candidates.length; ci += 1) {
          var s = candidates[ci];
          if (!s) continue;
          var mm = s.match(/"access_token":"(eyJ[^"]+)"/);
          if (mm) return mm[1];
          try {
            var o = JSON.parse(s);
            var c = o && (o.access_token || o.accessToken || o.token
              || (o.currentSession && o.currentSession.access_token));
            if (typeof c === "string" && c.indexOf("eyJ") === 0) return c;
          } catch (e) { /* */ }
        }
        return null;
      };
      // Supabase chunks the auth cookie across sb-<ref>-auth-token(.0/.1/…);
      // reassemble each base's chunks in index order before extracting.
      try {
        var parts = {};
        var cookies = (document.cookie || "").split("; ");
        for (var i = 0; i < cookies.length; i += 1) {
          var c = cookies[i];
          var eq = c.indexOf("=");
          if (eq < 0) continue;
          var mm = c.slice(0, eq).match(/^(sb-.*-auth-token)(?:\.(\d+))?$/);
          if (!mm) continue;
          var base = mm[1];
          var idx = mm[2] ? parseInt(mm[2], 10) : 0;
          (parts[base] = parts[base] || {})[idx] = c.slice(eq + 1);
        }
        for (var b in parts) {
          var idxs = Object.keys(parts[b]).map(Number).sort(function (x, y) { return x - y; });
          var joined = "";
          for (var j = 0; j < idxs.length; j += 1) joined += parts[b][idxs[j]];
          var t = extract(joined);
          if (t) return t;
        }
      } catch (e) { /* */ }
      try {
        for (var k = 0; k < localStorage.length; k += 1) {
          var lt = extract(localStorage.getItem(localStorage.key(k)));
          if (lt) return lt;
        }
      } catch (e) { /* */ }
      return null;
    }

    var token = findToken();
    var headers = { accept: "application/json, text/plain, */*" };
    if (token) headers.authorization = "Bearer " + token;
    fetch(url, { credentials: "include", headers: headers })
      .then(function (r) {
        return r.text().then(function (body) {
          window.dispatchEvent(new CustomEvent(eventName, {
            detail: JSON.stringify({ status: r.status, body: body }),
          }));
        });
      })
      .catch(function (err) {
        window.dispatchEvent(new CustomEvent(eventName, {
          detail: JSON.stringify({ status: 0, body: String(err) }),
        }));
      });
  }

  // Inject jaiAuthedFetchPage for one URL, resolve with the response body text
  // (rejects on non-2xx or timeout).
  function pageAuthedFetch(url, timeoutMs = 20000) {
    return new Promise((resolve, reject) => {
      const evt = "jai-af-" + Math.random().toString(36).slice(2);
      let done = false;
      let timer = null;
      const finish = (fn) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        fn();
      };
      timer = setTimeout(() => finish(() => reject(new Error("timeout"))), timeoutMs);
      window.addEventListener(
        evt,
        (e) => {
          let res;
          try {
            res = JSON.parse(e.detail);
          } catch (err) {
            finish(() => reject(err));
            return;
          }
          if (res.status >= 200 && res.status < 300) {
            finish(() => resolve(res.body));
          } else {
            finish(() => reject(new Error(`HTTP ${res.status}: ${(res.body || "").slice(0, 200)}`)));
          }
        },
        { once: true }
      );
      const s = document.createElement("script");
      s.textContent = `(${jaiAuthedFetchPage.toString()})(${JSON.stringify(evt)}, ${JSON.stringify(url)});`;
      (document.head || document.documentElement).appendChild(s);
      s.remove();
    });
  }

  const JanitorClient = {
    async fetchCharacter(id) {
      const body = await pageAuthedFetch(`https://janitorai.com/hampter/characters/${id}`);
      return JSON.parse(body);
    },

    async fetchScript(id) {
      const body = await pageAuthedFetch(`https://janitorai.com/hampter/script/${id}`);
      return JSON.parse(body);
    },

    // One page of a creator's catalogue, keyed on the creator UUID. Rows carry
    // `showdefinition` (open/hidden) + `name` (the title blurb — `chat_name` is
    // null here, it only appears on the per-card fetch) and the envelope carries
    // `page` / `size` / `total` for pagination. Bracketed `user_id[]` is sent
    // literally, exactly as the site does.
    async listCharacters(creatorId, page = 1) {
      const url =
        `https://janitorai.com/hampter/characters?page=${page}` +
        `&language=en&sort=latest&user_id[]=${encodeURIComponent(creatorId)}`;
      const body = await pageAuthedFetch(url);
      return JSON.parse(body);
    },
  };

  // ---------------------------------------------------------------------------
  // Overlay widgets — a single fixed-position container (#aspect-root) holds,
  // top to bottom: the bulk panel (profile pages only), a transient status line,
  // the purple Export button, and the connection pill. All styling lives in one
  // injected <style> instead of per-element inline styles.
  // ---------------------------------------------------------------------------
  const OVERLAY_STYLE = `
    #aspect-root {
      position: fixed; bottom: 12px; right: 12px; z-index: 999999;
      display: flex; flex-direction: column; align-items: flex-end; gap: 8px;
      font-family: system-ui, sans-serif; font-size: 12px; pointer-events: none;
    }
    #aspect-status {
      pointer-events: auto; display: none; max-width: 260px;
      padding: 3px 8px; border-radius: 8px; color: #fff;
      background: rgba(0,0,0,.6); word-break: break-word;
    }
    #aspect-export {
      pointer-events: auto; padding: 10px 14px; border-radius: 10px; border: none;
      background: #7a3db5; color: #fff; font-family: inherit; font-size: 13px;
      font-weight: 600; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,.3);
    }
    #aspect-export:disabled { cursor: default; }
    #aspect-hide {
      pointer-events: auto; display: none; padding: 8px 12px; border-radius: 10px;
      border: none; background: #333; color: #fff; font-family: inherit;
      font-size: 12px; font-weight: 600; cursor: pointer;
      box-shadow: 0 2px 8px rgba(0,0,0,.3);
    }
    #aspect-hide.jai-on { background: #b5533d; }
    /* Class-only, reversible hide of cards not worth showing: already saved
       (jai-captured) or skipped by the tag filter (jai-filtered). The marker
       class lands on the card wrapper; the <html> flag flips them all at once
       so JAI's own inline styles are never disturbed. */
    html.jai-hide-captured .profile-character-card-wrapper.jai-captured,
    html.jai-hide-captured .profile-character-card-wrapper.jai-filtered {
      display: none !important;
    }
    #aspect-pill {
      display: flex; align-items: center; gap: 8px; padding: 4px 10px;
      border-radius: 999px; background: #222; color: #fff; border: 1px solid #444;
      opacity: 0.9;
    }
    #aspect-pill .jai-clear {
      pointer-events: auto; cursor: pointer; color: #ff9d9d; font-weight: bold;
      letter-spacing: 0.5px; border-left: 1px solid #555; padding-left: 8px;
    }
    #aspect-pill .jai-clear:hover { color: #ff6b6b; }
    #aspect-bulk {
      pointer-events: auto; display: none; flex-direction: column; gap: 6px;
      background: #222; color: #fff; border: 1px solid #444; border-radius: 8px;
      padding: 8px 10px; width: 300px; opacity: 0.96;
    }
    #aspect-bulk .jai-bulk-head {
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
    }
    #aspect-bulk .jai-bulk-title { font-weight: bold; }
    #aspect-bulk-btn {
      pointer-events: auto; padding: 6px 12px; border-radius: 8px; color: #fff;
      background: #7a3db5; border: none; cursor: pointer; font: inherit;
      font-weight: 600;
    }
    #aspect-bulk-btn:disabled { opacity: 0.6; cursor: default; }
    #aspect-bulk .jai-bulk-status { opacity: 0.85; word-break: break-word; }
    #aspect-bulk .jai-bulk-list {
      display: flex; flex-direction: column; gap: 2px;
      max-height: 260px; overflow-y: auto; margin-top: 2px;
    }
    #aspect-bulk .jai-bulk-row { display: flex; align-items: baseline; gap: 6px; }
    #aspect-bulk .jai-bulk-icon { flex: 0 0 1.2em; text-align: center; }
    #aspect-bulk .jai-bulk-name {
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
  `;

  // ---------------------------------------------------------------------------
  // ExportButton — one always-purple action button (mirrors the saucepan
  // bridge). Readiness is no longer color-coded on the button itself — the pill
  // already spells it out (ready ✓ / hidden ✓ / hidden ✗) — so the scheduler
  // only dims the button slightly when the card in view can't be exported yet.
  // ---------------------------------------------------------------------------
  const ExportButton = {
    _el: null,

    setReady(ready) {
      if (!this._el || this._el.disabled) return;
      this._el.style.opacity = ready ? "1" : "0.6";
    },

    onClick() {
      return exportCard();
    },
  };

  // ---------------------------------------------------------------------------
  // ExportStatus — the transient line that sits just above the button (like the
  // saucepan bridge's "Fetching…" / "✓ Saved" toast). Export progress lands
  // here now instead of hijacking the button's own label.
  // ---------------------------------------------------------------------------
  const ExportStatus = {
    _el: null,

    show(text, isError) {
      if (!this._el) return;
      this._el.textContent = text;
      this._el.style.display = "block";
      this._el.style.background = isError ? "rgba(150,30,30,.85)" : "rgba(0,0,0,.6)";
    },

    hide() {
      if (this._el) this._el.style.display = "none";
    },
  };

  // ---------------------------------------------------------------------------
  // Pill — connection indicator + inline CLEAR affordance. CLEAR wipes the
  // server-side capture cache AND resets the plugin's remembered card state
  // (last id / hidden flag). Exported PNGs are untouched. flash() shows
  // transient feedback the scheduler's setStatus() won't overwrite until the
  // hold expires.
  // ---------------------------------------------------------------------------
  const Pill = {
    _el: null,
    _statusEl: null,
    _holdUntil: 0,

    setStatus(text) {
      if (!this._statusEl || Date.now() < this._holdUntil) return;
      this._statusEl.textContent = text;
    },

    flash(text, ms) {
      if (!this._statusEl) return;
      this._statusEl.textContent = text;
      this._holdUntil = Date.now() + ms;
    },

    async clear() {
      if (
        !window.confirm(
          "Clear Aspect Archive capture cache (captured hidden definitions + greetings) " +
            "and reset the plugin's remembered card state? Exported PNGs are not affected."
        )
      ) {
        return;
      }
      resetPluginState();
      this.flash("⏳ clearing…", 4000);
      try {
        const result = await ServerClient.clearCaptures();
        log("cleared captures ->", result.removed, "+ plugin state");
        this.flash(`✅ cleared ${result.removed}`, 3000);
      } catch (err) {
        warn("clear failed", err);
        this.flash("⚠️ clear failed", 6000);
      }
    },
  };

  // ---------------------------------------------------------------------------
  // Overlay — builds and keeps alive the single container. The watchdog
  // observes only <html>'s direct children (childList, NO subtree), so it does
  // not fire on every streamed chat token; it wakes only if our root detaches.
  // ---------------------------------------------------------------------------
  const Overlay = {
    _root: null,

    mount() {
      if (this._root && this._root.isConnected) return;

      if (!document.getElementById("aspect-style")) {
        const style = document.createElement("style");
        style.id = "aspect-style";
        style.textContent = OVERLAY_STYLE;
        (document.head || document.documentElement).appendChild(style);
      }

      const root = document.createElement("div");
      root.id = "aspect-root";

      const status = document.createElement("div");
      status.id = "aspect-status";

      const btn = document.createElement("button");
      btn.id = "aspect-export";
      btn.textContent = "⬇ Export to card";
      btn.addEventListener("click", () => ExportButton.onClick());

      const hideBtn = document.createElement("button");
      hideBtn.id = "aspect-hide";
      hideBtn.textContent = "🙈 Hide saved";
      hideBtn.title =
        "Hide cards already saved to disk, plus cards the include/exclude tag " +
        "filter would skip — on the homepage, search, or a creator page";
      hideBtn.addEventListener("click", () => HideCaptured.onClick());

      const pill = document.createElement("div");
      pill.id = "aspect-pill";
      const pillStatus = document.createElement("span");
      pillStatus.textContent = "⚪ Aspect";
      const clear = document.createElement("span");
      clear.className = "jai-clear";
      clear.textContent = "CLEAR";
      clear.title = "Clear server capture cache + reset remembered card state";
      clear.addEventListener("click", () => Pill.clear());
      pill.append(pillStatus, clear);

      root.append(BulkPanel.build(), hideBtn, status, btn, pill);
      document.documentElement.appendChild(root);

      ExportButton._el = btn;
      ExportStatus._el = status;
      Pill._el = pill;
      Pill._statusEl = pillStatus;
      HideCaptured.setButton(hideBtn);
      this._root = root;
    },

    keepAlive() {
      let scheduled = false;
      new MutationObserver(() => {
        if (scheduled || (this._root && this._root.isConnected)) return;
        scheduled = true;
        requestAnimationFrame(() => {
          scheduled = false;
          this.mount();
        });
      }).observe(document.documentElement, { childList: true });
    },
  };

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
  // Scheduler — one poll drives the pill + button. Paused while the tab is
  // hidden; backs off to 15s while the server is unreachable. A single
  // self-scheduling timer runs at a time; returning to the tab wakes it.
  //
  // Pill vocabulary (shared with the saucepan bridge):
  //   🟢 Aspect                  — connected, no card in view
  //   🟢 Aspect · ready ✓        — open card, exportable right now
  //   🟢 Aspect · hidden ✓       — hidden card, definition + greeting captured
  //   🟢 Aspect · hidden ✗       — hidden card, nothing captured yet
  //   🔴 Aspect (server down)    — server unreachable
  // ---------------------------------------------------------------------------
  let serverDown = false;
  let pollTimer = null;

  async function tick() {
    await CharacterState.refresh();

    // The bulk "download all open cards" panel lives on creator profile pages
    // only; show/hide it as the SPA navigates.
    BulkPanel.toggle(!!currentCreatorId());

    // The "hide saved cards" toggle lives on any listing surface: show its
    // button where cards render and, while active, fold in cards that
    // infinite-scroll has appended since the last tick.
    await HideCaptured.refresh();

    const onChar = !!currentCharacterId();
    const onChat = isChatView();
    const id = currentCharacterId() || GM_getValue(KEY_ID, "");
    const hidden = GM_getValue(KEY_HIDDEN, false);

    try {
      let ready = false;
      let statusText;
      if ((!onChar && !onChat) || !id) {
        await ServerClient.health();
        statusText = "🟢 Aspect";
      } else if (!hidden) {
        // Open card: the JSON carries everything; nothing to capture.
        await ServerClient.health();
        statusText = "🟢 Aspect · ready ✓";
        ready = true;
      } else {
        // Hidden card: the definition + primary greeting come from the chat
        // relay capture. Resolve THIS card's name (transient, from the JSON by
        // id) purely to ask the server whether its captures are present.
        const json = await CharacterState.ensureJson(id);
        const status = await ServerClient.captureStatus(characterName(json));
        const has = !!(status.system && status.greetings);
        statusText = `🟢 Aspect · hidden ${has ? "✓" : "✗"}`;
        ready = has;
      }
      serverDown = false;
      Pill.setStatus(statusText);
      ExportButton.setReady(ready);
    } catch {
      serverDown = true;
      Pill.setStatus("🔴 Aspect (server down)");
      ExportButton.setReady(false);
    }
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

  // ---------------------------------------------------------------------------
  // Export — the whole card export is now: fetch the character JSON, fetch its
  // public lorebooks, POST /build-jai. No DOM scraping, no greeting carousel walk.
  // Hidden cards work the same way: their definition + primary greeting are
  // already captured server-side from the chat relay, and the server merges
  // them with the JSON's alternate greetings / metadata.
  // ---------------------------------------------------------------------------
  async function fetchPublicLorebooks(character) {
    const scripts = Array.isArray(character.scripts) ? character.scripts : [];
    const ids = scripts
      .filter((s) => s && s.type === "lorebook" && s.is_public && s.id)
      .map((s) => s.id);
    if (!ids.length) return [];
    const results = await Promise.all(
      ids.map(async (id) => {
        try {
          return { id, raw: await JanitorClient.fetchScript(id) };
        } catch (err) {
          warn("lorebook fetch failed", id, err);
          return null;
        }
      })
    );
    const ok = results.filter(Boolean);
    log(`lorebooks: ${ok.length}/${ids.length} public fetched`);
    return ok;
  }

  // Resolve a character JSON, preferring the scheduler's per-page cache so a
  // freshly-viewed character page isn't refetched.
  async function resolveCharacter(id) {
    return CharacterState.cachedJson(id) || (await JanitorClient.fetchCharacter(id));
  }

  // The reusable core of a card export: resolve the character JSON (unless one
  // is supplied), fetch its public lorebooks, POST /build-jai. Shared by the single
  // Export-card button and the bulk "download all open cards" panel. The card
  // name is no longer sent from here — the server derives the real character
  // name from the API JSON (chat_name), so there's nothing to guess or prompt
  // for. Returns the server result plus the resolved character.
  //   opts.character — a pre-fetched JSON, to avoid a second round-trip
  //   opts.url       — source URL recorded on the card (defaults to the id URL)
  async function buildCardById(id, opts = {}) {
    const character = opts.character || (await resolveCharacter(id));
    const lorebooks = await fetchPublicLorebooks(character);
    const payload = {
      character: { id, url: opts.url || CHAR_BASE + id },
      character_json: character,
      avatar_url: character.avatar ? AVATAR_BASE + character.avatar : null,
      lorebooks,
    };
    const result = await ServerClient.build(payload);
    return { result, character };
  }

  // Progress lands in the status line above the button (ExportStatus), so the
  // button keeps its label and just disables for the duration.
  async function exportCard() {
    const el = ExportButton._el;
    el.disabled = true;
    ExportStatus.show("Exporting…");
    let holdMs = 2500;
    try {
      const id = currentCharacterId() || GM_getValue(KEY_ID, "");
      if (!id) {
        ExportStatus.show("⚠️ Open a character page first", true);
        holdMs = 5000;
        return;
      }

      // No name prompt: the server derives the real character name from the
      // API JSON (chat_name) and saves the card under it.
      const { result } = await buildCardById(id, { url: location.href });
      const warnings = result.warnings || [];
      if (result.ok && result.duplicate) {
        // Already on disk -- the server wrote nothing. Say so rather than "✓
        // Saved", which would claim an export that didn't happen.
        log("already saved:", result.path);
        ExportStatus.show("• Already saved");
        holdMs = 4000;
      } else if (result.ok) {
        log("exported card ->", result.path, warnings);
        if (warnings.length) {
          const n = warnings.length;
          ExportStatus.show(`⚠️ Saved — ${n} warning${n === 1 ? "" : "s"}`, false);
          el.title = warnings.join("\n");
          holdMs = 8000;
          warn("export warnings:", warnings);
        } else {
          ExportStatus.show("✓ Saved");
        }
      } else {
        ExportStatus.show(`⚠️ ${warnings[0] || "failed"}`, true);
        holdMs = 8000;
        warn("export failed", result);
      }
    } catch (err) {
      ExportStatus.show("⚠️ Failed", true);
      holdMs = 8000;
      warn("export failed", err);
    } finally {
      setTimeout(() => {
        ExportStatus.hide();
        el.title = "";
        el.disabled = false;
      }, holdMs);
    }
  }

  // ---------------------------------------------------------------------------
  // Bulk export — on a creator's /profiles/<uuid>_… page, enumerate the whole
  // catalogue and export every OPEN card, reusing the per-card buildCardById
  // path. Hidden cards are listed but skipped; auto-capturing hidden
  // definitions in bulk (create a chat + send a message per card) is deferred.
  //
  // IMPORTANT: the creator/list endpoint reports every row as is_public:true /
  // showdefinition:false as CONSTANT PLACEHOLDERS (verified across ~6 creators)
  // — it cannot tell open from hidden. Only the per-card /hampter/characters/<id>
  // response carries the real `showdefinition`. So we enumerate ids from the
  // list, then fetch each card to classify + export. Userscript-only; every
  // open card still goes through the same server /build-jai as the single button.
  // ---------------------------------------------------------------------------
  const LIST_PAGE_DELAY_MS = 600;    // between list-endpoint pages
  const CARD_BUILD_DELAY_MS = 1500;  // after exporting an open card (writes a PNG)
  const CARD_SKIP_DELAY_MS = 400;    // after skipping a hidden card (just a GET)
  const MAX_LIST_PAGES = 100;        // safety valve

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // ---------------------------------------------------------------------------
  // Tag filter (BULK_TAG_FILTER, from config.js) — applied to the cheap list
  // rows so filtered-out cards never trigger a per-card fetch.
  // ---------------------------------------------------------------------------

  // Normalize a tag for comparison: drop a leading emoji / "#" / punctuation run
  // (JanitorAI prefixes official tags with an emoji) then lower-case. Mirrors the
  // server's clean_tag so "👩‍🦰 Female" and "female" compare equal.
  function normTag(t) {
    return String(t == null ? "" : t)
      .replace(/^[^\p{L}\p{N}]+/u, "")
      .trim()
      .toLowerCase();
  }

  const _INCLUDE = (BULK_TAG_FILTER.include || []).map(normTag).filter(Boolean);
  const _EXCLUDE = (BULK_TAG_FILTER.exclude || []).map(normTag).filter(Boolean);

  // Every tag on a list row, normalized: official `tags[].name` + `custom_tags`.
  function rowTagSet(row) {
    const out = new Set();
    for (const t of row.tags || []) {
      if (t && t.name) out.add(normTag(t.name));
    }
    for (const c of row.custom_tags || []) out.add(normTag(c));
    out.delete("");
    return out;
  }

  // A card passes when it carries at least one include tag (or include is empty)
  // AND none of the exclude tags.
  function passesTagFilter(row) {
    const tset = rowTagSet(row);
    if (_INCLUDE.length && !_INCLUDE.some((t) => tset.has(t))) return false;
    if (_EXCLUDE.some((t) => tset.has(t))) return false;
    return true;
  }

  // Human-readable one-liner for the active filter, or "" when none is set.
  function filterSummary() {
    const parts = [];
    if (_INCLUDE.length) parts.push(`only ${_INCLUDE.join(", ")}`);
    if (_EXCLUDE.length) parts.push(`no ${_EXCLUDE.join(", ")}`);
    return parts.join(" · ");
  }

  // Creator UUID from a /profiles/<uuid>_<slug> path — the leading 36-char UUID
  // the list endpoint keys on via user_id[]=. Mirrors currentCharacterId().
  function currentCreatorId() {
    const m = location.pathname.match(/\/profiles\/([0-9a-f-]{36})/i);
    return m ? m[1].toLowerCase() : null;
  }

  // Every card id + display name in a creator's catalogue that passes the tag
  // filter. `chat_name` is null in list rows, so the label is the `name` blurb;
  // the real name is resolved per-card at build time. Pagination keys on the
  // full scanned count vs the envelope's `total` (not the kept count, which the
  // filter shrinks). Returns { rows, scanned, skipped }.
  async function enumerateCards(creatorId, onProgress) {
    const rows = [];
    let scanned = 0;
    let skipped = 0;
    for (let page = 1; page <= MAX_LIST_PAGES; page += 1) {
      const res = await JanitorClient.listCharacters(creatorId, page);
      const data = Array.isArray(res.data) ? res.data : [];
      for (const c of data) {
        if (!c || !c.id) continue;
        scanned += 1;
        if (!passesTagFilter(c)) {
          skipped += 1;
          continue;
        }
        rows.push({ id: c.id, name: (c.name || "").trim() || c.id });
      }
      const total = Number(res.total) || scanned;
      if (onProgress) onProgress(scanned, total, rows.length);
      if (!data.length || scanned >= total) break;
      await sleep(LIST_PAGE_DELAY_MS);
    }
    return { rows, scanned, skipped };
  }

  // ---------------------------------------------------------------------------
  // BulkPanel — the profile-page widget: a Download button, a status line, and
  // a scrollable list of rows whose leading icon tracks each card's fate
  // (⏳ pending/working · ✓ saved · ✗ hidden-skipped · ⚠ failed).
  // ---------------------------------------------------------------------------
  const BulkPanel = {
    _el: null,
    _btn: null,
    _status: null,
    _list: null,
    _rows: new Map(),
    _running: false,

    build() {
      const el = document.createElement("div");
      el.id = "aspect-bulk";

      const head = document.createElement("div");
      head.className = "jai-bulk-head";
      const title = document.createElement("span");
      title.className = "jai-bulk-title";
      title.textContent = "⬇ All open cards";
      const btn = document.createElement("button");
      btn.id = "aspect-bulk-btn";
      btn.textContent = "Download";
      btn.addEventListener("click", () => this.run());
      head.append(title, btn);

      const status = document.createElement("div");
      status.className = "jai-bulk-status";
      const fs = filterSummary();
      status.textContent = fs ? `filter: ${fs} — click Download` : "creator profile — click Download";

      const list = document.createElement("div");
      list.className = "jai-bulk-list";

      el.append(head, status, list);
      this._el = el;
      this._btn = btn;
      this._status = status;
      this._list = list;
      return el;
    },

    toggle(show) {
      if (this._el) this._el.style.display = show ? "flex" : "none";
    },

    _setStatus(text) {
      if (this._status) this._status.textContent = text;
    },

    _renderRows(rows) {
      this._list.innerHTML = "";
      this._rows.clear();
      for (const r of rows) {
        const row = document.createElement("div");
        row.className = "jai-bulk-row";
        const icon = document.createElement("span");
        icon.className = "jai-bulk-icon";
        icon.textContent = "⏳";
        const name = document.createElement("span");
        name.className = "jai-bulk-name";
        name.textContent = r.name;
        row.title = "pending";
        row.append(icon, name);
        this._list.append(row);
        this._rows.set(r.id, { icon, row });
      }
    },

    _setRow(id, iconText, title) {
      const entry = this._rows.get(id);
      if (!entry) return;
      entry.icon.textContent = iconText;
      if (title) entry.row.title = title;
    },

    async run() {
      if (this._running) return;
      const creatorId = currentCreatorId();
      if (!creatorId) {
        this._setStatus("⚠️ open a creator's profile page first");
        return;
      }
      this._running = true;
      this._btn.disabled = true;
      try {
        this._setStatus("⏳ listing cards…");
        const { rows, scanned, skipped } = await enumerateCards(creatorId, (n, total) =>
          this._setStatus(`⏳ listing cards… ${n}/${total}`)
        );
        if (!scanned) {
          this._setStatus("no cards found for this creator");
          return;
        }
        if (!rows.length) {
          this._setStatus(`no cards matched the tag filter (scanned ${scanned})`);
          return;
        }
        this._renderRows(rows);

        // Skip cards already on disk. The `_<id8>` fragment in each saved
        // filename is knowable from a list row (unlike the real name), so the
        // server can tell us which ids exist in one cheap call — those drop out
        // before the slow one-at-a-time classify/build loop. Fails soft: if the
        // check errors, we just export everything.
        this._setStatus("⏳ checking which cards are already saved…");
        let onDisk = new Set();
        try {
          onDisk = new Set(await ServerClient.existing(rows.map((r) => r.id)));
        } catch (err) {
          warn("existing-check failed; exporting all", err);
        }
        const pending = [];
        for (const r of rows) {
          if (onDisk.has(r.id)) {
            this._setRow(r.id, "•", "already saved — skipped");
          } else {
            pending.push(r);
          }
        }
        let already = rows.length - pending.length;
        if (!pending.length) {
          this._setStatus(`all ${rows.length} card(s) already saved — nothing to do`);
          return;
        }

        // The listing can't tell open from hidden, so every kept card is fetched
        // individually — warn before a long one-at-a-time run.
        const filterNote = skipped ? `\n\n${skipped} card(s) excluded by the tag filter.` : "";
        const onDiskNote = already ? `\n\n${already} card(s) already saved — skipped.` : "";
        if (
          !window.confirm(
            `Check ${pending.length} card(s) for open definitions and download each ` +
              "open one?\n\nThe listing can't tell open from hidden, so each card " +
              "is fetched one at a time. Hidden cards are skipped." +
              onDiskNote +
              filterNote
          )
        ) {
          this._setStatus(`${pending.length} cards — cancelled`);
          return;
        }

        let done = 0;
        let failed = 0;
        let hidden = 0;
        for (let i = 0; i < pending.length; i += 1) {
          const r = pending[i];
          this._setStatus(`(${i + 1}/${pending.length}) ✓${done} ✗${hidden} ⚠${failed} — ${r.name}`);
          let built = false;
          try {
            this._setRow(r.id, "⏳", "checking…");
            const character = await resolveCharacter(r.id);
            if (character.showdefinition === false) {
              hidden += 1;
              this._setRow(r.id, "✗", "hidden — skipped (needs a chat capture)");
            } else {
              this._setRow(r.id, "⏳", "exporting…");
              const { result } = await buildCardById(r.id, { character });
              const warnings = (result && result.warnings) || [];
              if (result && result.ok && result.duplicate) {
                // The pre-check above normally catches these; this covers the
                // case where it failed soft (or a card landed on disk since).
                already += 1;
                this._setRow(r.id, "•", "already saved — skipped");
              } else if (result && result.ok) {
                done += 1;
                built = true;
                this._setRow(
                  r.id,
                  "✓",
                  warnings.length ? `saved — ${warnings.length} warning(s)` : (result.path || "saved")
                );
              } else {
                failed += 1;
                this._setRow(r.id, "⚠", warnings[0] || "build failed");
                warn("bulk build failed", r.id, result);
              }
            }
          } catch (err) {
            failed += 1;
            this._setRow(r.id, "⚠", String(err));
            warn("bulk card error", r.id, err);
          }
          if (i < pending.length - 1) {
            await sleep(built ? CARD_BUILD_DELAY_MS : CARD_SKIP_DELAY_MS);
          }
        }
        const filtered = skipped ? ` · ⊘ ${skipped} filtered` : "";
        const existingNote = already ? ` · • ${already} already saved` : "";
        this._setStatus(
          `done · ✓ ${done} saved · ✗ ${hidden} hidden · ⚠ ${failed} failed${existingNote}${filtered}`
        );
      } catch (err) {
        warn("bulk run failed", err);
        this._setStatus("⚠️ " + String(err));
      } finally {
        this._running = false;
        this._btn.disabled = false;
      }
    },
  };

  // ---------------------------------------------------------------------------
  // HideCaptured — a browse-page toggle that folds away character cards not
  // worth showing, so scrolling a listing only surfaces cards worth downloading.
  // A card is folded away when EITHER:
  //   • it's already exported to disk, OR
  //   • the include/exclude tag filter (BULK_TAG_FILTER) would skip it — the
  //     same filter the bulk sweep uses to pick cards.
  //
  // This works on EVERY surface that renders the shared card tile — the
  // logged-in homepage (/), search, tag browse, creator profiles — because both
  // verdicts are read off the rendered tile itself rather than from any
  // page-specific list endpoint:
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
  // Disk membership goes through the SAME `POST /existing` id-fragment check the
  // bulk sweep uses (it matches the `_<id8>` filename fragment) — no new server
  // endpoint. A tag-filtered card is hidden up front and never touches it.
  // Only the /existing verdicts are memoized (they cost a round trip); the tag
  // verdict is re-derived every scan, so a wrapper React recycles for a
  // different card can never keep a stale marker.
  //
  // Hiding is class-only and fully reversible: a folded card's wrapper gets
  // `jai-captured` (on disk) or `jai-filtered` (tag-filtered), and while the
  // toggle is on `html.jai-hide-captured` drives the real `display:none` via CSS
  // — JAI's own inline styles are never touched. Re-scanning on the scheduler
  // tick folds in cards infinite-scroll adds.
  // ---------------------------------------------------------------------------
  const CARD_LINK_SEL = "a.profile-character-card-stack-link-component";
  const CARD_WRAP_SEL = ".profile-character-card-wrapper";
  const CARD_LIST_SEL = ".pp-cc-list-container"; // direct parent of the wrappers
  const CARD_TAGS_SEL = ".profile-character-card-tags"; // chip container on a tile
  const CARD_TAG_SEL =
    ".profile-character-card-tags-wrap-regular, .profile-character-card-tags-wrap-custom";
  const HIDE_ACTIVE_CLASS = "jai-hide-captured"; // on <html> while the toggle is on
  const CARD_SAVED_CLASS = "jai-captured";       // on a wrapper whose card is on disk
  const CARD_FILTERED_CLASS = "jai-filtered";    // on a wrapper the tag filter skips
  const CARD_ID_ATTR = "data-jai-id";            // resolved id, stamped for debugging

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

  const HideCaptured = {
    _btn: null,
    _active: false,
    _checked: new Map(),   // id -> bool (true = already on disk); memoized /existing
    _busy: false,
    _observer: null,       // watches the card grid for page changes
    _observed: null,       // the list container currently observed
    _debounce: null,       // coalesces a burst of grid mutations into one _sync

    setButton(btn) {
      this._btn = btn;
      this._render();
    },

    // Show the toggle only where cards actually render; hide it elsewhere.
    _toggleButton(show) {
      if (this._btn) this._btn.style.display = show ? "block" : "none";
    },

    _render() {
      if (!this._btn) return;
      if (this._active) {
        const n = document.querySelectorAll(
          `${CARD_WRAP_SEL}.${CARD_SAVED_CLASS}, ${CARD_WRAP_SEL}.${CARD_FILTERED_CLASS}`
        ).length;
        this._btn.textContent = `👁 Show hidden (${n})`;
      } else {
        this._btn.textContent = "🙈 Hide saved";
      }
      this._btn.classList.toggle("jai-on", this._active);
    },

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

    // Fold away cards not worth showing: tag-filtered ones hide on their chips
    // alone (and skip the disk check), and the rest are classified via
    // /existing (memoized) and hidden when already saved. Safe to call every
    // tick; the /existing guard keeps overlapping ticks from double-firing, and
    // ids left unclassified are simply retried on the next scan.
    async _sync() {
      const cards = this._scan();

      // Disk-check only cards the tag filter KEEPS; a filtered-out card is
      // hidden by its chips alone and never costs a round trip.
      const unknown = [
        ...new Set(
          cards.filter((c) => !c.filtered && !this._checked.has(c.id)).map((c) => c.id)
        ),
      ];
      if (unknown.length && !this._busy) {
        this._busy = true;
        try {
          const saved = new Set(await ServerClient.existing(unknown));
          for (const id of unknown) this._checked.set(id, saved.has(id));
        } catch (err) {
          warn("hide-captured: existing check failed", err);
          return; // leave unknowns unclassified; retried on the next scan
        } finally {
          this._busy = false;
        }
      }

      for (const { id, wrap, filtered } of cards) {
        wrap.classList.toggle(CARD_FILTERED_CLASS, filtered);
        wrap.classList.toggle(CARD_SAVED_CLASS, !filtered && this._checked.get(id) === true);
      }
      this._render();
    },

    // While active, watch the card grid so a pagination click (prev/next or a
    // page button) reapplies the hide state immediately instead of waiting for
    // the next 5s tick. The wrappers are direct children of CARD_LIST_SEL, so
    // childList there fires exactly when JAI swaps in a new page's cards — after
    // React has rendered them, sidestepping any fetch-vs-render race. The
    // container is replaced across SPA nav, so re-attach when it changes and
    // disconnect when the toggle goes off. A short debounce coalesces the
    // remove-old/add-new mutation burst into a single re-sync.
    _watch() {
      const container = this._active ? document.querySelector(CARD_LIST_SEL) : null;
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

    // Scheduler hook: keep the button scoped to listing surfaces and, while
    // active, fold in cards that infinite-scroll has appended since last tick.
    // Also (re)binds the pagination watcher as the SPA swaps the grid in/out.
    async refresh() {
      this._toggleButton(!!document.querySelector(CARD_LINK_SEL));
      this._watch();
      if (this._active) await this._sync();
    },

    async onClick() {
      this._active = !this._active;
      document.documentElement.classList.toggle(HIDE_ACTIVE_CLASS, this._active);
      this._watch();
      this._render();
      if (this._active) await this._sync();
    },
  };

  // ---------------------------------------------------------------------------
  // FetchHook — patch window.fetch and XMLHttpRequest at document-start so
  // JanitorAI's chat-completion request is intercepted before its app code ever
  // runs, relayed through the Aspect Archive server (which answers it AND captures
  // the hidden definition + primary greeting). Everything that doesn't look
  // like a chat-completion / models probe passes through untouched.
  //
  // ⚠ THE HOOK HAS TO RUN IN THE *PAGE* REALM. Tampermonkey runs this script in
  // a sandbox whose `window` is not the page's window, so the obvious
  // `window.fetch = ...` patches a copy JanitorAI's app code never calls. That
  // is not a theoretical worry: it is why this hook did nothing for a year
  // (docs' "M1 gate — no CSP, interception unnecessary" finding) and why nobody
  // noticed — the server was on 127.0.0.1, and a browser exempts loopback from
  // mixed-content blocking, so JanitorAI's own unintercepted https→http call
  // reached it directly and the captures landed anyway. Point the bridge at a
  // container on the LAN (docs/DEPLOY.md) and that free ride ends: https
  // JanitorAI → http://192.168.x.x is blocked before it leaves the page, and
  // chat dies with "A network error occurred...".
  //
  // So the hook is *injected* as an inline <script>: it runs as page code, in
  // the page's own realm, patching the fetch JanitorAI actually calls. It has
  // no GM_* access there, so the two calls it needs are bridged back over
  // CustomEvents to this sandbox, which does them with GM_xmlhttpRequest — the
  // only transport exempt from the page's mixed-content and CSP rules.
  // ---------------------------------------------------------------------------
  function looksLikeChatCompletion(url, bodyText) {
    if (url && url.includes("chat/completions")) return true;
    if (!bodyText) return false;
    try {
      const parsed = JSON.parse(bodyText);
      return Array.isArray(parsed.messages) && "model" in parsed;
    } catch {
      return false;
    }
  }

  // Scoped to our own configured endpoint host so we never shadow JanitorAI's
  // own unrelated "/models" calls elsewhere on the site.
  function looksLikeModelsProbe(method, url) {
    if ((method || "GET").toUpperCase() !== "GET" || !url) return false;
    return url.startsWith(SERVER) && url.includes("/models");
  }

  // The two CustomEvent names the injected page hook and this sandbox talk over.
  // Only JSON strings cross the boundary: a primitive is legible from both
  // realms in every browser, where an object would need cloneInto() in Firefox.
  const BRIDGE_REQUEST = "aspect:relay-request";
  const BRIDGE_RESPONSE = "aspect:relay-response";
  // Set on <html> by the injected script itself, and read back synchronously —
  // an inline <script> executes on append, so if the attribute is missing the
  // injection was blocked and we know it rather than guessing later.
  const HOOK_MARKER = "data-aspect-hook";

  // ---------------------------------------------------------------------------
  // pageWorldHook — runs in the PAGE realm, not here.
  //
  // Stringified into the injected <script>, so it may only close over names the
  // injected preamble also defines: SERVER, looksLikeChatCompletion,
  // looksLikeModelsProbe and the three constants above. No GM_*, no
  // ServerClient, no log() from client-server.js — everything it needs is
  // either a parameter-free constant re-emitted by pageHookSource() or defined
  // inside this function.
  // ---------------------------------------------------------------------------
  function pageWorldHook() {
    const TAG = "[aspect/page]";
    const plog = (...a) => console.log(TAG, ...a);
    const pwarn = (...a) => console.warn(TAG, ...a);

    // Bridge: ask the userscript sandbox to make a call for us.
    const pending = new Map();
    let seq = 0;

    document.addEventListener(BRIDGE_RESPONSE, (event) => {
      let message;
      try {
        message = JSON.parse(event.detail);
      } catch {
        return;
      }
      const entry = pending.get(message.id);
      if (!entry) return;
      pending.delete(message.id);
      if (message.ok) entry.resolve(message.text);
      else entry.reject(new Error(message.error || "relay failed"));
    });

    function askSandbox(kind, body) {
      return new Promise((resolve, reject) => {
        const id = ++seq;
        pending.set(id, { resolve, reject });
        document.dispatchEvent(
          new CustomEvent(BRIDGE_REQUEST, {
            detail: JSON.stringify({ id, kind, body: body === undefined ? null : body }),
          })
        );
      });
    }

    // A relayed reply is handed back as the site's own Response. Streaming
    // requests get the whole SSE body in one go: GM_xmlhttpRequest can only
    // resolve once, and the mock responder emits its frames without delay, so
    // there is nothing to trickle out anyway. The content type still has to
    // match what was asked for or the reader never parses it.
    function replyResponse(replyText, streaming) {
      return new Response(replyText, {
        status: 200,
        headers: {
          "Content-Type": streaming ? "text/event-stream" : "application/json",
        },
      });
    }

    function isStreaming(parsedBody) {
      return Boolean(parsedBody && parsedBody.stream);
    }

    const originalFetch = window.fetch;
    window.fetch = async function (input, init) {
      const url = typeof input === "string" ? input : input?.url;
      const method = init?.method || "GET";
      const bodyText = init?.body;

      if (looksLikeModelsProbe(method, url)) {
        try {
          return replyResponse(await askSandbox("models"), false);
        } catch (err) {
          pwarn("models probe relay failed, falling through", err);
        }
      }

      if (looksLikeChatCompletion(url, typeof bodyText === "string" ? bodyText : null)) {
        try {
          const parsedBody = typeof bodyText === "string" ? JSON.parse(bodyText) : bodyText;
          plog("relaying chat/completions via fetch", url);
          return replyResponse(await askSandbox("relay", parsedBody), isStreaming(parsedBody));
        } catch (err) {
          pwarn("relay failed, falling through to original fetch", err);
        }
      }

      return originalFetch.apply(this, arguments);
    };

    const OriginalXHR = window.XMLHttpRequest;
    const originalOpen = OriginalXHR.prototype.open;
    const originalSend = OriginalXHR.prototype.send;

    OriginalXHR.prototype.open = function (method, url, ...rest) {
      this.__aspectUrl = url;
      this.__aspectMethod = method;
      return originalOpen.call(this, method, url, ...rest);
    };

    function resolveWith(xhr, replyText) {
      Object.defineProperty(xhr, "readyState", { value: 4, configurable: true });
      Object.defineProperty(xhr, "status", { value: 200, configurable: true });
      Object.defineProperty(xhr, "responseText", { value: replyText, configurable: true });
      Object.defineProperty(xhr, "response", { value: replyText, configurable: true });
      xhr.dispatchEvent(new Event("readystatechange"));
      xhr.dispatchEvent(new Event("load"));
      xhr.dispatchEvent(new Event("loadend"));
    }

    OriginalXHR.prototype.send = function (body) {
      const url = this.__aspectUrl || "";
      const method = this.__aspectMethod || "GET";
      const bodyText = typeof body === "string" ? body : null;
      const xhr = this;

      if (looksLikeModelsProbe(method, url)) {
        askSandbox("models")
          .then((replyText) => resolveWith(xhr, replyText))
          .catch((err) => {
            pwarn("XHR models probe relay failed, falling through", err);
            originalSend.call(xhr, body);
          });
        return;
      }

      if (looksLikeChatCompletion(url, bodyText)) {
        const parsedBody = bodyText ? JSON.parse(bodyText) : {};
        plog("relaying chat/completions via XHR", url);
        askSandbox("relay", parsedBody)
          .then((replyText) => resolveWith(xhr, replyText))
          .catch((err) => {
            pwarn("XHR relay failed, falling through to original send", err);
            originalSend.call(xhr, body);
          });
        return;
      }

      return originalSend.call(this, body);
    };

    document.documentElement.setAttribute(HOOK_MARKER, "1");
    plog("page-world fetch hook installed");
  }

  // The <script> body: the constants pageWorldHook closes over, the two
  // matchers verbatim (one source of truth — the copy the tests exercise is the
  // copy the page runs), then the call.
  function pageHookSource() {
    return [
      "(function () {",
      '  "use strict";',
      "  const SERVER = " + JSON.stringify(SERVER) + ";",
      "  const BRIDGE_REQUEST = " + JSON.stringify(BRIDGE_REQUEST) + ";",
      "  const BRIDGE_RESPONSE = " + JSON.stringify(BRIDGE_RESPONSE) + ";",
      "  const HOOK_MARKER = " + JSON.stringify(HOOK_MARKER) + ";",
      String(looksLikeChatCompletion),
      String(looksLikeModelsProbe),
      "  (" + String(pageWorldHook) + ")();",
      "})();",
    ].join("\n");
  }

  const FetchHook = {
    install() {
      this._listenForBridge();
      if (this._injectPageHook()) return;
      warn(
        "page-world hook could not be installed (blocked inline script?) — " +
          "JanitorAI's chat request will go out unintercepted, which only works " +
          "if it can reach " +
          SERVER +
          " on its own (loopback, or https)"
      );
    },

    // Sandbox side of the bridge: the page hook can't use GM_xmlhttpRequest, we
    // can. Both calls already exist on ServerClient; this only routes to them.
    _listenForBridge() {
      document.addEventListener(BRIDGE_REQUEST, (event) => {
        let message;
        try {
          message = JSON.parse(event.detail);
        } catch {
          return;
        }
        const reply = (payload) =>
          document.dispatchEvent(
            new CustomEvent(BRIDGE_RESPONSE, {
              detail: JSON.stringify({ id: message.id, ...payload }),
            })
          );
        const call =
          message.kind === "models" ? ServerClient.models() : ServerClient.relay(message.body);
        call
          .then((text) => reply({ ok: true, text }))
          .catch((err) => reply({ ok: false, error: String((err && err.message) || err) }));
      });
    },

    _injectPageHook() {
      const parent = document.head || document.documentElement;
      if (!parent) return false;
      const script = document.createElement("script");
      script.textContent = pageHookSource();
      // An inline <script> runs synchronously on append, so the marker it sets
      // is already readable on the next line — this is the CSP check.
      parent.appendChild(script);
      script.remove();
      return document.documentElement.getAttribute(HOOK_MARKER) === "1";
    },
  };

  // ---------------------------------------------------------------------------
  // bootstrap
  // ---------------------------------------------------------------------------
  FetchHook.install();

  function boot() {
    Overlay.mount();
    Overlay.keepAlive();
    scheduleTick(0);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) scheduleTick(0);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
