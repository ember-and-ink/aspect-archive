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
