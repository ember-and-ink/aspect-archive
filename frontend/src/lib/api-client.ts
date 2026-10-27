import createClient from 'openapi-fetch'
import type { paths } from './api-schema'

// The archive server. In dev, Vite proxies /api and friends to :8000
// (vite.config.ts), so a same-origin base URL is right in every environment --
// the app's `base` applies to its assets, not to the API.
export const apiClient = createClient<paths>({
  baseUrl: window.location.origin,
  // Resolve `fetch` lazily rather than capturing it at module-load time, so
  // test tooling (MSW) that replaces `globalThis.fetch` after this module has
  // loaded still intercepts requests made through this client.
  fetch: (...args: Parameters<typeof fetch>) => globalThis.fetch(...args),
})

/**
 * Fired on `window` when an API call comes back 401 — the login gate
 * (`aspect/api/gate.py`) is on and this browser's session is gone. `AuthGate`
 * listens and swaps the app for the login screen.
 *
 * An event rather than a query-client call so this module stays free of React:
 * it is imported by code that has no client to hand.
 */
export const UNAUTHORIZED_EVENT = 'aspect:unauthorized'

apiClient.use({
  onResponse({ request, response }) {
    // The auth routes' own 401 is "wrong password", which the login form shows
    // itself.
    if (
      response.status === 401 &&
      !new URL(request.url).pathname.startsWith('/api/v1/auth/')
    ) {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    }
  },
})

/** What every `apiClient` call resolves to: one of `data`/`error`, and the response. */
type ApiResult<T> = {
  data?: T
  error?: unknown
  response: Response
}

/**
 * Turn an `apiClient` result into the data, or throw something a query can show.
 *
 * Worth having rather than checking `error` at each call site, because
 * `openapi-fetch` only populates `error` when the failing response had a body
 * it could parse. A 500 with an empty body — which is what an unhandled server
 * exception and a dead upstream both look like — arrives as *neither* `data`
 * nor `error`, and a query function that only tests `error` then returns
 * `undefined`, which TanStack Query reports as the useless "data is undefined".
 *
 * So the status is the thing to trust, and FastAPI's `{detail}` is used for the
 * message only when it is actually there.
 */
export async function unwrap<T>(
  call: Promise<ApiResult<T>>,
  what: string,
): Promise<T> {
  const { data, error, response } = await call
  if (!response.ok) {
    const detail =
      error && typeof error === 'object' && 'detail' in error
        ? String((error as { detail: unknown }).detail)
        : `${response.status} ${response.statusText}`.trim()
    throw new Error(`${what}: ${detail}`)
  }
  if (data === undefined) {
    throw new Error(`${what}: the server returned an empty body`)
  }
  return data
}
