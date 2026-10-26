import { useEffect, useMemo, useState } from 'react'
import { Search } from 'lucide-react'
import { useAllCardTags } from '@/hooks/use-all-card-tags'
import { useSettings, useUpdateUi2 } from '@/hooks/use-settings'
import { useApplyTags } from '@/hooks/use-apply-tags'
import { toast } from '@/lib/toast'
import { buildApplyPayload, type Variant } from '@/lib/tags/tag-analysis'
import {
  dictSnapshot,
  dictionaryDelta,
  ensureDictionary,
  loadBaseDictionary,
  rebuildMapping,
} from '@/lib/tags/dictionary'
import {
  buildEditorState,
  bulkMove,
  computeStats,
  deleteGroup,
  moveVariant,
  newEmptyGroup,
  renameCanonical,
  type EditorGroup,
  type EditorState,
} from '@/lib/tags/tags-editor'
import type { Delta } from '@/lib/tags/tag-delta'
import { TagStats } from '@/components/tags/TagStats'
import { CategoryAccordion } from '@/components/tags/CategoryAccordion'
import { TagBucket } from '@/components/tags/TagBucket'
import type { GroupTarget } from '@/components/tags/ChipMoveMenu'
import type { MoveDest, MoveFrom } from '@/components/tags/move-types'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'

const UNCATEGORIZED = 'Custom'

/**
 * The tag consolidation editor (docs/UI_REWRITE_PLAN.md §3.5, Stage 4).
 *
 * Zero server work: `POST /tags/apply` and the whole-archive survey both already
 * exist. The dictionary, the bucket logic and the delta persistence are the
 * salvaged tag-tools code ported to TypeScript (`src/lib/tags/`). This page is
 * the mock's UI rebuilt on top of them: staged stats, category accordions,
 * Unassigned/Removed buckets, and an Apply that resolves the working dictionary
 * into a literal plan the server applies by string equality.
 */
export function TagsSection() {
  const cards = useAllCardTags()
  const settings = useSettings()
  const saveDelta = useUpdateUi2()
  const applyTags = useApplyTags()

  const [editor, setEditor] = useState<EditorState | null>(null)
  const [openCats, setOpenCats] = useState<Set<string>>(new Set())
  const [find, setFind] = useState('')
  const [applyOpen, setApplyOpen] = useState(false)
  const [resetOpen, setResetOpen] = useState(false)

  const base = loadBaseDictionary()
  const baseSnap = useMemo(
    () => dictSnapshot(base.mapping, base.removedTags),
    [base],
  )

  // Build the editor once both the archive survey and the stored delta are in.
  useEffect(() => {
    if (editor || !cards.data || !settings.isSuccess) return
    const delta = settings.data?.ui2?.tagDictionaryDelta as Delta | undefined
    const dict = ensureDictionary(delta)
    const state = buildEditorState(
      cards.data,
      dict.mapping,
      dict.removedTags,
      dict.canonicalCategories,
    )
    setEditor(state)
    // Open the categories that carry a staged change; leave the rest collapsed.
    const changed = new Set<string>()
    for (const g of state.groups) {
      if (g.variants.some((v) => v.count > 0 && v.tag !== g.canonical)) {
        changed.add(g.category || UNCATEGORIZED)
      }
    }
    setOpenCats(changed)
  }, [editor, cards.data, settings.isSuccess, settings.data])

  /** Apply a state transform and persist the resulting delta. */
  const commit = (next: EditorState) => {
    setEditor(next)
    const { mapping, removedTags } = rebuildMapping(next)
    saveDelta.mutate({
      key: 'tagDictionaryDelta',
      value: dictionaryDelta(mapping, removedTags),
    })
  }

  const groupById = (id: string) => editor?.groups.find((g) => g.id === id)

  const onMove = (variant: Variant, from: MoveFrom, dest: MoveDest) => {
    if (!editor) return
    const to =
      dest === 'new' || dest === 'unassigned' || dest === 'removed'
        ? dest
        : groupById(dest.groupId)
    if (!to) return
    commit(moveVariant(editor, variant, from, to))
  }

  const onBulkMove = (
    variants: Variant[],
    from: 'unassigned' | 'removed',
    dest: MoveDest,
  ) => {
    if (!editor || variants.length === 0) return
    const to =
      dest === 'new' || dest === 'unassigned' || dest === 'removed'
        ? dest
        : groupById(dest.groupId)
    if (!to) return
    commit(bulkMove(editor, variants, from, to))
  }

  const onRename = (id: string, name: string) => {
    if (!editor) return
    commit(renameCanonical(editor, id, name))
  }

  const onDelete = (id: string) => {
    if (!editor) return
    const { state, blocked } = deleteGroup(editor, id)
    if (blocked) {
      toast(
        `“${blocked.canonical}” holds a core match rule and can't be deleted.`,
        'bad',
      )
      return
    }
    commit(state)
  }

  const onNewCanonical = () => {
    if (!editor) return
    commit(newEmptyGroup(editor))
    setOpenCats((c) => new Set(c).add(UNCATEGORIZED))
  }

  // Derived, memoised on the two inputs that change it — not on `find`, which
  // only filters what is already computed.
  const derived = useMemo(() => {
    if (!editor || !cards.data) return null
    const { mapping, removedTags } = rebuildMapping(editor)
    const plan = buildApplyPayload(cards.data, mapping, removedTags)
    const stats = computeStats(cards.data, plan)
    return { plan, stats }
  }, [editor, cards.data])

  const targets: GroupTarget[] = useMemo(
    () =>
      (editor?.groups ?? []).map((g) => ({ id: g.id, canonical: g.canonical })),
    [editor],
  )

  const isDirty = useMemo(() => {
    if (!editor) return false
    const { mapping, removedTags } = rebuildMapping(editor)
    return dictSnapshot(mapping, removedTags) !== baseSnap
  }, [editor, baseSnap])
  const planEmpty =
    !derived ||
    (Object.keys(derived.plan.rename).length === 0 &&
      derived.plan.remove.length === 0)

  const onReset = () => {
    if (!cards.data) return
    const dict = loadBaseDictionary()
    commit(
      buildEditorState(
        cards.data,
        dict.mapping,
        dict.removedTags,
        dict.canonicalCategories,
        editor?.seq ?? 0,
      ),
    )
    setResetOpen(false)
    toast('Dictionary reset to the shipped default.')
  }

  const onApply = async () => {
    if (!editor || !derived) return
    setApplyOpen(false)
    try {
      const result = await applyTags.mutateAsync(derived.plan)
      const failed = Object.keys(result.failed).length
      // Rebuild from the SAME working dictionary against the rewritten cards so
      // counts and buckets reflect what is now on disk, without re-reading the
      // dictionary or losing scroll/section state.
      const fresh = await cards.refetch()
      if (fresh.data) {
        const { mapping, removedTags } = rebuildMapping(editor)
        setEditor(
          buildEditorState(
            fresh.data,
            mapping,
            removedTags,
            loadBaseDictionary().canonicalCategories,
            editor.seq,
          ),
        )
      }
      toast(
        failed === 0
          ? `Tag consolidation applied: ${result.changed} card(s) changed`
          : `Applied with errors: ${result.changed} changed, ${failed} failed`,
        failed === 0 ? 'ok' : 'bad',
      )
    } catch (err) {
      toast(
        err instanceof Error ? err.message : 'Failed to apply the tag plan.',
        'bad',
      )
    }
  }

  const categories = useMemo(() => {
    if (!editor) return []
    return groupByCategory(
      editor.groups,
      base.categoryOrder,
      find.trim().toLowerCase(),
    )
  }, [editor, base.categoryOrder, find])

  return (
    <>
      <div className="sticky top-0 z-20 border-b border-line-soft bg-ground">
        <div className="mx-auto flex max-w-[1320px] flex-wrap items-center gap-3 px-5 py-3">
          <h1 className="text-[19px] font-semibold tracking-[-0.015em] whitespace-nowrap">
            Tags
          </h1>
          <span className="text-[12.5px] whitespace-nowrap text-faint">
            consolidate the vocabulary · edits are staged until you apply
          </span>
          <div className="flex min-w-[190px] items-center gap-2 rounded-full px-2">
            <Search className="size-3.5 text-faint" />
            <input
              value={find}
              onChange={(e) => setFind(e.target.value)}
              placeholder="Find a tag or variant…"
              className="w-full bg-transparent py-1 text-[13px] outline-none placeholder:text-faint"
            />
          </div>
          <div className="flex-1" />
          <button
            type="button"
            onClick={onNewCanonical}
            disabled={!editor}
            className="h-[33px] rounded-full border border-line px-3.5 text-[13px] hover:bg-raised disabled:opacity-50"
          >
            ＋ New canonical
          </button>
          <button
            type="button"
            onClick={() => setResetOpen(true)}
            disabled={!isDirty}
            className="h-[33px] rounded-full border border-line px-3.5 text-[13px] text-muted hover:bg-raised disabled:opacity-40"
          >
            ↺ Reset
          </button>
          <button
            type="button"
            onClick={() => setApplyOpen(true)}
            disabled={planEmpty || applyTags.isPending}
            className="h-[33px] rounded-full bg-sage px-4 text-[13px] font-semibold text-on-sage hover:brightness-110 disabled:opacity-40"
          >
            {applyTags.isPending ? 'Applying…' : 'Apply tags'}
          </button>
        </div>
      </div>

      <div className="mx-auto max-w-[1320px] px-5 pb-24">
        {(cards.isPending || (settings.isPending && !editor)) && (
          <p className="py-16 text-center text-faint">
            Loading the tag dictionary and scanning the archive…
          </p>
        )}
        {cards.error && (
          <p className="py-16 text-center text-bad">{cards.error.message}</p>
        )}
        {settings.error && !editor && (
          <p className="py-16 text-center text-bad">
            Could not read settings: {settings.error.message}
          </p>
        )}

        {editor && derived && (
          <>
            <TagStats
              stats={derived.stats}
              unassigned={editor.unassigned.length}
              removed={editor.removed.filter((v) => v.count > 0).length}
            />

            {categories.length === 0 && find.trim() && (
              <p className="py-10 text-center text-faint">
                No canonical or variant matches “{find.trim()}”.
              </p>
            )}

            {categories.map(({ category, groups }) => (
              <CategoryAccordion
                key={category}
                category={category}
                groups={groups}
                targets={targets}
                open={openCats.has(category) || find.trim().length > 0}
                onToggle={() =>
                  setOpenCats((c) => {
                    const next = new Set(c)
                    if (!next.delete(category)) next.add(category)
                    return next
                  })
                }
                onRename={onRename}
                onMove={onMove}
                onDelete={onDelete}
              />
            ))}

            <TagBucket
              kind="unassigned"
              variants={editor.unassigned}
              targets={targets}
              onMove={onMove}
              onBulkMove={onBulkMove}
            />
            <TagBucket
              kind="removed"
              variants={editor.removed}
              targets={targets}
              onMove={onMove}
              onBulkMove={onBulkMove}
            />
          </>
        )}
      </div>

      <AlertDialog open={applyOpen} onOpenChange={setApplyOpen}>
        <AlertDialogContent>
          <AlertDialogTitle>Apply tag consolidation?</AlertDialogTitle>
          <AlertDialogDescription>
            {derived && (
              <>
                This renames {derived.stats.renames} tag spelling(s) and removes{' '}
                {derived.stats.removals}, touching{' '}
                {derived.stats.affectedCards.toLocaleString()} card(s) on disk.
                This cannot be undone from here.
              </>
            )}
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
                onClick={onApply}
                className="rounded-[10px] bg-sage px-3.5 py-2 text-[13px] font-semibold text-on-sage hover:brightness-110"
              >
                Apply
              </button>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={resetOpen} onOpenChange={setResetOpen}>
        <AlertDialogContent>
          <AlertDialogTitle>Reset to the shipped mapping?</AlertDialogTitle>
          <AlertDialogDescription>
            This discards all your edits and restores the shipped default
            mapping. It does not change any cards.
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
                onClick={onReset}
                className="rounded-[10px] border border-bad bg-bad/90 px-3.5 py-2 text-[13px] font-semibold text-white hover:bg-bad"
              >
                Reset Tags
              </button>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

interface RenderedCategory {
  category: string
  groups: EditorGroup[]
}

/**
 * Group the editor's canonicals into their categories, ordered by the shipped
 * dictionary order with anything uncategorised last, and drop groups (and then
 * empty categories) that no longer match the find query.
 */
function groupByCategory(
  groups: EditorGroup[],
  categoryOrder: string[],
  query: string,
): RenderedCategory[] {
  const matches = (g: EditorGroup) =>
    !query ||
    g.canonical.toLowerCase().includes(query) ||
    g.variants.some((v) => v.count > 0 && v.tag.toLowerCase().includes(query))

  const byCat = new Map<string, EditorGroup[]>()
  for (const g of groups) {
    if (!matches(g)) continue
    const cat = g.category || UNCATEGORIZED
    if (!byCat.has(cat)) byCat.set(cat, [])
    byCat.get(cat)!.push(g)
  }

  const ordered = [
    ...categoryOrder.filter((c) => byCat.has(c)),
    ...[...byCat.keys()].filter((c) => !categoryOrder.includes(c)),
  ]
  return ordered.map((category) => ({ category, groups: byCat.get(category)! }))
}
