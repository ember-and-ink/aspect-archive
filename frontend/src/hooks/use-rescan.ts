import { useMutation, useQueryClient } from '@tanstack/react-query'
import { apiClient, unwrap } from '@/lib/api-client'
import { invalidateArchive } from './use-card-mutations'

/**
 * Rescan the archive directory (`POST /refresh`) and refetch everything that
 * reads from the index.
 *
 * The server never rescans on its own -- it builds the index at startup and
 * every write route refreshes it -- so this is the one door for a change made
 * outside the app: a card dropped in by hand, a `make` script. Shared by the
 * top bar's button and Settings › Maintenance so both refetch the same set.
 */
export function useRescan() {
  const qc = useQueryClient()
  return useMutation({
    mutationKey: ['rescan'],
    mutationFn: () =>
      unwrap(apiClient.POST('/api/v1/refresh'), 'could not rescan the archive'),
    onSuccess: () => invalidateArchive(qc),
  })
}
