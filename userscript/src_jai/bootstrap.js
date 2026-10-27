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
