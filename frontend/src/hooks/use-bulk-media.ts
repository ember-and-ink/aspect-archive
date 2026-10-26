import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiClient, unwrap } from '@/lib/api-client'

/**
 * Bulk localize (docs/UI_REWRITE_PLAN.md Stage 6B C1).
 *
 * The old UI ran this as a browser loop over every card, which meant closing
 * the tab abandoned the run. It is now one server-side job: the browser submits
 * and then only polls, so the run survives the tab, the laptop lid, and a
 * reload. What it does *not* survive is a server restart -- job state is a live
 * progress cache, and the manifests on disk are the real record. Re-submitting
 * after a restart is cheap precisely because `pending` mode passes over
 * everything that already finished.
 *
 * `everything` and `broken` are the rare rescans: the first re-resolves every
 * album and retries every dead link, the second retries only dead links and
 * failed albums, on just the cards that have them.
 */
export type BulkMode = 'pending' | 'everything' | 'broken'

export function useBulkLocalize() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (opts: { mode: BulkMode }) =>
      unwrap(
        apiClient.POST('/api/v1/media/jobs', {
          body: {
            scope: 'all',
            discover: true,
            mode: opts.mode,
            prefix: 'localized_media',
            phase: 'embedded',
          },
        }),
        'could not start the media run',
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['media-jobs'] })
    },
  })
}

/**
 * Poll one job. Stops polling the moment it settles, and asks for no events --
 * a whole-archive run can emit tens of thousands, and this UI shows counters,
 * not a log. (`events_dropped` on the payload is how a client that *did* want
 * them would learn it missed some.)
 */
export function useMediaJob(jobId: string | null) {
  return useQuery({
    queryKey: ['media-jobs', jobId],
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const state = query.state.data?.state
      return state === 'queued' || state === 'running' ? 1000 : false
    },
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/media/jobs/{job_id}', {
          params: { path: { job_id: jobId as string }, query: { after: 1e9 } },
        }),
        'could not read the media run',
      ),
  })
}

export function useCancelMediaJob() {
  return useMutation({
    mutationFn: (jobId: string) =>
      unwrap(
        apiClient.POST('/api/v1/media/jobs/{job_id}/cancel', {
          params: { path: { job_id: jobId } },
        }),
        'could not cancel the run',
      ),
  })
}

/** Any archive-wide run already going, so reopening Settings rejoins it rather
 *  than offering to start a second one. */
export function useActiveBulkJob() {
  return useQuery({
    queryKey: ['media-jobs', 'active'],
    refetchInterval: 5000,
    queryFn: async () => {
      const jobs = await unwrap(
        apiClient.GET('/api/v1/media/jobs', {
          params: { query: { active: true } },
        }),
        'could not list media runs',
      )
      return jobs.find((job) => job.scope === 'all') ?? null
    },
  })
}
