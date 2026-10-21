import { useState } from 'react'
import { Trash2 } from 'lucide-react'
import { BatchBarShell } from '@/components/BatchBarShell'
import { useBatchSelection } from '@/hooks/use-batch-selection'
import { useBulkDeleteCharacters } from '@/hooks/use-card-mutations'
import { toast } from '@/lib/toast'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'

/**
 * Batch mode's actions over the *character* grid — Delete all. Rendered once
 * from `AppShell` (like `BackToTop`) rather than from `CharactersPage`, since
 * it has to outlive whichever page turned batch mode on; `useBatchSelection`
 * already confines `active` to the grid routes.
 */
export function BatchActionBar() {
  const { active, selected, clear } = useBatchSelection()
  const bulkDelete = useBulkDeleteCharacters()
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [alsoGallery, setAlsoGallery] = useState(false)

  if (!active) return null

  const count = selected.size
  const plural = count === 1 ? '' : 's'

  const onDelete = () =>
    bulkDelete.mutate(
      { ids: [...selected], gallery: alsoGallery ? 'delete' : 'keep' },
      {
        onSuccess: (result) => {
          clear()
          const failedCount = Object.keys(result.failed).length
          if (failedCount)
            toast(
              `Deleted ${result.deleted.length}, ${failedCount} failed.`,
              'bad',
            )
          else
            toast(
              `Moved ${result.deleted.length} card${result.deleted.length === 1 ? '' : 's'} to the bin.`,
            )
        },
        onError: (error) => toast(error.message, 'bad'),
      },
    )

  return (
    <>
      <BatchBarShell>
        <button
          type="button"
          onClick={() => {
            setAlsoGallery(false)
            setConfirmOpen(true)
          }}
          disabled={count === 0}
          className="flex h-[34px] items-center gap-2 rounded-[10px] border border-bad bg-bad/90 px-3.5 text-[13px] font-semibold text-white hover:bg-bad disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Trash2 className="size-3.5" /> Delete all
        </button>
      </BatchBarShell>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogTitle>
            Delete {count} card{plural}?
          </AlertDialogTitle>
          <AlertDialogDescription>
            Cards are moved to the bin, not erased — they can be recovered from{' '}
            <span className="font-mono">data/.trash/</span> until the bin is
            emptied.
          </AlertDialogDescription>
          <label className="mt-3.5 flex items-center gap-2.5 rounded-lg border border-line-soft bg-raised px-3 py-2.5 text-[13px]">
            <input
              type="checkbox"
              checked={alsoGallery}
              onChange={(e) => setAlsoGallery(e.target.checked)}
              className="size-4 accent-[var(--sage)]"
            />
            Delete their gallery folders too
          </label>
          <AlertDialogFooter>
            <AlertDialogCancel asChild>
              <button
                type="button"
                className="rounded-[10px] border border-line px-3.5 py-2 text-[13px] hover:bg-raised"
              >
                Cancel
              </button>
            </AlertDialogCancel>
            <AlertDialogAction asChild>
              <button
                type="button"
                onClick={onDelete}
                disabled={bulkDelete.isPending}
                className="rounded-[10px] border border-bad bg-bad/90 px-3.5 py-2 text-[13px] font-semibold text-white hover:bg-bad disabled:opacity-60"
              >
                {bulkDelete.isPending ? 'Deleting…' : 'Delete'}
              </button>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
