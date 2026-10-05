import { useState } from 'react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import {
  type SyncPlanCard,
  type SyncResult,
  useLorebookActions,
  useSyncPlan,
} from '@/hooks/use-lorebooks'
import { toast } from '@/lib/toast'
import { ActionButton } from './parts'

const plural = (n: number, one: string, many = `${one}s`) =>
  `${n.toLocaleString()} ${n === 1 ? one : many}`

/** What one card's sync amounts to, as a phrase. */
function describeSync(card: SyncPlanCard) {
  const parts: string[] = []
  if (card.upgrades > 0)
    parts.push(`${plural(card.upgrades, 'entry', 'entries')} updated`)
  if (card.duplicates > 0)
    parts.push(`${plural(card.duplicates, 'stale duplicate')} removed`)
  if (card.attach.length > 0) parts.push(`attach ${card.attach.join(', ')}`)
  if (card.detach.length > 0) parts.push(`detach ${card.detach.join(', ')}`)
  return parts.join(' · ')
}

function describeResult(result: SyncResult) {
  const parts = [`Rewrote ${plural(result.synced, 'card')}`]
  if (result.upgraded > 0)
    parts.push(`${plural(result.upgraded, 'entry', 'entries')} updated`)
  if (result.removed > 0)
    parts.push(`${plural(result.removed, 'stale duplicate')} removed`)
  if (result.attached > 0) parts.push(`${result.attached} attached`)
  if (result.detached > 0) parts.push(`${result.detached} detached`)
  if (result.failed.length > 0) parts.push(`${result.failed.length} failed`)
  return `${parts.join(' · ')}.`
}

/**
 * The Sync button and its confirmation.
 *
 * Sync is the one lorebook action that rewrites cards, so it never fires from
 * a single click: the dialog lists every card it would touch and what would
 * change on each, read fresh from the server when it opens. `lorebook` scopes
 * it to one lorebook's changes.
 */
export function SyncButton({
  count,
  lorebook = '',
}: {
  count: number
  lorebook?: string
}) {
  const [open, setOpen] = useState(false)
  const plan = useSyncPlan(lorebook, open)
  const { sync } = useLorebookActions()
  const cards = plan.data?.cards ?? []

  const run = () =>
    sync.mutate(
      { lorebook },
      {
        onError: (error) => toast(error.message, 'bad'),
        onSuccess: (result) => {
          toast(describeResult(result), result.failed.length ? 'bad' : 'ok')
          for (const failure of result.failed.slice(0, 3))
            toast(`${failure.name}: ${failure.error}`, 'bad')
        },
      },
    )

  if (count === 0) return null
  return (
    <>
      <ActionButton
        tone="sage"
        disabled={sync.isPending}
        onClick={() => setOpen(true)}
      >
        {sync.isPending ? 'Syncing…' : `Sync ${plural(count, 'card')}…`}
      </ActionButton>
      <AlertDialog open={open} onOpenChange={setOpen}>
        <AlertDialogContent className="w-[min(560px,calc(100vw-32px))]">
          <AlertDialogTitle>
            {plan.data
              ? `Rewrite ${plural(cards.length, 'card')}?`
              : 'Sync lorebooks'}
          </AlertDialogTitle>
          <AlertDialogDescription>
            Each card’s embedded lorebook is overwritten in place. The lorebook
            a card has now is saved the first time it is synced, and can be put
            back from the card’s Lorebook tab.
          </AlertDialogDescription>
          {plan.isPending && (
            <p className="mt-4 text-[13px] text-faint">Working it out…</p>
          )}
          {plan.error && (
            <p className="mt-4 text-[13px] text-bad">{plan.error.message}</p>
          )}
          {plan.data && cards.length === 0 && (
            <p className="mt-4 text-[13px] text-faint">
              Nothing to sync — every card already matches.
            </p>
          )}
          {cards.length > 0 && (
            <ul className="mt-4 max-h-[46vh] overflow-y-auto rounded-xl border border-line-soft">
              {cards.map((row) => (
                <li
                  key={row.card.id}
                  className="flex items-center gap-2.5 border-b border-line-soft px-3 py-2 text-[13px] last:border-b-0"
                >
                  <img
                    src={row.card.thumb_url}
                    alt=""
                    loading="lazy"
                    className="size-6 flex-none rounded-full object-cover object-top"
                  />
                  <span className="max-w-[180px] flex-none truncate">
                    {row.card.name}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-right text-[12.5px] text-muted">
                    {describeSync(row)}
                  </span>
                </li>
              ))}
            </ul>
          )}
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
                disabled={cards.length === 0}
                onClick={run}
                className="rounded-[10px] border border-sage-line bg-sage-dim px-3.5 py-2 text-[13px] font-semibold text-sage hover:bg-sage/25 disabled:opacity-50"
              >
                Rewrite {plural(cards.length, 'card')}
              </button>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
