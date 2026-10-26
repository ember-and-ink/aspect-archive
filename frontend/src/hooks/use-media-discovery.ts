import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiClient, unwrap } from '@/lib/api-client'

/**
 * The client half of media discovery (docs/UI_REWRITE_PLAN.md §3.4, Stage 5):
 * scan a card's own text for remote image URLs (a dry run), then hand the
 * count off to a background job with `discover: true` so the server re-scans
 * and downloads in one run. The UI never sees a URL — it only ever triggers,
 * polls, and renders progress, exactly as §3.4 describes.
 */

export function useScanCharacterMedia(cardId: string) {
  return useMutation({
    mutationFn: () =>
      unwrap(
        apiClient.POST('/api/v1/characters/{card_id}/media/scan', {
          params: { path: { card_id: cardId } },
        }),
        'could not scan for media',
      ),
  })
}

export function useSubmitDiscoveredMedia(cardId: string) {
  return useMutation({
    mutationFn: () =>
      unwrap(
        apiClient.POST('/api/v1/media/jobs', {
          body: {
            scope: 'card',
            card_id: cardId,
            discover: true,
            items: [],
            // Batch-only; a single-card job always runs.
            mode: 'pending',
            prefix: 'localized_media',
            // A label recorded in the manifest run, not a filter: `discover`
            // scans embedded *and* lorebook surfaces and downloads both under
            // this one run (§3.4).
            phase: 'embedded',
          },
        }),
        'could not start the download',
      ),
  })
}

/** Polls until the job leaves `queued`/`running` — the same 400ms-ish cadence
 * the old `downloadViaServerRoute` used, via TanStack's own refetch loop
 * rather than a hand-rolled `setTimeout` chain. */
export function useMediaJobStatus(jobId: string | null) {
  return useQuery({
    queryKey: ['media-job', jobId],
    enabled: !!jobId,
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/media/jobs/{job_id}', {
          params: { path: { job_id: jobId! } },
        }),
        'could not check the download',
      ),
    refetchInterval: (query) => {
      const state = query.state.data?.state
      return state === 'queued' || state === 'running' ? 500 : false
    },
  })
}

/** The gallery pane's cache is stale in two ways a finished job can change: a
 * card that had no gallery folder yet now does (`['character', id]` carries
 * `gallery.exists`/`folder`), and the folder's own file listing. */
export function useInvalidateMediaCaches(cardId: string) {
  const client = useQueryClient()
  return () => {
    void client.invalidateQueries({ queryKey: ['character', cardId] })
    void client.invalidateQueries({ queryKey: ['gallery'] })
  }
}
