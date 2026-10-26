import { useMutation, useQueryClient } from '@tanstack/react-query'
import { apiClient, unwrap } from '@/lib/api-client'
import type { ApplyPayload } from '@/lib/tags/tag-analysis'

/**
 * Apply a literal `{rename, remove}` plan across the whole archive
 * (POST /api/v1/tags/apply). The server makes no matching decisions of its own —
 * the plan is resolved client-side by the salvaged tag logic, so what the editor
 * previewed is what lands on disk.
 *
 * On success everything the rewrite could have touched is invalidated: the tag
 * survey behind this page, the grid, the facet catalogue and the stats.
 */
export function useApplyTags() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (plan: ApplyPayload) =>
      unwrap(
        apiClient.POST('/api/v1/tags/apply', { body: plan }),
        'could not apply the tag plan',
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['all-card-tags'] })
      void qc.invalidateQueries({ queryKey: ['characters'] })
      void qc.invalidateQueries({ queryKey: ['facets'] })
      void qc.invalidateQueries({ queryKey: ['stats'] })
      void qc.invalidateQueries({ queryKey: ['count'] })
    },
  })
}
