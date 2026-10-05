import { Link, useLocation } from 'react-router'
import type { LorebookCard, LorebookEntry } from '@/hooks/use-lorebooks'
import { cn } from '@/lib/utils'

const VIEWS = [
  { to: '/lore', label: 'Lorebooks' },
  { to: '/lore/entries', label: 'Entries' },
]

/** The Lorebooks/Entries switcher -- the same pill group as `ToolsNav`.
 *
 *  Active state is worked out here rather than left to `NavLink`: a single
 *  lorebook lives at `/lore/:id`, which should light "Lorebooks", while
 *  `/lore/entries/...` is nested under the same prefix and should not. */
export function LorebooksNav() {
  const { pathname } = useLocation()
  const onEntries =
    pathname === '/lore/entries' || pathname.startsWith('/lore/entries/')
  return (
    <div className="flex gap-[3px] rounded-full border border-line bg-raised p-[3px]">
      {VIEWS.map((view) => (
        <Link
          key={view.to}
          to={view.to}
          className={cn(
            'rounded-full px-4 py-1.5 text-[13px] font-medium text-muted hover:text-text',
            (view.to === '/lore/entries') === onEntries &&
              'bg-text font-semibold text-[#121416]',
          )}
        >
          {view.label}
        </Link>
      ))}
    </div>
  )
}

/** The sticky bar every lorebook view opens with. */
export function LorebooksBar({ children }: { children?: React.ReactNode }) {
  return (
    <div className="sticky top-0 z-20 border-b border-line-soft bg-ground/95 backdrop-blur-[12px]">
      <div className="mx-auto flex max-w-[1320px] flex-wrap items-center gap-3 px-5 py-3">
        <LorebooksNav />
        {children}
      </div>
    </div>
  )
}

export function Pill({
  tone = 'plain',
  children,
  title,
}: {
  tone?: 'plain' | 'sage' | 'warn'
  children: React.ReactNode
  title?: string
}) {
  return (
    <span
      title={title}
      className={cn(
        'rounded-full border px-2 py-px text-[11.5px] whitespace-nowrap',
        tone === 'plain' && 'border-line text-muted',
        tone === 'sage' && 'border-sage-line bg-sage-dim text-sage',
        tone === 'warn' && 'border-warn/40 bg-warn/10 text-warn',
      )}
    >
      {children}
    </span>
  )
}

export function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'h-[33px] rounded-full border px-3.5 text-[13px] whitespace-nowrap',
        active
          ? 'border-sage-line bg-sage-dim text-sage'
          : 'border-line text-muted hover:bg-raised hover:text-text',
      )}
    >
      {children}
    </button>
  )
}

export function SearchField({
  value,
  onChange,
  placeholder,
}: {
  value: string
  onChange: (value: string) => void
  placeholder: string
}) {
  return (
    <input
      type="search"
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
      className="h-[33px] w-[260px] rounded-full border border-line bg-transparent px-3.5 text-[13px] placeholder:text-faint focus:border-sage-line focus:outline-none"
    />
  )
}

/** Small avatar tiles linking to the cards that carry something. With
 *  `action`, each tile grows a small button beside its name -- detach, undo. */
export function CardStrip({
  cards,
  action,
}: {
  cards: LorebookCard[]
  action?: {
    label: string
    disabled?: boolean
    run: (card: LorebookCard) => void
  }
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {cards.map((card) => (
        <span
          key={card.id}
          className="flex items-center rounded-full border border-line-soft bg-raised text-[12.5px] hover:border-line"
        >
          <Link
            to={`/characters/${encodeURIComponent(card.id)}`}
            title={card.creator ? `${card.name} · ${card.creator}` : card.name}
            className={cn(
              'flex items-center gap-2 py-1 pl-1 hover:text-text',
              action ? 'pr-2' : 'pr-3',
            )}
          >
            <img
              src={card.thumb_url}
              alt=""
              loading="lazy"
              className="size-6 rounded-full object-cover object-top"
            />
            <span className="max-w-[160px] truncate">{card.name}</span>
          </Link>
          {action && (
            <button
              type="button"
              disabled={action.disabled}
              onClick={() => action.run(card)}
              aria-label={`${action.label} ${card.name}`}
              className="mr-1 rounded-full px-2 py-0.5 text-[11.5px] text-faint hover:bg-hi hover:text-text disabled:opacity-50"
            >
              {action.label}
            </button>
          )}
        </span>
      ))}
    </div>
  )
}

/** One entry line as a row: title, keys, a preview of its newest text. */
export function EntryRow({
  entry,
  showLorebook = false,
}: {
  entry: LorebookEntry
  showLorebook?: boolean
}) {
  return (
    <Link
      to={`/lore/entries/${entry.id}`}
      className="block border-b border-line-soft px-4 py-3 last:border-b-0 hover:bg-raised"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[14px] font-medium">
          {entry.title || <span className="text-faint">Untitled entry</span>}
        </span>
        {entry.version_count > 1 && (
          <Pill tone={entry.resolved ? 'plain' : 'warn'}>
            {entry.version_count} versions
          </Pill>
        )}
        {entry.version_count > 1 && entry.resolved && (
          <Pill tone="sage">
            chosen
            {entry.pending_cards > 0 &&
              ` · ${entry.pending_cards} card${entry.pending_cards === 1 ? '' : 's'} to sync`}
          </Pill>
        )}
        {entry.constant && <Pill>always on</Pill>}
        <span className="ml-auto text-[12px] whitespace-nowrap text-faint">
          {showLorebook && `${entry.lorebook_name} · `}
          {entry.card_count} card{entry.card_count === 1 ? '' : 's'} ·{' '}
          {entry.chars.toLocaleString()} chars
        </span>
      </div>
      {entry.keys.length > 0 && (
        <div className="mt-1 truncate font-mono text-[11.5px] text-faint">
          {entry.keys.join(', ')}
        </div>
      )}
      <p className="mt-1 line-clamp-2 text-[13px] text-muted">
        {entry.preview}
      </p>
    </Link>
  )
}

export function Notice({
  tone = 'faint',
  children,
}: {
  tone?: 'faint' | 'bad'
  children: React.ReactNode
}) {
  return (
    <p
      className={cn(
        'py-16 text-center',
        tone === 'bad' ? 'text-bad' : 'text-faint',
      )}
    >
      {children}
    </p>
  )
}

/** A small bordered action button -- the one every lorebook decision uses. */
export function ActionButton({
  onClick,
  disabled,
  tone = 'plain',
  children,
}: {
  onClick: () => void
  disabled?: boolean
  tone?: 'plain' | 'sage'
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'rounded-full border px-3 py-1 text-[12.5px] whitespace-nowrap disabled:opacity-50',
        tone === 'sage'
          ? 'border-sage-line bg-sage-dim text-sage hover:bg-sage/25'
          : 'border-line text-muted hover:bg-hi hover:text-text',
      )}
    >
      {children}
    </button>
  )
}
