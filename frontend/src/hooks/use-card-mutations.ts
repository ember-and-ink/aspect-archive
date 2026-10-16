import { useMutation, useQueryClient } from '@tanstack/react-query'
import { apiClient } from '@/lib/api-client'
import type { components } from '@/lib/api-schema'
import type { CardData } from '@/lib/card'
import type { CardDetail, CardDetailResult } from './use-character-detail'

export type CardImport = components['schemas']['CardImportOut']

/**
 * Thrown when a write's `If-Match` did not match — another tab, or the old UI
 * during the overlap, wrote the card first. The UI shows this specially and
 * offers reload rather than retrying blind (docs/UI_REWRITE_PLAN.md §4.4).
 */
export class StaleWriteError extends Error {
  constructor() {
    super(
      'This card changed since you opened it. Reload before saving over it.',
    )
    this.name = 'StaleWriteError'
  }
}

/**
 * The queries a write can invalidate: the grid and the shelf both key on
 * `['characters', …]`, the tag popover on `['facets']`, the counts on
 * `['stats']`, and the detail's Related rows on `['related', …]`. A card edit
 * can move any of them, so all four are refetched after one lands.
 */
export function invalidateArchive(client: ReturnType<typeof useQueryClient>) {
  for (const key of [['characters'], ['facets'], ['stats'], ['related']])
    void client.invalidateQueries({ queryKey: key })
}

/**
 * Whole-card `PUT` with the detail read's `ETag` as the precondition.
 *
 * The mutation carries the `card` (a `data` object built by `lib/card-edit`) and
 * the `etag` the page is holding. A 412 becomes a `StaleWriteError`; success
 * writes the fresh card *and its new ETag* straight into the detail cache, so a
 * second edit in the same session has a current precondition without a refetch.
 */
export function usePutCharacter(id: string) {
  const client = useQueryClient()
  return useMutation<
    CardDetailResult,
    Error,
    { card: CardData; etag: string | null }
  >({
    mutationFn: async ({ card, etag }) => {
      const { data, response } = await apiClient.PUT(
        '/api/v1/characters/{card_id}',
        {
          params: { path: { card_id: id } },
          headers: etag ? { 'If-Match': etag } : undefined,
          body: { card },
        },
      )
      if (response.status === 412) throw new StaleWriteError()
      if (!response.ok || data === undefined)
        throw new Error(
          `could not save the card: ${response.status} ${response.statusText}`.trim(),
        )
      return { card: data, etag: response.headers.get('ETag') }
    },
    onSuccess: (result) => {
      client.setQueryData(['character', id], result)
      invalidateArchive(client)
    },
  })
}

/**
 * Star or unstar. The one targeted write — a boolean, no read-detail dance — so
 * it is optimistic: the star flips at once and rolls back only if the write
 * fails, then the grid is invalidated so its tiles agree.
 */
export function useSetFavorite(id: string) {
  const client = useQueryClient()
  return useMutation<boolean, Error, boolean, { previous?: CardDetailResult }>({
    mutationFn: async (value) => {
      const { data, response } = await apiClient.POST(
        '/api/v1/characters/{card_id}/favorite',
        { params: { path: { card_id: id } }, body: { value } },
      )
      if (!response.ok || data === undefined)
        throw new Error('could not update the favourite')
      return data.favorite
    },
    onMutate: async (value) => {
      await client.cancelQueries({ queryKey: ['character', id] })
      const previous = client.getQueryData<CardDetailResult>(['character', id])
      if (previous)
        client.setQueryData<CardDetailResult>(['character', id], {
          ...previous,
          card: { ...previous.card, favorite: value },
        })
      return { previous }
    },
    onError: (_error, _value, context) => {
      if (context?.previous)
        client.setQueryData(['character', id], context.previous)
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: ['characters'] })
    },
  })
}

/** Bin a card, optionally its gallery with it. */
export function useDeleteCharacter(id: string) {
  const client = useQueryClient()
  return useMutation<void, Error, 'keep' | 'delete'>({
    mutationFn: async (gallery) => {
      const { response } = await apiClient.DELETE(
        '/api/v1/characters/{card_id}',
        {
          params: { path: { card_id: id }, query: { gallery } },
        },
      )
      if (!response.ok)
        throw new Error(
          `could not delete the card: ${response.status} ${response.statusText}`.trim(),
        )
    },
    onSuccess: () => {
      client.removeQueries({ queryKey: ['character', id] })
      invalidateArchive(client)
    },
  })
}

/**
 * Bin many cards in one request — the batch-select grid's bulk delete.
 * Partial success comes back rather than throwing, since one bad id must not
 * hide that the rest actually binned (mirrors the server's `bulk_tags` shape).
 */
export function useBulkDeleteCharacters() {
  const client = useQueryClient()
  return useMutation<
    { deleted: string[]; failed: Record<string, string> },
    Error,
    { ids: string[]; gallery: 'keep' | 'delete' }
  >({
    mutationFn: async ({ ids, gallery }) => {
      const { data, response } = await apiClient.POST(
        '/api/v1/characters/bulk-delete',
        { body: { ids, gallery } },
      )
      if (!response.ok || data === undefined)
        throw new Error(
          `could not delete the cards: ${response.status} ${response.statusText}`.trim(),
        )
      return data
    },
    onSuccess: () => invalidateArchive(client),
  })
}

/**
 * Replace the portrait. Multipart, so it goes through `fetch` directly rather
 * than `openapi-fetch` — the endpoint re-encodes through intake's pipeline, and
 * the returned detail (with a fresh ETag) replaces the cache. The pixels change
 * behind an unchanged URL, so the caller cache-busts the image off the new ETag.
 */
export function useReplaceAvatar(id: string) {
  const client = useQueryClient()
  return useMutation<
    CardDetailResult,
    Error,
    { file: File; etag: string | null }
  >({
    mutationFn: async ({ file, etag }) => {
      const body = new FormData()
      body.set('image', file)
      const response = await fetch(
        `/api/v1/characters/${encodeURIComponent(id)}/avatar`,
        {
          method: 'PUT',
          body,
          headers: etag ? { 'If-Match': etag } : undefined,
        },
      )
      if (response.status === 412) throw new StaleWriteError()
      if (!response.ok) {
        const detail = await response.json().catch(() => null)
        throw new Error(
          detail?.detail ?? `could not replace the image: ${response.status}`,
        )
      }
      const data = (await response.json()) as CardDetailResult['card']
      return { card: data, etag: response.headers.get('ETag') }
    },
    onSuccess: (result) => {
      client.setQueryData(['character', id], result)
      invalidateArchive(client)
    },
  })
}

/**
 * Re-download the card's original image from its source and swap the pixels
 * in, keeping every field. The server refuses (422, card untouched) unless what
 * came back is clearly larger, and its reason is what the toast shows.
 */
export function useRefetchAvatar(id: string) {
  const client = useQueryClient()
  return useMutation<CardDetailResult, Error, { etag: string | null }>({
    mutationFn: async ({ etag }) => {
      const { data, error, response } = await apiClient.POST(
        '/api/v1/characters/{card_id}/avatar/refetch',
        {
          params: { path: { card_id: id } },
          headers: etag ? { 'If-Match': etag } : undefined,
        },
      )
      if (response.status === 412) throw new StaleWriteError()
      if (!response.ok || data === undefined) {
        const detail = (error as { detail?: unknown } | undefined)?.detail
        throw new Error(
          typeof detail === 'string'
            ? detail
            : `could not re-fetch the image: ${response.status}`,
        )
      }
      return { card: data, etag: response.headers.get('ETag') }
    },
    onSuccess: (result) => {
      client.setQueryData(['character', id], result)
      invalidateArchive(client)
    },
  })
}

/**
 * Fork a card — the born-here primitive (docs/FORKS_AND_EXTRAS_PLAN.md §3).
 * Returns the new card's detail so the caller can navigate straight to it;
 * no `etag` comes back (the route doesn't set one, same as every other write
 * here that returns via `get_character` internally), so the detail page's
 * own read supplies a fresh one.
 */
export function useForkCharacter(id: string) {
  const client = useQueryClient()
  return useMutation<CardDetail, Error, void>({
    mutationFn: async () => {
      const { data, response } = await apiClient.POST(
        '/api/v1/characters/{card_id}/fork',
        { params: { path: { card_id: id } } },
      )
      if (!response.ok || data === undefined)
        throw new Error(
          `could not fork the card: ${response.status} ${response.statusText}`.trim(),
        )
      return data
    },
    onSuccess: () => {
      invalidateArchive(client)
    },
  })
}

/**
 * Adopt a card PNG as a fork of the card whose `_<id8>` is `parentFragment` — the file-import primitive
 * (docs/FORKS_AND_EXTRAS_PLAN.md §3), for a rewrite made outside the archive.
 * The server stamps `extensions.fork`, shares the parent's gallery, and mints
 * a fresh id when the file still carries the parent's, so the rewrite isn't
 * waved through as the parent's own duplicate. A `duplicate: true` result
 * means this same file was already adopted; its `id` names that card.
 */
export function useImportFork(parentFragment: string) {
  const client = useQueryClient()
  return useMutation<CardImport, Error, File>({
    mutationFn: async (file) => {
      const body = new FormData()
      body.set('file', file, file.name)
      body.set('fork_of', parentFragment)
      const response = await fetch('/api/v1/characters', {
        method: 'POST',
        body,
      })
      if (!response.ok) {
        const detail = await response.json().catch(() => null)
        throw new Error(
          detail?.detail ?? `could not upload the fork: ${response.status}`,
        )
      }
      return (await response.json()) as CardImport
    },
    onSuccess: () => invalidateArchive(client),
  })
}

/**
 * Adopt one or more card PNGs. `POST /characters` takes one file, so multi-file
 * is a client loop — reported per file, since a duplicate is a success and a
 * failed file should not sink the rest.
 */
export function useImportCharacters() {
  const client = useQueryClient()
  return useMutation<
    { file: string; result?: CardImport; error?: string }[],
    Error,
    { files: File[]; onDuplicate: 'skip' | 'overwrite' }
  >({
    mutationFn: async ({ files, onDuplicate }) => {
      const results: { file: string; result?: CardImport; error?: string }[] =
        []
      for (const file of files) {
        const body = new FormData()
        body.set('file', file)
        body.set('on_duplicate', onDuplicate)
        try {
          const response = await fetch('/api/v1/characters', {
            method: 'POST',
            body,
          })
          if (!response.ok) {
            const detail = await response.json().catch(() => null)
            results.push({
              file: file.name,
              error: detail?.detail ?? `HTTP ${response.status}`,
            })
          } else {
            results.push({
              file: file.name,
              result: (await response.json()) as CardImport,
            })
          }
        } catch (error) {
          results.push({ file: file.name, error: String(error) })
        }
      }
      return results
    },
    onSuccess: () => invalidateArchive(client),
  })
}
