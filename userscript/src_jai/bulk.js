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
