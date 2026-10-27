import { useMutation, useQueryClient } from '@tanstack/react-query'
import { apiClient } from '@/lib/api-client'

/**
 * Bin many files out of one gallery folder in one request -- the Gallery
 * pane's batch-select delete.
 *
 * Deliberately does *not* touch the folder's `.media.json` manifest (that's
 * server-side, `aspect/api/v1/galleries.py`'s `bulk_delete_files`): a binned
 * file keeps its manifest entry claiming the source URL as downloaded, so a
 * plain "Localize media" pass still treats the card as complete and does not
 * go re-fetch it. Only a full rescan re-derives the URL list from scratch and
 * notices the file is gone.
 */
export function useBulkDeleteGalleryFiles(folder: string | undefined) {
  const client = useQueryClient()
  return useMutation<
    { deleted: string[]; failed: Record<string, string> },
    Error,
    string[]
  >({
    mutationFn: async (names) => {
      if (!folder) throw new Error('this card has no gallery folder')
      const { data, response } = await apiClient.POST(
        '/api/v1/galleries/{folder}/files/bulk-delete',
        { params: { path: { folder } }, body: { names } },
      )
      if (!response.ok || data === undefined)
        throw new Error(
          `could not delete the files: ${response.status} ${response.statusText}`.trim(),
        )
      return data
    },
    onSuccess: () =>
      void client.invalidateQueries({ queryKey: ['gallery', folder] }),
  })
}
