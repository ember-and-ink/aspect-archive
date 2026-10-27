import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiClient, unwrap } from '@/lib/api-client'

/**
 * The stored settings blob (docs/UI_REWRITE_PLAN.md §3.7).
 *
 * It is an opaque object the server neither reads nor schema-checks. The new app
 * keeps all of its own keys under a single `ui2` namespace so it never collides
 * with the keys the old `web/` UI writes into the same blob during the overlap.
 */
export type SettingsBlob = Record<string, unknown> &
  Partial<ProviderSettings> & { ui2?: Ui2Settings }

/**
 * Provider credentials and followed-creator lists, which live at the blob's
 * ROOT rather than under `ui2` (Stage 6B).
 *
 * The same exception `httpProxyUrl` already has, for the same reason: these are
 * not new preferences this app invented, they are the keys the old UI and the
 * server-side scripts already read and write, holding real data on disk. Moving
 * a live `chubToken` under `ui2` would not "namespace" it, it would orphan it.
 * Anything this app invents for itself still goes in `Ui2Settings`.
 */
export interface ProviderSettings {
  /** Chub's URQL_TOKEN, sent as `Authorization: Bearer`. Despite the name this
   *  is not GraphQL -- `URQL_TOKEN` is just the localStorage key chub.ai keeps
   *  it under, and the value is a plain REST bearer token. */
  chubToken: string | null
  /** Whether to keep `chubToken` across a settings reset. */
  chubRememberToken: boolean
  chubNsfw: boolean
  datacatToken: string | null
  datacatNsfw: boolean
  /** Civitai's API key. Not a card provider -- this is read by the Civitai
   *  gallery extractor (`aspect/media/civitai.py`) and only matters for images
   *  Civitai hides from logged-out visitors. */
  civitaiApiKey: string | null
  /** Creators followed on DataCat. Purely local -- DataCat has no account to
   *  sync with -- so this list *is* the feature, not a cache of one. */
  datacatFollowedCreators: FollowedCreator[]
  /** Always-excluded tags per provider, layered under the session chips. */
  providerExcludeTags: Record<string, string[]>
}

export interface FollowedCreator {
  id: string
  name: string
  source: string
}

const PROVIDER_DEFAULTS: ProviderSettings = {
  chubToken: null,
  chubRememberToken: true,
  chubNsfw: true,
  datacatToken: null,
  datacatNsfw: true,
  civitaiApiKey: null,
  datacatFollowedCreators: [],
  providerExcludeTags: {},
}

/** The new app's own settings. Grows as later stages need it. */
export interface Ui2Settings {
  /** The tag dictionary delta (see tag-delta.ts). */
  tagDictionaryDelta?: unknown
  /** Duplicate-group ids (`DuplicateGroupOut.group_id`) the user has reviewed
   *  and decided are not duplicates. Filtered out of the Duplicates section's
   *  list client-side; the server makes no duplicate-matching decisions of
   *  its own to remember this against. */
  duplicatesDismissed?: string[]
  /** Applied to the Characters grid whenever the URL carries no `sort=` of
   *  its own -- an explicit link (the shelf's "See all", a bookmark) always
   *  wins over this. One of `lib/browse.ts`'s `SORTS` values. */
  defaultSort?: string
  /** Whether the "Recently added" shelf shows above the grid. Also flipped by
   *  the shelf's own Hide button, so the two controls agree. */
  showRecentShelf?: boolean
  /** Whether the Characters grid titles its tiles with the source page's
   *  listing name (the tagline) instead of the character's name. Flipped by
   *  clicking the page heading. A view preference rather than a filter, so it
   *  is stored rather than carried in the URL. */
  showListingNames?: boolean
  /** Which providers Discover's toggle offers. Absent = both enabled, the
   *  same default the page had before this setting existed. */
  providers?: { chub?: boolean; datacat?: boolean }
  [key: string]: unknown
}

export function useSettings() {
  return useQuery({
    queryKey: ['settings'],
    // The blob changes only when this app (or the old UI) writes it, and it is
    // read on route entry rather than continuously.
    staleTime: 60_000,
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/settings'),
        'could not read settings',
      ) as Promise<SettingsBlob>,
  })
}

/**
 * Update one key under the `ui2` namespace with a read-modify-write.
 *
 * **The trap this exists to avoid (§3.7):** `PUT /settings` is a whole-document
 * replace, and that same blob is the only copy of the old UI's Chub and DataCat
 * tokens. Writing just `{ ui2: … }` would destroy them, and the failure would
 * surface later, in Discover, looking like an auth bug. So every write fetches
 * the current blob first and merges onto it. The GET is fresh (not the cached
 * query) so two writes in quick succession cannot clobber each other's merge.
 */
export function useUpdateUi2() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ key, value }: { key: string; value: unknown }) => {
      const current = (await unwrap(
        apiClient.GET('/api/v1/settings'),
        'could not read settings',
      )) as SettingsBlob
      const merged: SettingsBlob = {
        ...current,
        ui2: { ...(current.ui2 ?? {}), [key]: value },
      }
      await unwrap(
        apiClient.PUT('/api/v1/settings', { body: merged }),
        'could not save settings',
      )
      return merged
    },
    onSuccess: (merged) => {
      qc.setQueryData(['settings'], merged)
    },
  })
}

/**
 * Update one or more keys at the blob's ROOT, not under `ui2`.
 *
 * The deliberate exception to the namespacing rule above, for two kinds of key:
 * `httpProxyUrl`, the single key `aspect/runtime/net.py` reads out of this file
 * (see its `SETTINGS_KEY`) and reads *flat*; and the `ProviderSettings` keys,
 * which already hold real data written by the old UI. Nesting either under
 * `ui2` would not namespace it, it would orphan it -- and the failure would
 * surface later, in Discover, looking like an auth bug.
 *
 * Same read-modify-write as `useUpdateUi2`: fetch fresh, merge, PUT the whole
 * document, so nothing else in the blob is destroyed.
 */
export function useUpdateRoot() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (patch: Partial<SettingsBlob>) => {
      const current = (await unwrap(
        apiClient.GET('/api/v1/settings'),
        'could not read settings',
      )) as SettingsBlob
      const merged: SettingsBlob = { ...current, ...patch }
      await unwrap(
        apiClient.PUT('/api/v1/settings', { body: merged }),
        'could not save settings',
      )
      return merged
    },
    onSuccess: (merged) => {
      qc.setQueryData(['settings'], merged)
    },
  })
}

/** `useUpdateRoot` narrowed to the proxy URL, which is where this started.
 *  The second argument is react-query's per-call `{onSuccess, onError}`, which
 *  callers do use -- dropping it silently swallows their toasts. */
export function useUpdateProxyUrl() {
  const update = useUpdateRoot()
  type Options = Parameters<typeof update.mutate>[1]
  return {
    ...update,
    mutate: (value: string, options?: Options) =>
      update.mutate({ httpProxyUrl: value }, options),
    mutateAsync: (value: string, options?: Options) =>
      update.mutateAsync({ httpProxyUrl: value }, options),
  }
}

/**
 * The provider keys, with defaults applied so callers never branch on
 * "absent vs false". Read-only; write with `useUpdateRoot`.
 */
export function useProviderSettings(): ProviderSettings & { loaded: boolean } {
  const { data } = useSettings()
  const blob = (data ?? {}) as SettingsBlob
  // `null` is a real stored value for the credentials ("explicitly cleared"),
  // so only `undefined` falls back to the default.
  const pick = <K extends keyof ProviderSettings>(
    key: K,
  ): ProviderSettings[K] =>
    blob[key] === undefined
      ? PROVIDER_DEFAULTS[key]
      : (blob[key] as ProviderSettings[K])

  return {
    chubToken: pick('chubToken') ?? null,
    chubRememberToken: pick('chubRememberToken') ?? true,
    chubNsfw: pick('chubNsfw') ?? true,
    datacatToken: pick('datacatToken') ?? null,
    datacatNsfw: pick('datacatNsfw') ?? true,
    civitaiApiKey: pick('civitaiApiKey') ?? null,
    datacatFollowedCreators: pick('datacatFollowedCreators') ?? [],
    providerExcludeTags: pick('providerExcludeTags') ?? {},
    loaded: data !== undefined,
  }
}
