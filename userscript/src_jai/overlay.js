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
