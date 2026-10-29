"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { loadModules } = require("./helpers/load-src");

// scheduler.js has no top-level side effects (tick/runLoop are only ever
// declared, never invoked at load time), so it loads cleanly with just the
// couple of DOM-ish globals characterName/currentCharacterId/isChatView need.
// location/document are shared, mutable objects so a single load can drive
// every assertion below.
function load() {
  const location = { pathname: "" };
  const document = { _hasMessageBody: false, querySelector() { return this._hasMessageBody ? {} : null; } };
  const mods = loadModules(
    ["scheduler.js"],
    ["characterName", "currentCharacterId", "isChatView", "pageKindFor"],
    { location, document }
  );
  return { ...mods, location, document };
}

test("characterName prefers chat_name and trims whitespace", () => {
  const { characterName } = load();
  assert.equal(characterName({ chat_name: "  Aurora  ", name: "Aurora (title blurb)" }), "Aurora");
});

test("characterName falls back to the title blurb when chat_name is absent", () => {
  const { characterName } = load();
  assert.equal(characterName({ name: "Some Blurb" }), "Some Blurb");
});

test("characterName returns an empty string for missing/null input", () => {
  const { characterName } = load();
  assert.equal(characterName({}), "");
  assert.equal(characterName(null), "");
  assert.equal(characterName(undefined), "");
});

test("currentCharacterId reads and lower-cases the uuid from /characters/<uuid>_slug", () => {
  const { currentCharacterId, location } = load();
  location.pathname = "/characters/1A2B3C4D-1234-5678-9ABC-1234567890AB_some-slug";
  assert.equal(currentCharacterId(), "1a2b3c4d-1234-5678-9abc-1234567890ab");
});

test("currentCharacterId returns null off a character page (e.g. a /chats/ url)", () => {
  const { currentCharacterId, location } = load();
  location.pathname = "/chats/1a2b3c4d-1234-5678-9abc-1234567890ab";
  assert.equal(currentCharacterId(), null);
});

test("isChatView reflects whether a message-body element is present", () => {
  const { isChatView, document } = load();
  assert.equal(isChatView(), false);
  document._hasMessageBody = true;
  assert.equal(isChatView(), true);
});

const UUID = "1a2b3c4d-1234-5678-9abc-1234567890ab";

test("pageKindFor: a character page is 'character'", () => {
  const { pageKindFor } = load();
  assert.equal(pageKindFor(`/characters/${UUID}_some-slug`, false), "character");
});

test("pageKindFor: a /chats/ url or a chat in view is 'chat'", () => {
  const { pageKindFor } = load();
  assert.equal(pageKindFor(`/chats/${UUID}`, false), "chat");
  assert.equal(pageKindFor("/", true), "chat");
});

test("pageKindFor: a creator profile is 'profile'", () => {
  const { pageKindFor } = load();
  assert.equal(pageKindFor(`/profiles/${UUID}_creator`, false), "profile");
});

test("pageKindFor: home, search and tag browse are all 'listing'", () => {
  const { pageKindFor } = load();
  assert.equal(pageKindFor("/", false), "listing");
  assert.equal(pageKindFor("/search", false), "listing");
  assert.equal(pageKindFor("/tags/female", false), "listing");
});
