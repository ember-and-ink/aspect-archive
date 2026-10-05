import { useState } from 'react'
import { Link } from 'react-router'
import {
  type CardLorebook,
  useCardLorebooks,
  useLorebookActions,
  useLorebookOriginal,
  useLorebooks,
} from '@/hooks/use-lorebooks'
import { toast } from '@/lib/toast'
import { ActionButton, Pill, SearchField } from './parts'

/**
 * The lorebooks behind a card's embedded book, on the card's Lorebook tab.
 *
 * A card embeds one flat book; this is that book taken back apart into the
 * lorebooks it holds, and where they are attached and detached. Both are
 * decisions that wait for a sync, so each row says which state it is in and
 * offers the way back. Once a sync has rewritten the card, the book it had
 * before is offered here too.
 */
export function CardLorebooks({ cardId }: { cardId: string }) {
  const books = useCardLorebooks(cardId)
  const original = useLorebookOriginal(cardId)
  const actions = useLorebookActions()
  const onError = (error: Error) => toast(error.message, 'bad')
  const busy = actions.attach.isPending || actions.detach.isPending
  const rows = books.data ?? []
  const waiting = rows.filter(
    (row) => row.state !== 'attached' || row.pending_upgrades > 0,
  ).length

  return (
    <div className="mb-5 rounded-[15px] border border-line-soft bg-surface px-4 py-3">
      <div className="flex flex-wrap items-center gap-2 text-[13px]">
        <span className="font-semibold">Lorebooks on this card</span>
        {waiting > 0 && (
          <span className="text-[12.5px] text-muted">
            Changes below are written at the next{' '}
            <Link to="/lore" className="text-sage hover:underline">
              sync
            </Link>
            .
          </span>
        )}
      </div>

      {books.error && (
        <p className="mt-2 text-[12.5px] text-bad">{books.error.message}</p>
      )}
      {books.data && rows.length === 0 && (
        <p className="mt-2 text-[12.5px] text-faint">None attached.</p>
      )}
      {rows.map((row) => (
        <Row
          key={`${row.lorebook_id}-${row.state}`}
          row={row}
          busy={busy}
          onAttach={() =>
            actions.attach.mutate(
              { card: cardId, lorebook: row.lorebook_id },
              { onError },
            )
          }
          onDetach={() =>
            actions.detach.mutate(
              { card: cardId, lorebook: row.lorebook_id },
              { onError },
            )
          }
        />
      ))}

      <AttachPicker
        exclude={rows.map((row) => row.lorebook_id)}
        disabled={busy}
        onAttach={(lorebook) =>
          actions.attach.mutate({ card: cardId, lorebook }, { onError })
        }
      />

      {original.data?.exists && original.data.differs && (
        <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-line-soft pt-3 text-[12.5px] text-muted">
          <span>
            A sync rewrote this card’s lorebook
            {original.data.taken_at &&
              ` on ${original.data.taken_at.slice(0, 10)}`}
            . The original ({original.data.entry_count}{' '}
            {original.data.entry_count === 1 ? 'entry' : 'entries'}) is saved.
          </span>
          <span className="flex-1" />
          <ActionButton
            disabled={actions.restoreOriginal.isPending}
            onClick={() =>
              actions.restoreOriginal.mutate(cardId, {
                onError,
                onSuccess: () => toast('Original lorebook restored.'),
              })
            }
          >
            Restore the original
          </ActionButton>
        </div>
      )}
    </div>
  )
}

function Row({
  row,
  busy,
  onAttach,
  onDetach,
}: {
  row: CardLorebook
  busy: boolean
  onAttach: () => void
  onDetach: () => void
}) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 text-[13.5px]">
      <Link to={`/lore/${row.lorebook_id}`} className="hover:underline">
        {row.name}
      </Link>
      {row.state === 'attaching' && <Pill tone="sage">attach on sync</Pill>}
      {row.state === 'detaching' && <Pill tone="warn">detach on sync</Pill>}
      {row.pending_upgrades > 0 && (
        <Pill tone="sage">
          {row.pending_upgrades}{' '}
          {row.pending_upgrades === 1 ? 'entry' : 'entries'} to update
        </Pill>
      )}
      <span className="text-[12.5px] text-faint">
        {row.entry_count} {row.entry_count === 1 ? 'entry' : 'entries'} ·{' '}
        {row.card_count} card{row.card_count === 1 ? '' : 's'}
      </span>
      <span className="flex-1" />
      {/* Each undo is the opposite request: asking for what the card already
          is withdraws the decision. */}
      {row.state === 'attached' && (
        <ActionButton disabled={busy} onClick={onDetach}>
          Detach
        </ActionButton>
      )}
      {row.state === 'attaching' && (
        <ActionButton disabled={busy} onClick={onDetach}>
          Undo
        </ActionButton>
      )}
      {row.state === 'detaching' && (
        <ActionButton disabled={busy} onClick={onAttach}>
          Undo
        </ActionButton>
      )}
    </div>
  )
}

function AttachPicker({
  exclude,
  disabled,
  onAttach,
}: {
  exclude: string[]
  disabled: boolean
  onAttach: (lorebook: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')

  if (!open)
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-2.5 text-[12.5px] text-faint hover:text-text"
      >
        Attach a lorebook…
      </button>
    )
  return (
    <div className="mt-3 border-t border-line-soft pt-3">
      <SearchField
        value={search}
        onChange={setSearch}
        placeholder="Find a lorebook by name or creator"
      />
      <Matches
        search={search}
        exclude={exclude}
        disabled={disabled}
        onAttach={(lorebook) => {
          onAttach(lorebook)
          setSearch('')
          setOpen(false)
        }}
      />
    </div>
  )
}

/** Its own component so the whole lorebook list is only fetched once the
 *  picker is open. */
function Matches({
  search,
  exclude,
  disabled,
  onAttach,
}: {
  search: string
  exclude: string[]
  disabled: boolean
  onAttach: (lorebook: string) => void
}) {
  const all = useLorebooks()
  const needle = search.trim().toLowerCase()
  const matches = needle
    ? (all.data?.lorebooks ?? [])
        .filter(
          (book) =>
            !exclude.includes(book.id) &&
            (book.name.toLowerCase().includes(needle) ||
              book.creators.some((c) => c.toLowerCase().includes(needle))),
        )
        .slice(0, 8)
    : []
  return (
    <>
      {matches.map((book) => (
        <div
          key={book.id}
          className="mt-2 flex flex-wrap items-center gap-3 text-[13.5px]"
        >
          <Link to={`/lore/${book.id}`} className="hover:underline">
            {book.name}
          </Link>
          <span className="text-[12.5px] text-faint">
            {book.creators.slice(0, 2).join(', ')} · {book.entry_count} entr
            {book.entry_count === 1 ? 'y' : 'ies'} · {book.card_count} card
            {book.card_count === 1 ? '' : 's'}
          </span>
          <span className="flex-1" />
          <ActionButton disabled={disabled} onClick={() => onAttach(book.id)}>
            Attach
          </ActionButton>
        </div>
      ))}
      {needle && matches.length === 0 && all.data && (
        <p className="mt-2 text-[12.5px] text-faint">No lorebooks match.</p>
      )}
    </>
  )
}
