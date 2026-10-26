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
