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
