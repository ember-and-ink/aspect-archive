import { useState } from 'react'
import { Link } from 'react-router'
import { ChevronRight, RefreshCw, Trash2 } from 'lucide-react'
import {
  type DuplicateGroup,
  type DuplicateMember,
  useDeleteAnyCard,
  useDuplicateGroups,
} from '@/hooks/use-duplicates'
import { useSettings, useUpdateUi2 } from '@/hooks/use-settings'
import { toast } from '@/lib/toast'
import { cn } from '@/lib/utils'
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
 * Same-creator duplicate review (docs/plans/ticklish-moseying-shore.md).
 *
 * Detection is entirely server-side (`GET /api/v1/duplicates`, backed by
 * `proxy.cards.dupes`) and scoped hard to one creator at a time -- this page
 * never sees, and could not accidentally render, a cross-creator match. What
 * it owns is review: showing *why* a group was flagged, letting a card be
 * discarded (the existing reversible bin, `DELETE /characters/{id}`), or the
 * whole group dismissed as not-a-duplicate. A dismissal is a client-side
 * decision -- it persists to `ui2.duplicatesDismissed` via the same
 * read-modify-write `useUpdateUi2` the tag dictionary delta already uses, not
 * a new server concept.
 */
export function DuplicatesSection() {
  const scan = useDuplicateGroups()
  const settings = useSettings()
  const saveDismissed = useUpdateUi2()
  const deleteCard = useDeleteAnyCard()

  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set())
  const [showDismissed, setShowDismissed] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<{
    id: string
    name: string
  } | null>(null)

  const dismissed = new Set(
    (settings.data?.ui2?.duplicatesDismissed as string[] | undefined) ?? [],
  )

  const toggleGroup = (id: string) =>
    setOpenGroups((current) => {
      const next = new Set(current)
      if (!next.delete(id)) next.add(id)
      return next
    })

  const setDismissed = (next: Set<string>) =>
    saveDismissed.mutate({ key: 'duplicatesDismissed', value: [...next] })

  const dismissGroup = (group: DuplicateGroup) => {
    setDismissed(new Set(dismissed).add(group.group_id))
    toast(`Dismissed the “${group.creator}” group.`)
  }

  const restoreGroup = (group: DuplicateGroup) => {
    const next = new Set(dismissed)
    next.delete(group.group_id)
    setDismissed(next)
  }

  const onConfirmDelete = () => {
    if (!deleteTarget) return
    const { id, name } = deleteTarget
    setDeleteTarget(null)
    deleteCard.mutate(
      { id, gallery: 'keep' },
      {
        onSuccess: () => toast(`Moved “${name}” to the bin.`),
        onError: (error) => toast(error.message, 'bad'),
      },
    )
  }

  const groups = scan.data?.groups ?? []
  const active = groups.filter((g) => !dismissed.has(g.group_id))
  const dismissedGroups = groups.filter((g) => dismissed.has(g.group_id))

  return (
    <>
      <div className="sticky top-0 z-20 border-b border-line-soft bg-ground">
        <div className="mx-auto flex max-w-[1320px] flex-wrap items-center gap-3 px-5 py-3">
          <h1 className="text-[19px] font-semibold tracking-[-0.015em] whitespace-nowrap">
            Duplicates
          </h1>
          <span className="text-[12.5px] whitespace-nowrap text-faint">
            same creator only · a bounded heuristic, review before discarding
          </span>
          <div className="flex-1" />
          {scan.data && (
            <span className="text-[12.5px] text-faint">
              {active.length} group{active.length === 1 ? '' : 's'} ·{' '}
              {scan.data.scanned.toLocaleString()} cards scanned
            </span>
          )}
          <button
            type="button"
            onClick={() => void scan.refetch()}
            disabled={scan.isFetching}
            className="flex h-[33px] items-center gap-1.5 rounded-full border border-line px-3.5 text-[13px] hover:bg-raised disabled:opacity-50"
          >
            <RefreshCw
              className={cn('size-3.5', scan.isFetching && 'animate-spin')}
            />
            {scan.isFetching ? 'Scanning…' : 'Rescan'}
          </button>
        </div>
      </div>

      <div className="mx-auto max-w-[1320px] px-5 pb-24">
        {scan.isPending && (
          <p className="py-16 text-center text-faint">
            Scanning the archive for same-creator matches…
          </p>
        )}
        {scan.error && (
          <p className="py-16 text-center text-bad">{scan.error.message}</p>
        )}
        {scan.data && active.length === 0 && (
          <p className="py-16 text-center text-faint">
            No candidate duplicates found.
          </p>
        )}

        {active.map((group) => (
          <DuplicateGroupAccordion
            key={group.group_id}
            group={group}
            open={openGroups.has(group.group_id)}
            onToggle={() => toggleGroup(group.group_id)}
            onDismiss={() => dismissGroup(group)}
            onDelete={(member) =>
              setDeleteTarget({ id: member.id, name: member.name })
            }
          />
        ))}

        {dismissedGroups.length > 0 && (
          <div className="mt-4">
            <button
              type="button"
              onClick={() => setShowDismissed((v) => !v)}
              className="text-[12.5px] text-faint hover:text-text"
            >
              {showDismissed ? 'Hide' : 'Show'} {dismissedGroups.length}{' '}
              dismissed group
              {dismissedGroups.length === 1 ? '' : 's'}
            </button>
            {showDismissed &&
              dismissedGroups.map((group) => (
                <div
                  key={group.group_id}
                  className="mt-2.5 flex items-center gap-3 rounded-[15px] border border-line-soft bg-surface px-5 py-3"
                >
                  <span className="text-[13.5px] text-faint">
                    {group.creator} — {group.members.length} cards
                  </span>
                  <button
                    type="button"
                    onClick={() => restoreGroup(group)}
                    className="ml-auto rounded-full border border-line px-3 py-1 text-[12.5px] hover:bg-raised"
                  >
                    Restore
                  </button>
                </div>
              ))}
          </div>
        )}
      </div>

      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogTitle>Delete “{deleteTarget?.name}”?</AlertDialogTitle>
          <AlertDialogDescription>
            The card is moved to the bin, not erased — it can be recovered from{' '}
            <span className="font-mono">data/.trash/</span> until the bin is
            emptied.
          </AlertDialogDescription>
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
                onClick={onConfirmDelete}
                disabled={deleteCard.isPending}
                className="rounded-[10px] border border-bad bg-bad/90 px-3.5 py-2 text-[13px] font-semibold text-white hover:bg-bad disabled:opacity-60"
              >
                {deleteCard.isPending ? 'Deleting…' : 'Delete'}
              </button>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

/** The name shared by the most members of a group, and how many distinct
 *  names appear at all -- in practice a group is almost always one name
 *  worn by every member (that's how most groups formed in the first place),
 *  but this stays honest about it rather than silently picking one when a
 *  group matched on evidence other than the name. */
function dominantName(members: DuplicateMember[]): {
  name: string
  variants: number
} {
  const counts = new Map<string, number>()
  for (const member of members)
    counts.set(member.name, (counts.get(member.name) ?? 0) + 1)
  const [name] = [...counts.entries()].sort((a, b) => b[1] - a[1])[0] ?? ['']
  return { name, variants: counts.size }
}

function DuplicateGroupAccordion({
  group,
  open,
  onToggle,
  onDismiss,
  onDelete,
}: {
  group: DuplicateGroup
  open: boolean
  onToggle: () => void
  onDismiss: () => void
  onDelete: (member: DuplicateMember) => void
}) {
  const strong = group.pairs.some((p) => p.strength === 'strong')
  const reasons = [...new Set(group.pairs.flatMap((p) => p.reasons))]
  const { name: commonName, variants } = dominantName(group.members)

  return (
    <section className="mb-2.5 overflow-hidden rounded-[15px] border border-line bg-surface">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2.5 px-5 py-3.5 text-left hover:bg-raised"
      >
        <ChevronRight
          className={cn(
            'size-3.5 shrink-0 text-faint transition-transform',
            open && 'rotate-90',
          )}
        />
        <h3 className="truncate text-[14px] font-semibold">
          <span className="text-sage">{group.creator}</span>
          <span className="text-faint"> — </span>
          {commonName}
          {variants > 1 && (
            <span className="ml-1.5 text-[11px] font-normal text-faint">
              +{variants - 1} name variant{variants - 1 === 1 ? '' : 's'}
            </span>
          )}
        </h3>
        <span
          className={cn(
            'shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium',
            strong ? 'border-sage-line text-sage' : 'border-line text-faint',
          )}
        >
          {strong ? 'Strong match' : 'Name match only'}
        </span>
        <span className="ml-auto shrink-0 text-[12px] text-faint">
          {group.members.length} cards
        </span>
        <span
          role="button"
          tabIndex={0}
          onClick={(e) => {
            e.stopPropagation()
            onDismiss()
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.stopPropagation()
              onDismiss()
            }
          }}
          className="shrink-0 rounded-full border border-line px-2.5 py-1 text-[11.5px] text-faint hover:bg-ground hover:text-text"
        >
          Not a duplicate
        </span>
      </button>

      {open && (
        <div className="px-5 pb-4">
          {reasons.length > 0 && (
            <p className="mb-3 text-[12px] text-faint">
              Why grouped: {reasons.join(' · ')}
            </p>
          )}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
            {group.members.map((member) => (
              <DuplicateMemberTile
                key={member.id}
                member={member}
                onDelete={() => onDelete(member)}
              />
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

function DuplicateMemberTile({
  member,
  onDelete,
}: {
  member: DuplicateMember
  onDelete: () => void
}) {
  return (
    <div className="rounded-xl border border-line-soft bg-raised p-2">
      <Link
        to={`/characters/${encodeURIComponent(member.id)}`}
        className="block"
      >
        <div className="aspect-[2/3] w-full overflow-hidden rounded-lg bg-ground">
          <img
            src={`${member.thumb_url}?size=280`}
            alt=""
            loading="lazy"
            className="size-full object-cover"
          />
        </div>
        <h4 className="mt-1.5 line-clamp-1 text-[13px] font-medium">
          {member.name}
        </h4>
        <p className="line-clamp-1 text-[11px] text-faint">
          {member.page_name || '—'}
        </p>
        <p className="text-[11px] text-faint">
          {member.description_chars.toLocaleString()} chars
        </p>
      </Link>
      <button
        type="button"
        onClick={onDelete}
        className="mt-1.5 flex w-full items-center justify-center gap-1.5 rounded-[8px] py-1.5 text-[11.5px] text-faint hover:bg-ground hover:text-bad"
      >
        <Trash2 className="size-3.5" /> Discard
      </button>
    </div>
  )
}
