"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { loadModules } = require("./helpers/load-src");

// bootstrap.js's FetchHook.install() runs at load time and calls
// client-server.js's log(), and boot() reaches for Overlay/scheduleTick from
// other modules we don't need here -- so pull in client-server.js (for
// log/warn) and keep readyState "loading" so boot() itself is deferred to a
// DOMContentLoaded listener that's never fired.
//
// The document stub has to be good enough for install(): it listens for the
// bridge event and injects an inline <script>, checking afterwards that the
// script set its marker attribute. appendChild sets that attribute here, which
// is what a browser gets by actually running the script.
//
// SERVER is seeded rather than loaded: it lives in config.js now (persisted,
// since the server can be remote). Seeding it is what keeps the probe
// assertions below about the *matching rule* rather than about config content.
function load(server = "http://127.0.0.1:8000") {
  function XHRStub() {}
  const documentElement = {
    attributes: {},
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
    getAttribute(name) {
      return name in this.attributes ? this.attributes[name] : null;
    },
  };
  const document = {
    readyState: "loading",
    documentElement,
    addEventListener() {},
    createElement: () => ({ textContent: "", remove() {} }),
    // Stands in for the browser running the injected script on append.
    appendChild: () => documentElement.setAttribute("data-aspect-hook", "1"),
  };
  document.head = document;
  return loadModules(
    ["client-server.js", "bootstrap.js"],
    [
      "looksLikeChatCompletion",
      "looksLikeModelsProbe",
      "pageHookSource",
      "BRIDGE_REQUEST",
      "BRIDGE_RESPONSE",
    ],
    {
      document,
      window: { XMLHttpRequest: XHRStub },
      SERVER: server,
      GM_xmlhttpRequest() {},
    }
  );
}

test("looksLikeChatCompletion matches on URL alone", () => {
  const { looksLikeChatCompletion } = load();
  assert.equal(looksLikeChatCompletion("https://janitorai.com/chat/completions", null), true);
  assert.equal(looksLikeChatCompletion("https://janitorai.com/chat/completions", undefined), true);
});

test("looksLikeChatCompletion falls back to sniffing a messages[] + model body", () => {
  const { looksLikeChatCompletion } = load();
  const body = JSON.stringify({ model: "gpt", messages: [{ role: "user", content: "hi" }] });
  assert.equal(looksLikeChatCompletion("https://example.com/anything", body), true);
});

test("looksLikeChatCompletion rejects a body missing messages[] or model", () => {
  const { looksLikeChatCompletion } = load();
  assert.equal(
    looksLikeChatCompletion("https://example.com/x", JSON.stringify({ model: "gpt" })),
    false
  );
  assert.equal(
    looksLikeChatCompletion(
      "https://example.com/x",
      JSON.stringify({ messages: "not-an-array", model: "gpt" })
    ),
    false
  );
});

test("looksLikeChatCompletion rejects unrelated URLs with no/invalid body", () => {
  const { looksLikeChatCompletion } = load();
  assert.equal(looksLikeChatCompletion("https://example.com/x", null), false);
  assert.equal(looksLikeChatCompletion("https://example.com/x", "not json"), false);
});

test("looksLikeModelsProbe matches a GET against our own server's /models", () => {
  const { looksLikeModelsProbe } = load();
  assert.equal(looksLikeModelsProbe("GET", "http://127.0.0.1:8000/v1/models"), true);
  assert.equal(looksLikeModelsProbe(undefined, "http://127.0.0.1:8000/v1/models"), true); // defaults to GET
});

test("looksLikeModelsProbe rejects non-GET methods", () => {
  const { looksLikeModelsProbe } = load();
  assert.equal(looksLikeModelsProbe("POST", "http://127.0.0.1:8000/v1/models"), false);
});

test("looksLikeModelsProbe follows SERVER to a remote host", () => {
  // The probe is matched with url.startsWith(SERVER), so pointing the bridge
  // at a container elsewhere means the URL configured in JanitorAI's provider
  // settings has to move with it -- a mismatch stops the probe being
  // intercepted, silently. This is the case that catches that coupling.
  const { looksLikeModelsProbe } = load("http://192.168.1.50:8000");
  assert.equal(looksLikeModelsProbe("GET", "http://192.168.1.50:8000/v1/models"), true);
  assert.equal(looksLikeModelsProbe("GET", "http://127.0.0.1:8000/v1/models"), false);
});

test("looksLikeModelsProbe never shadows JanitorAI's own unrelated /models calls", () => {
  const { looksLikeModelsProbe } = load();
  assert.equal(looksLikeModelsProbe("GET", "https://janitorai.com/some/models"), false);
  assert.equal(looksLikeModelsProbe("GET", ""), false);
});

// ---------------------------------------------------------------------------
// The injected page-realm hook. These run pageHookSource()'s output in a vm
// context standing in for the page: the point is that the hook patches *that*
// realm's fetch, which is the whole reason it is injected rather than assigned
// from the userscript sandbox.
// ---------------------------------------------------------------------------

// Enough of a page to install into: an event target, an <html> to mark, a
// fetch/XHR to replace, and a Response that records what it was built with.
function makePageRealm() {
  const listeners = new Map();
  const document = {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    dispatchEvent(event) {
      for (const fn of listeners.get(event.type) || []) fn(event);
      return true;
    },
    documentElement: {
      attributes: {},
      setAttribute(name, value) {
        this.attributes[name] = value;
      },
      getAttribute(name) {
        return name in this.attributes ? this.attributes[name] : null;
      },
    },
  };
  const calls = { original: [] };
  function XHRStub() {}
  XHRStub.prototype.open = function () {};
  XHRStub.prototype.send = function () {};
  const context = {
    document,
    console: { log() {}, warn() {} },
    window: {
      fetch: async (...args) => {
        calls.original.push(args);
        return "ORIGINAL";
      },
      XMLHttpRequest: XHRStub,
    },
    CustomEvent: class {
      constructor(type, init) {
        this.type = type;
        this.detail = init && init.detail;
      }
    },
    Response: class {
      constructor(body, init) {
        this.body = body;
        this.status = init.status;
        this.headers = init.headers;
      }
    },
  };
  vm.createContext(context);
  return { context, document, calls };
}

// Stands in for the userscript sandbox on the other side of the bridge:
// answers a relay request with `reply`, and records what was asked.
function attachSandbox(realm, reply) {
  const seen = [];
  realm.document.addEventListener("aspect:relay-request", (event) => {
    const message = JSON.parse(event.detail);
    seen.push(message);
    realm.document.dispatchEvent(
      new realm.context.CustomEvent("aspect:relay-response", {
        detail: JSON.stringify({ id: message.id, ...reply(message) }),
      })
    );
  });
  return seen;
}

function installPageHook(server = "http://192.168.1.50:8000") {
  const { pageHookSource } = load(server);
  const realm = makePageRealm();
  vm.runInContext(pageHookSource(), realm.context, { filename: "page-hook.js" });
  return realm;
}

test("the injected source patches the PAGE realm's fetch, not the sandbox's", () => {
  const realm = installPageHook();
  assert.equal(
    realm.document.documentElement.getAttribute("data-aspect-hook"),
    "1",
    "the hook marks <html> so the injector can tell it actually ran"
  );
  assert.notEqual(realm.context.window.fetch.name, "fetch");
});

test("a chat completion is relayed over the bridge and answered as a Response", async () => {
  const realm = installPageHook();
  const asked = attachSandbox(realm, () => ({ ok: true, text: '{"choices":[]}' }));

  const response = await realm.context.window.fetch(
    "http://192.168.1.50:8000/v1/chat/completions",
    { method: "POST", body: JSON.stringify({ model: "m", messages: [{ role: "system" }] }) }
  );

  assert.equal(asked.length, 1);
  assert.equal(asked[0].kind, "relay");
  assert.deepEqual(asked[0].body.messages, [{ role: "system" }]);
  assert.equal(response.body, '{"choices":[]}');
  assert.equal(response.headers["Content-Type"], "application/json");
  assert.equal(realm.calls.original.length, 0, "the page's own fetch is never dialed");
});

test("a streaming request gets the SSE content type back", async () => {
  const realm = installPageHook();
  attachSandbox(realm, () => ({ ok: true, text: "data: [DONE]\n\n" }));

  const response = await realm.context.window.fetch(
    "http://192.168.1.50:8000/v1/chat/completions",
    { method: "POST", body: JSON.stringify({ model: "m", stream: true, messages: [] }) }
  );

  assert.equal(response.headers["Content-Type"], "text/event-stream");
});

test("a failed relay falls through to the page's own fetch", async () => {
  const realm = installPageHook();
  attachSandbox(realm, () => ({ ok: false, error: "server down" }));

  const result = await realm.context.window.fetch(
    "http://192.168.1.50:8000/v1/chat/completions",
    { method: "POST", body: JSON.stringify({ model: "m", messages: [] }) }
  );

  assert.equal(result, "ORIGINAL");
  assert.equal(realm.calls.original.length, 1);
});

test("unrelated requests are passed straight through", async () => {
  const realm = installPageHook();
  const asked = attachSandbox(realm, () => ({ ok: true, text: "" }));

  const result = await realm.context.window.fetch("https://janitorai.com/hampter/characters");

  assert.equal(result, "ORIGINAL");
  assert.equal(asked.length, 0);
});
