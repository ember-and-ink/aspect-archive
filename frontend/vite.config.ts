/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// The archive server, for the dev proxy below.
const SERVER = 'http://localhost:8000'

// Everything on the server that the app calls, as a prefix list. `/api` covers
// both the archive contract (/api/v1) and DataCat's session transport
// (/api/v1/datacat); `/proxy` is the CORS passthrough a provider fetch falls
// back to, and without it Discover cannot work in dev. `/existing` is the
// duplicate guard, still living in the userscript namespace.
//
// `/build-` is the acquisition namespace (`/build-chub`, `/build-datacat`) and
// its absence here is why Discover's Get answered 404 on every click in dev:
// the POST never left Vite. These routes are mounted at the server root, not
// under /api (aspect/server.py mounts `build_router` unprefixed), so no other
// entry in this list covers them.
const SERVER_PATHS = ['/api', '/proxy', '/health', '/existing', '/build-']

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // The app owns the root: it is served from frontend/dist by the server
  // itself, and its assets are requested from /assets (docs/UI_REWRITE_PLAN.md
  // §2.5). This was '/next/' for the length of the overlap with `web/`.
  base: '/',
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    proxy: Object.fromEntries(
      SERVER_PATHS.map((prefix) => [
        prefix,
        { target: SERVER, changeOrigin: true },
      ]),
    ),
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
  },
})
