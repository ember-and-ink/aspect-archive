import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiClient, unwrap } from '@/lib/api-client'
import type { components } from '@/lib/api-schema'
import { invalidateArchive } from './use-card-mutations'

export type DuplicateGroup = components['schemas']['DuplicateGroupOut']
export type DuplicateMember = components['schemas']['DuplicateMemberOut']
export type DuplicatePair = components['schemas']['DuplicatePairOut']

/** A full-archive duplicate scan. Server-computed in one request -- the
 *  detection itself (avatar hashing, prose comparison) runs in
 *  `aspect.cards.dupes`, never client-side, so this is a plain GET rather than
 *  the N-per-card detail fan-out a client-side scan would need. */
export function useDuplicateGroups() {
  return useQuery({
    queryKey: ['duplicate-groups'],
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/duplicates'),
        'could not scan for duplicates',
      ),
  })
}

/**
 * Bin any one card by id. Unlike `useDeleteCharacter` (bound to a single card
 * at hook-creation time, for the detail page's own delete button), this takes
 * the id as the mutation argument -- one hook instance serves every card in
 * every group on the Duplicates page.
 */
export function useDeleteAnyCard() {
  const client = useQueryClient()
  return useMutation<void, Error, { id: string; gallery: 'keep' | 'delete' }>({
    mutationFn: async ({ id, gallery }) => {
      const { response } = await apiClient.DELETE(
        '/api/v1/characters/{card_id}',
        { params: { path: { card_id: id }, query: { gallery } } },
      )
      if (!response.ok)
        throw new Error(
          `could not delete the card: ${response.status} ${response.statusText}`.trim(),
        )
    },
    onSuccess: (_data, { id }) => {
      client.removeQueries({ queryKey: ['character', id] })
      invalidateArchive(client)
      void client.invalidateQueries({ queryKey: ['duplicate-groups'] })
    },
  })
}
