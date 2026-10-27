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
