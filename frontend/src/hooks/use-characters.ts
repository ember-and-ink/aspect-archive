import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { apiClient, unwrap } from '@/lib/api-client'
import { toQuery, type BrowseState } from '@/lib/browse'

/**
 * How many cards a page of the grid holds.
 *
 * The whole reason there is no virtualizer (docs/UI_REWRITE_PLAN.md §4.3): the
 * old UI fetched all 3,839 cards as a 5.9 MB boot payload and then virtualized
 * the DOM to survive it. Paging server-side makes the first paint ~40 KB and
 * lets the DOM grow only as far as the user actually scrolls.
 */
const PAGE_SIZE = 100

export function useCharacters(state: BrowseState) {
  const query = toQuery(state)
  return useInfiniteQuery({
    queryKey: ['characters', query],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      unwrap(
        apiClient.GET('/api/v1/characters', {
          params: { query: { ...query, limit: PAGE_SIZE, offset: pageParam } },
        }),
        'could not list the archive',
      ),
    getNextPageParam: (last) => {
      const seen = last.offset + last.items.length
      return seen < last.total ? seen : undefined
    },
  })
}

/**
 * The newest cards, for the shelf. A second query rather than a slice of the
 * grid's own: the shelf shows what arrived most recently whatever the grid is
 * currently sorted by, which is the whole point of it.
 */
export function useRecentlyAdded(count: number) {
  return useQuery({
    queryKey: ['characters', 'recent', count],
    enabled: count > 0,
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/characters', {
          params: { query: { sort: '-added', limit: count } },
        }),
        'could not read recent cards',
      ),
  })
}

/**
 * The catalogues behind the Tags, Creator and Source pills — every tag, creator
 * and source_kind the archive holds, counted over all of it.
 *
 * One query for all three: `/facets` computes them in a single sweep and the
 * three pills would otherwise fire three identical requests.
 */
export function useFacets() {
  return useQuery({
    queryKey: ['facets'],
    // Tags change only when a card is edited or imported, and the popover is
    // opened repeatedly during one browse session.
    staleTime: 5 * 60_000,
    queryFn: () =>
      unwrap(
        // The whole vocabulary, not a top-N slice. The popover has its own
        // search box, so a cap silently made rare tags unfindable rather than
        // merely unlisted -- on this archive 231 of 631 tags fell off a
        // 400-tag limit, every one of them a tag a card actually carries.
        apiClient.GET('/api/v1/facets', { params: { query: { limit: 0 } } }),
        'could not read the tag list',
      ),
  })
}

/**
 * How many cards the archive holds, for the browse header's "of N". Its own
 * route rather than `/stats`, which stats every gallery file and thumb on the
 * share (~5 s) to fill the Settings sections that actually show those numbers.
 */
export function useArchiveCount() {
  return useQuery({
    queryKey: ['count'],
    staleTime: 5 * 60_000,
    queryFn: () =>
      unwrap(apiClient.GET('/api/v1/count'), 'could not count the archive'),
  })
}

/** The full archive and disk health -- Settings only; see `useArchiveCount`. */
export function useArchiveStats() {
  return useQuery({
    queryKey: ['stats'],
    staleTime: 5 * 60_000,
    queryFn: () =>
      unwrap(apiClient.GET('/api/v1/stats'), 'could not read the archive'),
  })
}
