import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Dialog } from 'radix-ui'
import { Search, X } from 'lucide-react'
import { apiClient, unwrap } from '@/lib/api-client'
import { useDebounced } from '@/hooks/use-debounced'
import type { Scope } from '@/lib/browse'
import { cn } from '@/lib/utils'
import { CardGrid } from './CardGrid'
import { CardTile } from './CardTile'

/**
 * The scope chips.
 *
 * The mock had a fifth, Description, and it is cut: the index deliberately
 * holds no prose, so answering it would mean either keeping 40 MB of
 * descriptions resident or reading 3,839 PNGs per keystroke
 * (docs/UI_REWRITE_PLAN.md §3.9).
 */
const SCOPES: { value: Scope; label: string }[] = [
  { value: 'all', label: 'All fields' },
  { value: 'name', label: 'Name' },
  { value: 'creator', label: 'Creator' },
  { value: 'tags', label: 'Tags' },
]

/** How many results the overlay shows. It is a jump-to, not a browse surface. */
const LIMIT = 60

export function SearchOverlay({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [text, setText] = useState('')
  const [scope, setScope] = useState<Scope>('all')
  const query = useDebounced(text.trim())

  // Each opening starts fresh. Reopening onto the previous query's results
  // reads as a stale page rather than as a search.
  useEffect(() => {
    if (!open) return
    setText('')
    setScope('all')
  }, [open])

  const results = useQuery({
    queryKey: ['search', query, scope],
    enabled: open,
    queryFn: () =>
      unwrap(
        apiClient.GET('/api/v1/characters', {
          params: {
            query: {
              ...(query ? { q: query, scope } : { sort: '-added' }),
              limit: LIMIT,
            },
          },
        }),
        'could not search the archive',
      ),
  })

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-70 bg-[#0a0c0d]/80" />
        <Dialog.Content
          aria-describedby={undefined}
          className="fixed inset-0 z-70 flex flex-col px-5 pt-16 pb-5 focus:outline-none"
        >
          <Dialog.Title className="sr-only">Search the archive</Dialog.Title>

          <div className="mx-auto flex w-full max-w-[1560px] items-center gap-3.5 border-b border-line pb-3.5">
            <Search className="size-6 flex-none text-sage" strokeWidth={1.8} />
            <input
              autoFocus
              autoComplete="off"
              // Distinct from the dialog's own title, which is also "Search
              // the archive" -- two things with one accessible name is
              // ambiguous to a screen reader as much as to a test.
              aria-label="Search query"
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="Search names, creators, tags…"
              className="flex-1 border-0 bg-transparent font-serif text-[32px] focus:outline-none"
            />
            <Dialog.Close className="grid size-[35px] place-items-center rounded-full border border-white/7 text-muted-foreground hover:text-text">
              <X className="size-4" />
            </Dialog.Close>
          </div>

          <div className="mx-auto flex w-full max-w-[1560px] gap-[7px] pt-3.5">
            {SCOPES.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => setScope(option.value)}
                className={cn(
                  'inline-flex h-8 items-center rounded-[9px] border border-line px-[11px] text-[13px] text-muted-foreground hover:border-[#3a4145] hover:bg-raised hover:text-text',
                  scope === option.value &&
                    'border-sage-line bg-sage-dim text-sage',
                )}
              >
                {option.label}
              </button>
            ))}
            <div className="flex-1" />
            <span className="self-center text-[12.5px] text-faint">
              {results.data
                ? query
                  ? `${results.data.total.toLocaleString()} matches`
                  : 'recently added'
                : ''}
            </span>
          </div>

          <div className="mx-auto w-full max-w-[1560px] flex-1 overflow-y-auto">
            {results.error && (
              <p className="py-16 text-center text-bad">
                {results.error.message}
              </p>
            )}
            {results.data?.items.length === 0 && (
              <p className="py-16 text-center text-faint">No matches.</p>
            )}
            <CardGrid className="pt-[18px] pb-6">
              {results.data?.items.map((card) => (
                <CardTile
                  key={card.id}
                  card={card}
                  // Clicking a result navigates; the overlay has to get out of
                  // the way itself, since the link does not close it.
                  onClick={() => onOpenChange(false)}
                />
              ))}
            </CardGrid>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
