import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { apiClient, unwrap } from '@/lib/api-client'
import type { components } from '@/lib/api-schema'

export type Lorebook = components['schemas']['LorebookOut']
export type LorebookDetail = components['schemas']['LorebookDetailOut']
export type LorebookEntry = components['schemas']['LorebookEntryOut']
export type LorebookEntryDetail =
  components['schemas']['LorebookEntryDetailOut']
export type LorebookCard = components['schemas']['LorebookCardOut']
export type CardLorebook = components['schemas']['CardLorebookOut']
export type SyncPlanCard = components['schemas']['LorebookSyncCardOut']
export type SyncResult = components['schemas']['LorebookSyncOut']

/** Every distinct lorebook, whole. A few hundred rows even on the full
 *  archive, so filtering and sorting happen client-side over one response. */
export function useLorebooks() {
  return useQuery({
    queryKey: ['lorebooks'],
    queryFn: () =>
      unwrap(apiClient.GET('/api/v1/lorebooks'), 'could not load lorebooks'),
  })
}

export function useLorebook(id: string) {
  return useQuery({
    queryKey: ['lorebook', id],
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/lorebooks/{lorebook_id}', {
          params: { path: { lorebook_id: id } },
        }),
        'could not load the lorebook',
      ),
  })
}

export type EntryQuery = {
  q: string
  changed: boolean
  unresolved: boolean
  sort: 'versions' | 'cards' | 'title'
  limit: number
}

/** Entries are the one lorebook list too long to send whole (~10k lines), so
 *  search and the changed-only filter run server-side. */
export function useLorebookEntries(query: EntryQuery) {
  return useQuery({
    queryKey: ['lorebook-entries', query],
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/lorebooks/entries', {
          params: { query },
        }),
        'could not load lorebook entries',
      ),
    placeholderData: keepPreviousData,
  })
}

export function useLorebookEntry(id: string) {
  return useQuery({
    queryKey: ['lorebook-entry', id],
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/lorebooks/entries/{entry_id}', {
          params: { path: { entry_id: id } },
        }),
        'could not load the entry',
      ),
  })
}

/** What a sync would do, card by card. `lorebook` scopes it to one lorebook's
 *  changes; empty is everything. Fetched only while something is showing it. */
export function useSyncPlan(lorebook: string, enabled: boolean) {
  return useQuery({
    queryKey: ['lorebook-sync', lorebook],
    enabled,
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/lorebooks/sync', {
          params: { query: lorebook ? { lorebook } : {} },
        }),
        'could not work out the sync',
      ),
  })
}

/** The lorebooks on one card, plus any waiting to be attached to it. */
export function useCardLorebooks(card: string) {
  return useQuery({
    queryKey: ['card-lorebooks', card],
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/characters/{card_id}/lorebooks', {
          params: { path: { card_id: card } },
        }),
        'could not read the card’s lorebooks',
      ),
  })
}

/** Whether a sync has rewritten this card's lorebook, and can be undone. */
export function useLorebookOriginal(card: string) {
  return useQuery({
    queryKey: ['lorebook-original', card],
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/characters/{card_id}/lorebook-original', {
          params: { path: { card_id: card } },
        }),
        'could not check for the original lorebook',
      ),
  })
}

const FAMILY = [
  'lorebooks',
  'lorebook',
  'lorebook-entries',
  'lorebook-entry',
  'lorebook-sync',
  'card-lorebooks',
  'lorebook-original',
]
/** What else goes stale when an action rewrites cards rather than recording a
 *  decision: the cards themselves. */
const CARDS = ['character', 'characters']

function useAction<A, R>(run: (args: A) => Promise<R>, writesCards = false) {
  const client = useQueryClient()
  return useMutation<R, Error, A>({
    mutationFn: run,
    onSuccess: () =>
      Promise.all(
        [...FAMILY, ...(writesCards ? CARDS : [])].map((key) =>
          client.invalidateQueries({ queryKey: [key] }),
        ),
      ),
  })
}

/** For the DELETEs that answer 204: there is no body to unwrap. */
async function done(call: Promise<{ response: Response }>, what: string) {
  const { response } = await call
  if (!response.ok)
    throw new Error(`${what}: ${response.status} ${response.statusText}`)
}

/**
 * The lorebook decisions: choose a version, merge two entries or two
 * lorebooks, attach a lorebook to a card or detach it, and the undo of each.
 *
 * None of those writes to a card -- they are recorded server-side
 * (`data/lorebooks.json`) and change what these pages report. Every one moves
 * counts on every lorebook view at once (a merge even changes ids), so they
 * all invalidate the whole family rather than patching caches.
 *
 * `sync` and `restoreOriginal` are the two that do rewrite cards.
 */
export function useLorebookActions() {
  return {
    choose: useAction(({ entry, hash }: { entry: string; hash: string }) =>
      unwrap(
        apiClient.PUT('/api/v1/lorebooks/entries/{entry_id}/chosen', {
          params: { path: { entry_id: entry } },
          body: { hash },
        }),
        'could not choose the version',
      ),
    ),
    clearChoice: useAction((entry: string) =>
      unwrap(
        apiClient.DELETE('/api/v1/lorebooks/entries/{entry_id}/chosen', {
          params: { path: { entry_id: entry } },
        }),
        'could not clear the choice',
      ),
    ),
    chooseNewest: useAction((lorebook: string) =>
      unwrap(
        apiClient.POST('/api/v1/lorebooks/{lorebook_id}/choose-newest', {
          params: { path: { lorebook_id: lorebook } },
        }),
        'could not choose the newest versions',
      ),
    ),
    mergeEntries: useAction(
      ({ entry, into }: { entry: string; into: string }) =>
        unwrap(
          apiClient.POST('/api/v1/lorebooks/entries/{entry_id}/merge', {
            params: { path: { entry_id: entry } },
            body: { into },
          }),
          'could not merge the entries',
        ),
    ),
    unmergeEntries: useAction((merge: string) =>
      done(
        apiClient.DELETE('/api/v1/lorebooks/entries/merges/{merge_id}', {
          params: { path: { merge_id: merge } },
        }),
        'could not undo the merge',
      ),
    ),
    mergeLorebooks: useAction(
      ({ lorebook, into }: { lorebook: string; into: string }) =>
        unwrap(
          apiClient.POST('/api/v1/lorebooks/{lorebook_id}/merge', {
            params: { path: { lorebook_id: lorebook } },
            body: { into },
          }),
          'could not merge the lorebooks',
        ),
    ),
    unmergeLorebooks: useAction((merge: string) =>
      done(
        apiClient.DELETE('/api/v1/lorebooks/merges/{merge_id}', {
          params: { path: { merge_id: merge } },
        }),
        'could not undo the merge',
      ),
    ),
    /** Also how a pending detach is withdrawn. */
    attach: useAction(
      ({ card, lorebook }: { card: string; lorebook: string }) =>
        unwrap(
          apiClient.PUT(
            '/api/v1/characters/{card_id}/lorebooks/{lorebook_id}',
            {
              params: { path: { card_id: card, lorebook_id: lorebook } },
            },
          ),
          'could not attach the lorebook',
        ),
    ),
    /** Also how a pending attach is withdrawn. */
    detach: useAction(
      ({ card, lorebook }: { card: string; lorebook: string }) =>
        unwrap(
          apiClient.DELETE(
            '/api/v1/characters/{card_id}/lorebooks/{lorebook_id}',
            { params: { path: { card_id: card, lorebook_id: lorebook } } },
          ),
          'could not detach the lorebook',
        ),
    ),
    sync: useAction(
      ({ lorebook = '' }: { lorebook?: string }) =>
        unwrap(
          apiClient.POST('/api/v1/lorebooks/sync', {
            body: { lorebook, cards: [] },
          }),
          'could not sync the lorebooks',
        ),
      true,
    ),
    restoreOriginal: useAction(
      (card: string) =>
        unwrap(
          apiClient.POST(
            '/api/v1/characters/{card_id}/lorebook-original/restore',
            { params: { path: { card_id: card } } },
          ),
          'could not restore the original lorebook',
        ),
      true,
    ),
  }
}
