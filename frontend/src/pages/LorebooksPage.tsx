import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import {
  FilterChip,
  LorebooksBar,
  Notice,
  Pill,
  SearchField,
} from '@/components/lorebooks/parts'
import { SyncButton } from '@/components/lorebooks/sync'
import { type Lorebook, useLorebooks } from '@/hooks/use-lorebooks'

type Filter =
  'all' | 'shared' | 'undecided' | 'pending' | 'similar' | 'uncaptured'

const FILTERS: {
  key: Filter
  label: string
  test: (b: Lorebook) => boolean
}[] = [
  { key: 'all', label: 'All', test: () => true },
  { key: 'shared', label: 'Shared', test: (b) => b.card_count > 1 },
  {
    key: 'undecided',
    label: 'To decide',
    test: (b) => b.unresolved_entries > 0,
  },
  { key: 'pending', label: 'To sync', test: (b) => b.pending_cards > 0 },
  {
    key: 'similar',
    label: 'Merge candidates',
    test: (b) => b.similar_count > 0,
  },
  {
    key: 'uncaptured',
    label: 'Not captured',
    test: (b) => !b.has_content,
  },
]

/**
 * Lorebooks (`/lore`) -- every distinct lorebook in the archive, one row
 * each however many cards embed a copy. Derived server-side from the cards
 * (`proxy.archive.lorebooks`). The one thing here that writes is Sync, which
 * carries every stored decision out onto the cards after a confirmation.
 */
export function LorebooksPage() {
  const query = useLorebooks()
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<Filter>('shared')

  const all = query.data?.lorebooks
  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase()
    const test = FILTERS.find((f) => f.key === filter)!.test
    return (all ?? []).filter(
      (book) =>
        test(book) &&
        (!needle ||
          book.name.toLowerCase().includes(needle) ||
          [...book.authors, ...book.creators].some((c) =>
            c.toLowerCase().includes(needle),
          )),
    )
  }, [all, search, filter])

  const stats = query.data?.stats

  return (
    <>
      <LorebooksBar>
        <SearchField
          value={search}
          onChange={setSearch}
          placeholder="Filter by name or creator"
        />
        {FILTERS.map((f) => (
          <FilterChip
            key={f.key}
            active={filter === f.key}
            onClick={() => setFilter(f.key)}
          >
            {f.label}
            {all && (
              <span className="ml-1.5 opacity-60">
                {all.filter(f.test).length}
              </span>
            )}
          </FilterChip>
        ))}
        <div className="flex-1" />
        {stats && (
          <span className="text-[12.5px] whitespace-nowrap text-faint">
            {stats.cards.toLocaleString()} cards ·{' '}
            {stats.entries_embedded.toLocaleString()} entries embedded,{' '}
            {stats.entries_unique.toLocaleString()} distinct
          </span>
        )}
        {stats && <SyncButton count={stats.pending_cards} />}
      </LorebooksBar>

      <div className="mx-auto max-w-[1320px] px-5 pt-4 pb-24">
        {query.isPending && <Notice>Reading lorebooks off the cards…</Notice>}
        {query.error && <Notice tone="bad">{query.error.message}</Notice>}
        {all && shown.length === 0 && <Notice>No lorebooks match.</Notice>}
        {shown.length > 0 && (
          <div className="overflow-hidden rounded-[15px] border border-line-soft bg-surface">
            {shown.map((book) => (
              <LorebookRow key={book.id} book={book} />
            ))}
          </div>
        )}
      </div>
    </>
  )
}

function LorebookRow({ book }: { book: Lorebook }) {
  // Who wrote the lorebook when JanitorAI said so; otherwise the creators of
  // the cards carrying it, which is usually the same person.
  const names = book.authors.length > 0 ? book.authors : book.creators
  return (
    <Link
      to={`/lore/${book.id}`}
      className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line-soft px-4 py-3 last:border-b-0 hover:bg-raised"
    >
      <span className="text-[14.5px] font-medium">{book.name}</span>
      <span className="text-[12.5px] text-faint">
        {names.slice(0, 2).join(', ')}
        {names.length > 2 && ` +${names.length - 2}`}
      </span>
      {!book.has_content && (
        <Pill
          tone="warn"
          title="JanitorAI lists it for these characters, but no card holds its entries — private, or never fetched"
        >
          not captured
        </Pill>
      )}
      {book.unresolved_entries > 0 ? (
        <Pill
          tone="warn"
          title="Entries whose text differs between cards, with no version chosen"
        >
          {book.unresolved_entries} to decide
        </Pill>
      ) : (
        book.revision_count > 1 && (
          <Pill title="Cards carry different copies of this lorebook">
            {book.revision_count} revisions
          </Pill>
        )
      )}
      {book.pending_cards > 0 && (
        <Pill tone="sage" title="Cards a sync would rewrite for this lorebook">
          {book.pending_cards} card{book.pending_cards === 1 ? '' : 's'} to sync
        </Pill>
      )}
      {book.similar_count > 0 && (
        <Pill title="Other lorebooks share entries, or a title and author, with this one">
          overlaps {book.similar_count}
        </Pill>
      )}
      {book.refs.length === 0 && (
        <Pill title="No provider id on the cards — matched on identical entries alone">
          no source id
        </Pill>
      )}
      <span className="ml-auto text-[12.5px] whitespace-nowrap text-muted">
        {book.card_count} card{book.card_count === 1 ? '' : 's'}
        {book.listed_card_count > 0 &&
          ` (+${book.listed_card_count} listing it)`}{' '}
        · {book.entry_count} entr{book.entry_count === 1 ? 'y' : 'ies'}
      </span>
    </Link>
  )
}
