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
