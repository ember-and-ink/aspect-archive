// ==UserScript==
// @name         saucepan-proxy bridge
// @namespace    https://github.com/mjnitz02/jai-proxy
// @version      0.9.1
// @description  Thin bridge: exports a Saucepan companion as a V3 card PNG via Saucepan's clean JSON API (no DOM scraping) and shows a local Aspect Archive connection pill. Card assembly lives server-side.
// @match        https://saucepan.ai/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      saucepan.ai
// @connect      127.0.0.1
// @connect      localhost
// @connect      *
// ==/UserScript==
//
// `@connect *` is there because the server no longer has to be on this machine
// (see docs/DEPLOY.md): its URL is read from Tampermonkey storage at runtime,
// so the host it points at cannot be declared here at compile time, and without
// a matching @connect Tampermonkey blocks the request outright. The loopback
// entries above stay for the default case.
//
// SOURCE LAYOUT — this file is COMPILED. Do not edit saucepan-proxy-bridge.user.js
// by hand; edit userscript/src_saucepan/*.js and run `make compile` (see
// scripts/compile_userscript_saucepan.py). The modules are concatenated, in
// order, inside a single IIFE beneath this banner.

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Config + logging. The saucepan bridge is the twin of the JanitorAI bridge:
  // it fetches a companion straight from saucepan's own JSON API and hands the
  // raw export to the local Aspect Archive server (POST /build-saucepan), which does
  // all the deobfuscation / mapping / PNG assembly. (That whole pipeline used to
  // be a 1200-line in-page DOM scraper; it now lives server-side in
  // aspect/saucepan_fragments.py + saucepan_mapper.py + cardbuilder.py.)
  // ---------------------------------------------------------------------------
  // Where the Aspect Archive server is. Persisted in Tampermonkey storage rather
  // than compiled in, because it may now be a container on another machine
  // (docs/DEPLOY.md). To repoint the bridge, run this once in the console on
  // saucepan.ai — no recompile, no reinstall:
  //
  //   GM_setValue("serverUrl", "http://192.168.1.50:8000")
  //
  // and reload. Clearing it falls back to the local default below. Plain http
  // to a LAN address works from HTTPS saucepan: every call goes out through
  // GM_xmlhttpRequest (see client-server.js), which is exempt from the page's
  // mixed-content and CSP rules.
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
  const SAUCEPAN_ORIGIN = "https://saucepan.ai";

  const TAG = "[saucepan-export]";
  const log = (...a) => console.log(TAG, ...a);
  const warn = (...a) => console.warn(TAG, ...a);

  // ---------------------------------------------------------------------------
  // ServerClient — all traffic to the local Aspect Archive server goes via
  // GM_xmlhttpRequest so it's exempt from the page's CSP / mixed-content rules.
  // `/health` drives the connection pill; `/build-saucepan` turns a raw export
  // into a saved PNG card server-side.
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
          onerror: () => reject(new Error("cannot reach Aspect Archive server at " + SERVER)),
          ontimeout: () => reject(new Error("server timeout")),
        });
      });
    },

    async health() {
      const { text } = await this._request({ path: "/health" });
      return JSON.parse(text);
    },

    async build(payload) {
      const { text } = await this._request({
        method: "POST",
        path: "/build-saucepan",
        body: payload,
        timeout: 60000,
      });
      return JSON.parse(text);
    },

    // Ask which of these lorebook ids the server already has cached. Returns
    // {cached, missing}: skip fetching `cached` (they ride into the build by id),
    // fetch only `missing` from saucepan.
    async lorebooksExisting(source, ids) {
      const { text } = await this._request({
        method: "POST",
        path: "/lorebooks/existing",
        body: { source, ids },
      });
      return JSON.parse(text);
    },

    // Wipe the server's lorebook cache (the CLEAR affordance). Exported PNGs are
    // untouched -- this only drops the fetch-skipping cache so the next export
    // re-pulls fresh lorebooks.
    async clearLorebooks() {
      const { text } = await this._request({ method: "POST", path: "/clear-lorebooks" });
      return JSON.parse(text);
    },
  };

  // ---------------------------------------------------------------------------
  // SaucepanClient — reads a companion (and its lorebooks) straight from
  // saucepan's own JSON API, the way the page's own client does.
  //
  //   GET /api/v1/companion/definition?companion_id=<id>  -> {sections, card}
  //   GET /api/v2/companions/<id>                          -> {companion, ...}
  //   GET /api/v2/lorebooks/<lid>/chapters                 -> {chapters:[{index,title}]}
  //   GET /api/v2/lorebooks/<lid>/chapters/<index>         -> {title, text_fragments}
  // Lorebook ids aren't in the companion payload, so they're read from the
  // profile's own /lorebook/<id> links (same source the page uses).
  //
  // Macros ({{user}}/{{char}}) come back intact from these endpoints -- the
  // account-handle substitution only happens in the chat stream, which we never
  // touch -- so there is no {{user}} restoration step here.
  // ---------------------------------------------------------------------------

  // saucepan API auth. Every /api call needs a bearer JWT plus a constant
  // Cloudflare edge-gate header; without both the API returns 404 (not 401).
  // The JWT lives in localStorage under "hallucination/auth_token" (the sandbox
  // shares the page's localStorage). If a future build rotates the edge header,
  // requests 404 again and both name/value can be re-read from any authed /api
  // call in DevTools → Network.
  const SAUCEPAN_EDGE_HEADER = "cf-edge-token-8af3";
  const SAUCEPAN_EDGE_TOKEN = "e3b0c44298fc1c14";

  function authHeaders() {
    let token = localStorage.getItem("hallucination/auth_token");
    if (token && token[0] === '"') {
      try {
        token = JSON.parse(token);
      } catch (e) {
        /* leave as-is */
      }
    }
    const headers = { Accept: "application/json" };
    headers[SAUCEPAN_EDGE_HEADER] = SAUCEPAN_EDGE_TOKEN;
    if (token) headers.Authorization = "Bearer " + token;
    return headers;
  }

  // GET a saucepan API path (absolute URL required by GM_xmlhttpRequest).
  function apiGet(path) {
    const url = SAUCEPAN_ORIGIN + path;
    const headers = authHeaders();
    log("GET", path, "(auth:", headers.Authorization ? "bearer" : "NONE", ")");
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "GET",
        url,
        headers,
        onload: (r) => {
          if (r.status >= 200 && r.status < 300) {
            try {
              resolve(JSON.parse(r.responseText));
            } catch (e) {
              reject(new Error("bad JSON from " + path));
            }
          } else {
            // 404 here usually means the auth/edge-token headers were rejected.
            warn("HTTP", r.status, "for", path, "— check bearer token + cf-edge-token");
            reject(new Error("HTTP " + r.status + " for " + path));
          }
        },
        onerror: () => reject(new Error("network error for " + path)),
      });
    });
  }

  const SaucepanClient = {
    // The companion id from a /companion/<id> path.
    companionIdFromUrl() {
      const m = location.pathname.match(/\/companion\/([0-9a-f-]{8,})/i);
      return m ? m[1] : null;
    },

    // Lorebook ids from the profile's own /lorebook/<id> links (the companion
    // payload doesn't carry them).
    lorebookIds() {
      const ids = new Set();
      for (const a of document.querySelectorAll('a[href*="/lorebook/"]')) {
        const m = (a.href || "").match(/\/lorebook\/([0-9a-f-]{8,})/i);
        if (m) ids.add(m[1]);
      }
      return [...ids];
    },

    // The `/api/v2/companions/<id>` response: {companion:{...}, is_favorited, …}.
    async fetchCompanion(id) {
      return apiGet(`/api/v2/companions/${id}`);
    },

    // Whether a companion's definition is HIDDEN. `open_definition` (on the
    // inner companion object) is saucepan's own flag — the same one the "Definition:
    // Hidden" badge renders from, and the one the server's is_open() reads. A
    // hidden card's /api/v1/companion/definition returns a decoy error instead
    // of real sections, and this bridge has no chat-capture path, so a hidden
    // companion can only export its public blurb (server warns "not open").
    isHidden(v2) {
      const inner = v2 && v2.companion;
      return !!inner && inner.open_definition === false;
    },

    // The raw export for a companion id: {id, definition, companion, lorebooks,
    // cached_lorebook_ids}. Exactly the shape POST /build-saucepan expects.
    //
    // opts.lorebookIds — the lorebook ids to actually fetch (the cache MISSES;
    //   defaults to every lorebook on the page when omitted). Fetching a
    //   lorebook is the slow part of an export — one request per chapter — so
    //   skipping the ones the server already has is the whole speed-up.
    // opts.cachedLorebookIds — ids the server already has; stamped onto the
    //   export as `cached_lorebook_ids` so the build pulls them from cache.
    // opts.onProgress(p) — called as fetching advances so the overlay can show a
    //   live count. Fires with one of:
    //     {phase:"definition"} | {phase:"companion"} | {phase:"lore", done, total}
    // Chapters are still fetched one at a time (a steady serial trickle, the
    // same request pattern the page itself makes) rather than in a burst, to
    // stay well under any saucepan rate limit.
    async fetchExport(id, opts = {}) {
      const report = typeof opts.onProgress === "function" ? opts.onProgress : () => {};
      const fetchIds = opts.lorebookIds || this.lorebookIds();
      const cachedIds = opts.cachedLorebookIds || [];
      const out = { id, cached_lorebook_ids: cachedIds };

      report({ phase: "definition" });
      out.definition = await apiGet(`/api/v1/companion/definition?companion_id=${id}`);

      report({ phase: "companion" });
      out.companion = await this.fetchCompanion(id);

      // Read each MISSING lorebook's chapter LIST first (one cheap call each).
      // These carry the chapter counts, so summing them gives a real denominator
      // for the progress count before any chapter body is fetched.
      const books = [];
      let total = 0;
      for (const lid of fetchIds) {
        const list = await apiGet(`/api/v2/lorebooks/${lid}/chapters`);
        const chapters = (list && list.chapters) || [];
        books.push({ id: lid, list, chapters });
        total += chapters.length;
      }

      // Now fetch the chapter bodies, reporting "done / total" as each lands.
      out.lorebooks = [];
      let done = 0;
      for (const book of books) {
        const chapters = [];
        for (const c of book.chapters) {
          chapters.push(await apiGet(`/api/v2/lorebooks/${book.id}/chapters/${c.index}`));
          done += 1;
          report({ phase: "lore", done, total });
        }
        out.lorebooks.push({ id: book.id, list: book.list, chapters });
      }
      log(
        `fetched export for ${id} — lorebooks fetched: ${out.lorebooks.length}, ` +
          `from cache: ${cachedIds.length}`
      );
      return out;
    },
  };

  // ---------------------------------------------------------------------------
  // Export — fetch the companion's raw JSON export and hand it to the server
  // (POST /build-saucepan), which does the deobfuscation / mapping / PNG build.
  // The server derives the real character name from the companion JSON
  // (companion.name), so there's no name prompt. Progress lands in the status
  // line above the button (ExportStatus), mirroring the JanitorAI bridge.
  // ---------------------------------------------------------------------------
  async function exportCompanion() {
    const btn = ExportButton._el;
    btn.disabled = true;
    ExportStatus.show("Fetching…");
    let holdMs = 2500;
    try {
      const id = SaucepanClient.companionIdFromUrl();
      if (!id) {
        ExportStatus.show("⚠️ Open a /companion/<id> page first", true);
        holdMs = 5000;
        return;
      }

      // Ask the server which of this companion's lorebooks it already has cached,
      // so we fetch only the misses (a lorebook is one saucepan request PER
      // chapter — the slow part — and creators reuse "standard" lorebooks across
      // many characters). A failed/unreachable check just fetches everything.
      const allIds = SaucepanClient.lorebookIds();
      let cached = [];
      let missing = allIds;
      if (allIds.length) {
        try {
          const split = await ServerClient.lorebooksExisting("saucepan", allIds);
          cached = split.cached || [];
          missing = split.missing || allIds;
        } catch (err) {
          warn("lorebook cache check failed; fetching all", err);
        }
      }

      const export_ = await SaucepanClient.fetchExport(id, {
        lorebookIds: missing,
        cachedLorebookIds: cached,
        onProgress: (p) => {
          if (p.phase === "lore") {
            ExportStatus.show(`Fetching lore ${p.done} / ${p.total}…`);
          } else if (p.phase === "companion") {
            const note = cached.length ? ` (${cached.length} cached)` : "";
            ExportStatus.show(`Fetching companion${note}…`);
          } else {
            ExportStatus.show("Fetching…");
          }
        },
      });

      ExportStatus.show("Building on server…");
      const res = await ServerClient.build({ character: export_ });
      const warnings = res.warnings || [];
      if (res.ok && res.duplicate) {
        // Already on disk -- the server wrote nothing. Say so rather than "✓
        // Saved", which would claim an export that didn't happen.
        log("already saved:", res.path);
        ExportStatus.show("• Already saved");
        holdMs = 4000;
      } else if (res.ok) {
        log("built:", res.path, "warnings:", warnings);
        if (warnings.length) {
          const n = warnings.length;
          ExportStatus.show(`⚠️ Saved — ${n} warning${n === 1 ? "" : "s"}`, false);
          holdMs = 8000;
        } else {
          ExportStatus.show("✓ Saved");
        }
      } else {
        ExportStatus.show(`⚠️ ${warnings[0] || "build failed"}`, true);
        holdMs = 8000;
        warn("build failed:", warnings);
      }
    } catch (err) {
      ExportStatus.show("⚠️ " + (err && err.message ? err.message : String(err)), true);
      holdMs = 8000;
      warn(err);
    } finally {
      setTimeout(() => {
        ExportStatus.hide();
        btn.disabled = false;
      }, holdMs);
    }
  }

  // ---------------------------------------------------------------------------
  // Overlay widgets — a single fixed-position container (#saucepan-export-root)
  // holds, top to bottom: a transient status line, the purple Export button,
  // and the connection pill (with an inline CLEAR affordance). Mirrors the
  // JanitorAI bridge. Hidden companions ARE detected (pill shows `hidden ✗`) but
  // still can't export their definition. CLEAR wipes the server's lorebook cache
  // (the fetch-skipping speed cache), not any captured definition — saucepan has
  // no chat-capture path. Exported PNGs are never affected.
  // ---------------------------------------------------------------------------
  const OVERLAY_STYLE = `
    #saucepan-export-root {
      position: fixed; bottom: 16px; right: 16px; z-index: 999999;
      display: flex; flex-direction: column; align-items: flex-end; gap: 8px;
      font-family: system-ui, sans-serif; font-size: 12px; pointer-events: none;
    }
    #saucepan-export-status {
      pointer-events: auto; display: none; max-width: 260px;
      padding: 3px 8px; border-radius: 8px; color: #fff;
      background: rgba(0,0,0,.6); word-break: break-word;
    }
    #saucepan-export-btn {
      pointer-events: auto; padding: 10px 14px; border-radius: 10px; border: none;
      background: #7a3db5; color: #fff; font-family: inherit; font-size: 13px;
      font-weight: 600; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,.3);
    }
    #saucepan-export-btn:disabled { cursor: default; }
    #saucepan-export-pill {
      display: flex; align-items: center; gap: 8px; padding: 4px 10px;
      border-radius: 999px; background: #222; color: #fff; border: 1px solid #444;
      opacity: 0.9;
    }
    #saucepan-export-pill .saucepan-clear {
      pointer-events: auto; cursor: pointer; color: #ff9d9d; font-weight: bold;
      letter-spacing: 0.5px; border-left: 1px solid #555; padding-left: 8px;
    }
    #saucepan-export-pill .saucepan-clear:hover { color: #ff6b6b; }
  `;

  // ---------------------------------------------------------------------------
  // ExportButton — one always-purple action button. Readiness (on a companion
  // page vs not) is shown by the pill; the scheduler only dims the button
  // slightly when there's no companion in view to export.
  // ---------------------------------------------------------------------------
  const ExportButton = {
    _el: null,

    setReady(ready) {
      if (!this._el || this._el.disabled) return;
      this._el.style.opacity = ready ? "1" : "0.6";
    },

    onClick() {
      return exportCompanion();
    },
  };

  // ---------------------------------------------------------------------------
  // ExportStatus — the transient line just above the button ("Fetching…",
  // "Building on server…", "✓ Saved", "⚠️ …").
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
  // server's lorebook cache so the next export re-fetches lorebooks fresh;
  // exported PNGs are untouched. flash() shows transient feedback that the
  // scheduler's setStatus() won't overwrite until the hold expires.
  // ---------------------------------------------------------------------------
  const Pill = {
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
          "Clear the Aspect Archive lorebook cache? The next export will re-fetch " +
            "each lorebook fresh from saucepan. Exported PNGs are not affected."
        )
      ) {
        return;
      }
      this.flash("⏳ clearing…", 4000);
      try {
        const result = await ServerClient.clearLorebooks();
        log("cleared lorebook cache ->", result.removed);
        this.flash(`✅ cleared ${result.removed}`, 3000);
      } catch (err) {
        warn("clear failed", err);
        this.flash("⚠️ clear failed", 6000);
      }
    },
  };

  // ---------------------------------------------------------------------------
  // Overlay — builds and keeps alive the single container. The watchdog re-adds
  // the overlay if the SPA detaches it.
  // ---------------------------------------------------------------------------
  const Overlay = {
    _root: null,

    mount() {
      if (this._root && this._root.isConnected) return;
      if (!document.body) return;

      if (!document.getElementById("saucepan-export-style")) {
        const style = document.createElement("style");
        style.id = "saucepan-export-style";
        style.textContent = OVERLAY_STYLE;
        (document.head || document.documentElement).appendChild(style);
      }

      const root = document.createElement("div");
      root.id = "saucepan-export-root";

      const status = document.createElement("div");
      status.id = "saucepan-export-status";

      const btn = document.createElement("button");
      btn.id = "saucepan-export-btn";
      btn.textContent = "⬇ Export to card";
      btn.addEventListener("click", () => ExportButton.onClick());

      const pill = document.createElement("div");
      pill.id = "saucepan-export-pill";
      const pillStatus = document.createElement("span");
      pillStatus.textContent = "⚪ saucepan";
      const clear = document.createElement("span");
      clear.className = "saucepan-clear";
      clear.textContent = "CLEAR";
      clear.title = "Clear the server's lorebook cache (next export re-fetches fresh)";
      clear.addEventListener("click", () => Pill.clear());
      pill.append(pillStatus, clear);

      root.append(status, btn, pill);
      document.body.appendChild(root);

      ExportButton._el = btn;
      ExportStatus._el = status;
      Pill._statusEl = pillStatus;
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
      }).observe(document.documentElement, { childList: true, subtree: true });
    },
  };

  // ---------------------------------------------------------------------------
  // Companion state — the companion's open/hidden status, read from saucepan's
  // JSON (`open_definition`), cached per id. Mirrors the JanitorAI bridge's
  // CharacterState: one fetch per companion page, reused every tick.
  // ---------------------------------------------------------------------------
  const CompanionState = {
    _id: null,
    _payload: null,
    _fetching: false,

    // Ensure the `/api/v2/companions/<id>` payload for `id` is cached, fetching
    // it once if needed. Returns the payload, or null while a fetch is in flight
    // / on error.
    async ensure(id) {
      if (!id) return null;
      if (this._id === id && this._payload) return this._payload;
      if (this._fetching) return null;
      this._fetching = true;
      try {
        const payload = await SaucepanClient.fetchCompanion(id);
        this._id = id;
        this._payload = payload;
        return payload;
      } catch (err) {
        warn("companion fetch failed", err);
        return null;
      } finally {
        this._fetching = false;
      }
    },
  };

  // ---------------------------------------------------------------------------
  // Scheduler — one poll drives the connection pill + button dim. Paused while
  // the tab is hidden; backs off to 15s while the server is unreachable. A
  // single self-scheduling timer runs at a time; returning to the tab wakes it.
  //
  // Pill vocabulary (shared with the JanitorAI bridge):
  //   🟢 saucepan               — connected, no companion in view
  //   🟢 saucepan · ready ✓     — open companion, exportable right now
  //   🟢 saucepan · hidden ✗    — hidden companion; only the public blurb can
  //                               export (the definition API returns a decoy
  //                               error and there's no capture path — unlike a
  //                               JanitorAI hidden card, this never becomes ✓)
  //   🔴 saucepan (server down) — server unreachable
  // ---------------------------------------------------------------------------
  let serverDown = false;
  let pollTimer = null;

  async function tick() {
    const id = SaucepanClient.companionIdFromUrl();
    try {
      await ServerClient.health();
      serverDown = false;
      if (!id) {
        Pill.setStatus("🟢 saucepan");
        ExportButton.setReady(false);
      } else {
        // Classify open vs hidden from the companion JSON (cached per id). A
        // null payload (fetch failed / in flight) stays unclassified rather
        // than falsely claiming the card is exportable.
        const payload = await CompanionState.ensure(id);
        if (!payload) {
          Pill.setStatus("🟢 saucepan");
          ExportButton.setReady(false);
        } else if (SaucepanClient.isHidden(payload)) {
          Pill.setStatus("🟢 saucepan · hidden ✗");
          ExportButton.setReady(false);
        } else {
          Pill.setStatus("🟢 saucepan · ready ✓");
          ExportButton.setReady(true);
        }
      }
    } catch {
      serverDown = true;
      Pill.setStatus("🔴 saucepan (server down)");
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
  // bootstrap
  // ---------------------------------------------------------------------------
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
