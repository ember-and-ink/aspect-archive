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

  // "Recent only" keeps the cards first seen inside this window. The same 48h
  // the archive's Discover uses (RECENT_WINDOW_MS in the frontend), so "recent"
  // means one thing on both halves.
  const RECENT_WINDOW_MS = 48 * 60 * 60 * 1000;

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

    // Stamp these card ids as seen -- an id already recorded keeps its first
    // stamp -- and read back when each was first seen, epoch ms on the
    // server's clock. What "Recent only" filters on. The `janitor` bucket of
    // the same store Discover's Following feed writes (aspect.state.seen).
    async seen(ids) {
      const { text } = await this._request({
        method: "POST",
        path: "/api/v1/discover/seen",
        body: { provider: "janitor", ids },
      });
      return JSON.parse(text).first_seen || {};
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
  // Overlay widgets — a single fixed-position container (#aspect-root) in the
  // bottom-right corner, in one of two shapes:
  //
  //   collapsed — one pill: status dot, "Aspect", and whatever is active or
  //               relevant on this page ("· 12 hidden · recent", "· ready ✓",
  //               "· 18/38" mid-bulk). Transient messages (export progress,
  //               errors) briefly replace its text; errors tint it red.
  //   expanded  — a panel of sections, each shown only where it applies:
  //                 This creator   — bulk Download all (creator profiles)
  //                 Switches       — Hide cards I have / Recent only (listings
  //                                  and profiles)
  //                 Export         — status line + Export card (character and
  //                                  chat pages)
  //                 Footer         — connection, CLEAR, collapse (every page)
  //
  // Which sections show is decided from the URL (currentPageKind), so it is
  // right the moment the page changes rather than after the next tick. The
  // values that do arrive late — counts, connection, status — sit in slots
  // with a fixed height, so nothing shifts when they land.
  //
  // The collapsed/expanded choice is a layout preference and persists
  // (GM_setValue); first install starts collapsed. Colours are the archive's
  // own palette (frontend/src/index.css), so the overlay reads as part of it.
  // All styling lives in one injected <style> instead of per-element inline
  // styles.
  // ---------------------------------------------------------------------------
  const KEY_EXPANDED = "overlay_expanded";

  const OVERLAY_STYLE = `
    #aspect-root {
      --a-surface: #16191b; --a-raised: #1d2124; --a-hi: #242a2d;
      --a-line: #272c30; --a-text: #e9ebec; --a-muted: #9aa2a6;
      --a-sage: #57c2a2; --a-on-sage: #08130f; --a-warn: #d3a44f; --a-bad: #d97a72;
      position: fixed; bottom: 12px; right: 12px; z-index: 999999;
      display: flex; flex-direction: column; align-items: flex-end;
      font-family: system-ui, sans-serif; font-size: 12px; line-height: 1.35;
      color: var(--a-text); pointer-events: none;
    }
    #aspect-root * { box-sizing: border-box; }
    #aspect-root button {
      font: inherit; color: inherit; margin: 0; letter-spacing: normal;
      text-transform: none;
    }
    #aspect-root [hidden] { display: none !important; }
    #aspect-root.jai-collapsed #aspect-panel { display: none; }
    #aspect-root:not(.jai-collapsed) #aspect-pill { display: none; }

    #aspect-pill {
      pointer-events: auto; display: flex; align-items: center; gap: 8px;
      height: 36px; padding: 0 12px; border-radius: 999px; cursor: pointer;
      border: 1px solid var(--a-line); background: var(--a-surface);
      font-weight: 600; white-space: nowrap;
      box-shadow: 0 8px 28px rgba(0,0,0,.55);
    }
    #aspect-pill .jai-sum { color: var(--a-muted); }
    #aspect-pill .jai-sum.jai-active { color: var(--a-sage); }
    #aspect-pill.jai-error { border-color: var(--a-bad); background: #2a1716; }
    #aspect-pill.jai-error .jai-sum { color: var(--a-bad); }

    .jai-dot {
      flex: none; width: 8px; height: 8px; border-radius: 50%;
      background: var(--a-muted);
    }
    .jai-dot[data-state="ok"] { background: var(--a-sage); }
    .jai-dot[data-state="warn"] { background: var(--a-warn); }
    .jai-dot[data-state="down"] { background: var(--a-bad); }

    #aspect-panel {
      pointer-events: auto; width: 264px; display: flex; flex-direction: column;
      gap: 10px; padding: 12px; border-radius: 12px;
      border: 1px solid var(--a-line); background: var(--a-surface);
      box-shadow: 0 8px 28px rgba(0,0,0,.55);
    }
    /* Every visible section is followed by another (the footer is always
       there), so a bottom rule on each separates them without caring which
       ones are hidden. */
    .jai-section {
      display: flex; flex-direction: column; gap: 8px;
      padding-bottom: 10px; border-bottom: 1px solid var(--a-line);
    }
    .jai-label {
      font-size: 11px; font-weight: 600; letter-spacing: .06em;
      text-transform: uppercase; color: var(--a-muted);
    }

    .jai-switch {
      display: flex; align-items: center; justify-content: space-between;
      gap: 10px; width: 100%; padding: 2px 0; border: none; background: none;
      cursor: pointer; text-align: left;
    }
    .jai-switch-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
    .jai-switch-title { font-weight: 600; }
    .jai-switch-sub {
      height: 15px; font-size: 11px; color: var(--a-muted);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .jai-track {
      flex: none; position: relative; width: 34px; height: 20px;
      border-radius: 999px; background: var(--a-hi); border: 1px solid var(--a-line);
    }
    .jai-track::after {
      content: ""; position: absolute; top: 1px; left: 1px; width: 16px;
      height: 16px; border-radius: 50%; background: var(--a-muted);
      transition: transform .15s;
    }
    .jai-switch[aria-checked="true"] .jai-track {
      background: var(--a-sage); border-color: var(--a-sage);
    }
    .jai-switch[aria-checked="true"] .jai-track::after {
      transform: translateX(14px); background: var(--a-on-sage);
    }

    #aspect-status {
      height: 15px; font-size: 11px; color: var(--a-muted);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    #aspect-status.jai-error { color: var(--a-bad); }
    #aspect-export {
      display: flex; align-items: center; justify-content: center; gap: 8px;
      height: 38px; border-radius: 8px; border: none; cursor: pointer;
      background: var(--a-sage); color: var(--a-on-sage) !important;
      font-size: 13px !important; font-weight: 700;
    }
    #aspect-export:disabled { cursor: default; }

    .jai-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .jai-foot-status {
      display: flex; align-items: center; gap: 6px; min-width: 0;
      color: var(--a-muted); white-space: nowrap; overflow: hidden;
    }
    .jai-foot-status span:last-child { overflow: hidden; text-overflow: ellipsis; }
    .jai-foot-status.jai-error { color: var(--a-bad); }
    .jai-foot-actions { display: flex; align-items: center; gap: 2px; flex: none; }
    .jai-clear {
      padding: 2px 6px; border: none; background: none; cursor: pointer;
      color: var(--a-bad) !important; font-size: 11px !important;
      font-weight: 600; letter-spacing: .04em !important;
    }
    .jai-clear:hover { color: #ff8f86 !important; }
    .jai-collapse {
      display: flex; padding: 2px 4px; border: none; background: none;
      cursor: pointer; color: var(--a-muted) !important;
    }

    #aspect-bulk .jai-bulk-head {
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
    }
    #aspect-bulk-btn {
      padding: 5px 10px; border-radius: 8px; border: none; cursor: pointer;
      background: var(--a-sage); color: var(--a-on-sage) !important; font-weight: 700;
    }
    #aspect-bulk-btn:disabled { opacity: 0.6; cursor: default; }
    #aspect-bulk .jai-bulk-status { color: var(--a-muted); word-break: break-word; }
    #aspect-bulk .jai-bulk-list {
      display: flex; flex-direction: column; gap: 2px;
      max-height: 220px; overflow-y: auto;
    }
    #aspect-bulk .jai-bulk-list:empty { display: none; }
    #aspect-bulk .jai-bulk-row { display: flex; align-items: baseline; gap: 6px; }
    #aspect-bulk .jai-bulk-icon { flex: 0 0 1.2em; text-align: center; }
    #aspect-bulk .jai-bulk-name {
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }

    /* Class-only, reversible hide of tiles (TileFilters): already saved
       (jai-captured) or skipped by the tag filter (jai-filtered) while Hide is
       on, first seen outside the window (jai-stale) while Recent only is on.
       The marker classes land on the card wrapper; the <html> flags flip them
       all at once so JAI's own inline styles are never disturbed. */
    html.jai-hide-captured .profile-character-card-wrapper.jai-captured,
    html.jai-hide-captured .profile-character-card-wrapper.jai-filtered,
    html.jai-recent-only .profile-character-card-wrapper.jai-stale {
      display: none !important;
    }
  `;

  // A small stroke icon. Built with createElementNS rather than an innerHTML
  // string, so it can never trip a Trusted Types policy on the page.
  function strokeIcon(size, paths) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("width", String(size));
    svg.setAttribute("height", String(size));
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2.4");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    for (const d of paths) {
      const path = document.createElementNS(NS, "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    return svg;
  }

  // What the export status line says when no message is showing, keyed by the
  // scheduler's card-state word. Absent (not yet resolved): blank.
  const CARD_STATE_LINE = {
    "ready ✓": "Open definition, ready to export",
    "hidden ✓": "Hidden definition captured, ready to export",
    "hidden ✗": "Hidden: send one message to capture it",
  };

  const ICON_DOWNLOAD = ["M12 4v11", "m7 10 5 5 5-5", "M5 20h14"];
  const ICON_CHEVRON_UP = ["m6 15 6-6 6 6"];
  const ICON_CHEVRON_DOWN = ["m6 9 6 6 6-6"];

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // ---------------------------------------------------------------------------
  // ExportButton — the one Export card action. Readiness isn't colour-coded on
  // the button — the connection line spells it out (ready ✓ / hidden ✓ /
  // hidden ✗) — so the scheduler only dims it while the card in view can't be
  // exported yet.
  // ---------------------------------------------------------------------------
  const ExportButton = {
    _el: null,

    setReady(ready) {
      if (!this._el || this._el.disabled) return;
      this._el.style.opacity = ready ? "1" : "0.55";
    },

    onClick() {
      return exportCard();
    },
  };

  // ---------------------------------------------------------------------------
  // ExportStatus — export progress ("Exporting…", "✓ Saved", errors). Shown in
  // the status line above the button while the panel is open, and on the pill
  // while it is collapsed; held until hide().
  // ---------------------------------------------------------------------------
  const ExportStatus = {
    show(text, isError) {
      Overlay.showTransient(text, isError);
    },

    hide() {
      Overlay.clearTransient();
    },
  };

  // CLEAR wipes the server-side capture cache AND resets the plugin's
  // remembered card state (last id / hidden flag). Exported PNGs are untouched.
  async function clearCaptures() {
    if (
      !window.confirm(
        "Clear Aspect Archive capture cache (captured hidden definitions + greetings) " +
          "and reset the plugin's remembered card state? Exported PNGs are not affected."
      )
    ) {
      return;
    }
    resetPluginState();
    Overlay.showTransient("Clearing…", false, 4000);
    try {
      const result = await ServerClient.clearCaptures();
      log("cleared captures ->", result.removed, "+ plugin state");
      Overlay.showTransient(`✓ Cleared ${result.removed}`, false, 3000);
    } catch (err) {
      warn("clear failed", err);
      Overlay.showTransient("⚠️ Clear failed", true, 6000);
    }
  }

  // ---------------------------------------------------------------------------
  // Overlay — builds and keeps alive the single container, and renders it from
  // state owned elsewhere: the connection (set by the scheduler's tick), the
  // switches (TileFilters.describe()), the bulk run (BulkPanel.progress) and the
  // page kind (the URL). render() is cheap and idempotent; anything that
  // changes one of those calls it.
  //
  // The watchdog observes only <html>'s direct children (childList, NO
  // subtree), so it does not fire on every streamed chat token; it wakes only
  // if our root detaches.
  // ---------------------------------------------------------------------------
  const Overlay = {
    _root: null,
    _els: {},
    _expanded: false,
    _connection: { state: "connecting", detail: "" }, // state: connecting | ok | down
    _transient: null, // { text, isError } — overrides the status text while set
    _transientTimer: null,

    mount() {
      if (this._root && this._root.isConnected) return;

      if (!document.getElementById("aspect-style")) {
        const style = document.createElement("style");
        style.id = "aspect-style";
        style.textContent = OVERLAY_STYLE;
        (document.head || document.documentElement).appendChild(style);
      }

      this._expanded = !!GM_getValue(KEY_EXPANDED, false);
      const e = {};
      const root = el("div");
      root.id = "aspect-root";
      e.root = root;

      // -- collapsed pill
      const pill = el("button");
      pill.id = "aspect-pill";
      pill.type = "button";
      pill.title = "Show the Aspect panel";
      e.pillDot = el("span", "jai-dot");
      e.pillText = el("span", "", "Aspect");
      e.pillSum = el("span", "jai-sum");
      const pillChevron = el("span");
      pillChevron.style.display = "flex";
      pillChevron.style.color = "var(--a-muted)";
      pillChevron.append(strokeIcon(12, ICON_CHEVRON_UP));
      pill.append(e.pillDot, e.pillText, e.pillSum, pillChevron);
      pill.addEventListener("click", () => this.setExpanded(true));
      e.pill = pill;

      // -- expanded panel
      const panel = el("div");
      panel.id = "aspect-panel";

      e.creator = el("div", "jai-section");
      e.creator.append(BulkPanel.build());

      e.filters = el("div", "jai-section");
      e.hideSwitch = this._switch(
        "Hide cards I have",
        "Hide cards already saved to the archive, plus cards your include/exclude " +
          "tag filter would skip",
        () => TileFilters.toggle("hide")
      );
      e.recentSwitch = this._switch(
        "Recent only",
        "Show only cards that first appeared on your screen in the last 48 hours",
        () => TileFilters.toggle("recent")
      );
      e.filters.append(e.hideSwitch.row, e.recentSwitch.row);

      e.export = el("div", "jai-section");
      e.status = el("div");
      e.status.id = "aspect-status";
      const btn = el("button");
      btn.id = "aspect-export";
      btn.type = "button";
      btn.append(strokeIcon(16, ICON_DOWNLOAD), document.createTextNode("Export card"));
      btn.addEventListener("click", () => ExportButton.onClick());
      e.export.append(e.status, btn);

      const foot = el("div", "jai-foot");
      e.footStatus = el("span", "jai-foot-status");
      e.footDot = el("span", "jai-dot");
      e.footText = el("span", "", "Aspect");
      e.footStatus.append(e.footDot, e.footText);
      const actions = el("span", "jai-foot-actions");
      const clear = el("button", "jai-clear", "CLEAR");
      clear.type = "button";
      clear.title = "Clear server capture cache + reset remembered card state";
      clear.addEventListener("click", () => clearCaptures());
      const collapse = el("button", "jai-collapse");
      collapse.type = "button";
      collapse.title = "Collapse";
      collapse.setAttribute("aria-label", "Collapse the Aspect panel");
      collapse.append(strokeIcon(12, ICON_CHEVRON_DOWN));
      collapse.addEventListener("click", () => this.setExpanded(false));
      actions.append(clear, collapse);
      foot.append(e.footStatus, actions);

      panel.append(e.creator, e.filters, e.export, foot);
      root.append(panel, pill);
      document.documentElement.appendChild(root);

      ExportButton._el = btn;
      this._els = e;
      this._root = root;
      this.render();
    },

    // One switch row: title, a fixed-height subtext slot, and the track.
    _switch(title, tooltip, onClick) {
      const row = el("button", "jai-switch");
      row.type = "button";
      row.setAttribute("role", "switch");
      row.setAttribute("aria-checked", "false");
      row.title = tooltip;
      const text = el("span", "jai-switch-text");
      const sub = el("span", "jai-switch-sub");
      text.append(el("span", "jai-switch-title", title), sub);
      row.append(text, el("span", "jai-track"));
      row.addEventListener("click", onClick);
      return { row, sub };
    },

    setExpanded(expanded) {
      this._expanded = expanded;
      GM_setValue(KEY_EXPANDED, expanded);
      this.render();
    },

    // From the scheduler's tick. `detail` is the card-state word for a
    // character/chat page ("ready ✓", "hidden ✓", "hidden ✗"), else "".
    setConnection(state, detail) {
      this._connection = { state, detail: detail || "" };
      this.render();
    },

    // A message that overrides the status text: until clearTransient(), or for
    // `ms` when given.
    showTransient(text, isError, ms) {
      clearTimeout(this._transientTimer);
      this._transient = { text, isError: !!isError };
      if (ms) this._transientTimer = setTimeout(() => this.clearTransient(), ms);
      this.render();
    },

    clearTransient() {
      clearTimeout(this._transientTimer);
      this._transient = null;
      this.render();
    },

    render() {
      if (!this._root) return;
      const e = this._els;
      const kind = currentPageKind();
      const onCard = kind === "character" || kind === "chat";
      const listing = kind === "listing" || kind === "profile";
      const conn = this._connection;
      const transient = this._transient;

      e.root.classList.toggle("jai-collapsed", !this._expanded);
      e.creator.hidden = kind !== "profile";
      e.filters.hidden = !listing;
      e.export.hidden = !onCard;

      // Connection: dot + words, shared by the pill and the footer.
      let dot = "connecting";
      let words = "connecting";
      if (conn.state === "down") {
        dot = "down";
        words = "server down";
      } else if (conn.state === "ok") {
        dot = conn.detail === "hidden ✗" ? "warn" : "ok";
        words = onCard ? conn.detail : "";
      }
      e.pillDot.dataset.state = dot;
      e.footDot.dataset.state = dot;

      // Switches.
      const filters = TileFilters.describe();
      e.hideSwitch.row.setAttribute("aria-checked", String(TileFilters.hide));
      e.hideSwitch.sub.textContent = filters.hideSub;
      e.recentSwitch.row.setAttribute("aria-checked", String(TileFilters.recent));
      e.recentSwitch.sub.textContent = filters.recentSub;

      // A transient message goes to the export status line when that section
      // is showing, else the footer — and to the pill either way.
      const inStatusLine = transient && onCard;
      e.status.textContent = inStatusLine
        ? transient.text
        : (conn.state === "ok" && CARD_STATE_LINE[conn.detail]) || "";
      e.status.classList.toggle("jai-error", !!(inStatusLine && transient.isError));
      const footTransient = transient && !onCard;
      e.footText.textContent = footTransient
        ? transient.text
        : words
          ? `Aspect · ${words}`
          : "Aspect";
      e.footStatus.classList.toggle("jai-error", !!(footTransient && transient.isError));

      // Collapsed pill.
      const parts = [];
      if (words) parts.push(words);
      if (listing && conn.state !== "down") parts.push(...filters.summary);
      if (BulkPanel.progress) parts.push(BulkPanel.progress);
      if (transient) {
        e.pillText.textContent = transient.text;
        e.pillSum.textContent = "";
      } else {
        e.pillText.textContent = "Aspect";
        e.pillSum.textContent = parts.map((p) => `· ${p}`).join(" ");
      }
      e.pillSum.classList.toggle("jai-active", listing && filters.summary.length > 0);
      e.pill.classList.toggle("jai-error", !!(transient && transient.isError));
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
  // BulkPanel — the overlay's "This creator" section (creator profiles only; the
  // overlay decides when it shows): a Download all button, a status line, and
  // a scrollable list of rows whose leading icon tracks each card's fate
  // (⏳ pending/working · ✓ saved · ✗ hidden-skipped · ⚠ failed). While a run
  // is going, `progress` ("18/38") is what the collapsed pill shows.
  // ---------------------------------------------------------------------------
  const BulkPanel = {
    _el: null,
    _btn: null,
    _status: null,
    _list: null,
    _rows: new Map(),
    _running: false,
    progress: "",

    build() {
      const el = document.createElement("div");
      el.id = "aspect-bulk";
      el.style.display = "flex";
      el.style.flexDirection = "column";
      el.style.gap = "6px";

      const head = document.createElement("div");
      head.className = "jai-bulk-head";
      const title = document.createElement("span");
      title.className = "jai-label";
      title.textContent = "This creator";
      const btn = document.createElement("button");
      btn.id = "aspect-bulk-btn";
      btn.type = "button";
      btn.textContent = "Download all";
      btn.title = "Export every open card this creator has published";
      btn.addEventListener("click", () => this.run());
      head.append(title, btn);

      const status = document.createElement("div");
      status.className = "jai-bulk-status";
      const fs = filterSummary();
      status.textContent = fs ? `Open cards only · filter: ${fs}` : "Open cards only";

      const list = document.createElement("div");
      list.className = "jai-bulk-list";

      el.append(head, status, list);
      this._el = el;
      this._btn = btn;
      this._status = status;
      this._list = list;
      return el;
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
          this.progress = `${i + 1}/${pending.length}`;
          Overlay.render();
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
        this.progress = "";
        Overlay.render();
      }
    },
  };

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
  // Dispatched by the page hook whenever the SPA's URL changes, so the sandbox
  // can re-apply the overlay and tile filters now rather than on the next tick.
  const NAVIGATE_EVENT = "aspect:navigate";
  // Set on <html> by the injected script itself, and read back synchronously —
  // an inline <script> executes on append, so if the attribute is missing the
  // injection was blocked and we know it rather than guessing later.
  const HOOK_MARKER = "data-aspect-hook";

  // ---------------------------------------------------------------------------
  // pageWorldHook — runs in the PAGE realm, not here.
  //
  // Stringified into the injected <script>, so it may only close over names the
  // injected preamble also defines: SERVER, looksLikeChatCompletion,
  // looksLikeModelsProbe and the four constants above. No GM_*, no
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

    // SPA navigation. JanitorAI's router moves between pages with
    // history.pushState, which no event announces, and the sandbox can't patch
    // it for the same reason it can't patch fetch: its `history` isn't the one
    // the router calls. replaceState fires constantly for scroll restoration
    // with the URL unchanged, so only a real URL change is announced.
    const history = window.history;
    if (history && typeof history.pushState === "function") {
      let lastHref = window.location.href;
      const announce = () => {
        if (window.location.href === lastHref) return;
        lastHref = window.location.href;
        document.dispatchEvent(new CustomEvent(NAVIGATE_EVENT));
      };
      for (const name of ["pushState", "replaceState"]) {
        const original = history[name];
        history[name] = function () {
          const result = original.apply(this, arguments);
          announce();
          return result;
        };
      }
      window.addEventListener("popstate", announce);
    }

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
      "  const NAVIGATE_EVENT = " + JSON.stringify(NAVIGATE_EVENT) + ";",
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
    document.addEventListener(NAVIGATE_EVENT, onNavigate);
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
