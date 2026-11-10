import { useState } from 'react'
import { Globe, Tags, User } from 'lucide-react'
import {
  Popover,
  PopoverContent,
  PopoverCount,
  PopoverDot,
  PopoverHeading,
  PopoverItem,
  PopoverSeparator,
  PopoverTrigger,
} from '@/components/ui/popover'
import { useFacets } from '@/hooks/use-characters'
import { groupSources } from '@/lib/card'
import type { BrowseState } from '@/lib/browse'
import { cn } from '@/lib/utils'

/**
 * The three long-list filters — tags, creator, source — each behind its own icon
 * pill rather than in the chip strip.
 *
 * The strip holds a fixed number of chips and stays put; these three do not.
 * Tags alone can run to a dozen selections, and every one of them used to widen
 * the toolbar until the sort control was pushed off the row. A pill is constant
 * width whatever is selected inside it, so the bar cannot grow — the cost is
 * that a selected tag is no longer visible without opening the pill, which the
 * count on the pill and the pinned "selected first" ordering inside it are there
 * to soften.
 */

/** The shared trigger. Lit when the filter is doing something, muted when not. */
function Pill({
  icon: Icon,
  label,
  active,
  tone = 'inc',
  title,
}: {
  icon: typeof Tags
  /** What is selected, shown beside the icon. Absent while the pill is off, so
   *  an unused pill costs one icon's width. */
  label?: string
  active: boolean
  tone?: 'inc' | 'exc'
  title: string
}) {
  return (
    <PopoverTrigger
      title={title}
      aria-label={title}
      className={cn(
        'inline-flex h-[30px] max-w-[190px] flex-none items-center gap-1.5 rounded-full border border-white/6 bg-white/3 px-2.5 text-[13px] text-muted-foreground hover:bg-white/8 hover:text-text',
        active && 'border-sage-line bg-sage-dim text-sage hover:bg-sage-dim',
        active &&
          tone === 'exc' &&
          'border-bad/30 bg-bad/12 text-bad hover:bg-bad/12',
      )}
    >
      <Icon className="size-3.5 flex-none" />
      {label && <span className="truncate">{label}</span>}
    </PopoverTrigger>
  )
}

/** The search box the tag and creator lists share. */
function PillSearch({
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
      className="mt-0.5 mb-1.5 h-8 w-full rounded-[9px] border border-line bg-ground px-2.5 focus:border-sage focus:outline-none"
      placeholder={placeholder}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      autoFocus
    />
  )
}

function Pending() {
  return <div className="px-2.5 py-2 text-[13px] text-faint">reading…</div>
}

function Failed({ message }: { message: string }) {
  return <div className="px-2.5 py-2 text-[13px] text-bad">{message}</div>
}

function NoMatches() {
  return (
    <div className="px-2.5 py-2 text-[13px] text-faint">nothing matches</div>
  )
}

export function TagsPill({
  state,
  onChange,
}: {
  state: BrowseState
  onChange: (next: BrowseState) => void
}) {
  const [search, setSearch] = useState('')
  const facets = useFacets()

  const cycleTag = (tag: string) => {
    const tags = new Map(state.tags)
    const current = tags.get(tag)
    if (!current) tags.set(tag, 'inc')
    else if (current === 'inc') tags.set(tag, 'exc')
    else tags.delete(tag)
    onChange({ ...state, tags })
  }

  const needle = search.trim().toLowerCase()
  const all = facets.data?.tags ?? []
  const matching = all.filter((t) => t.value.toLowerCase().includes(needle))
  // Selected tags first, so the pill can be emptied without hunting for them in
  // a 634-row list — they are the rows the user is most likely to want next.
  const chosen = matching.filter((t) => state.tags.has(t.value))
  const rest = matching.filter((t) => !state.tags.has(t.value))

  const count = state.tags.size
  const modes = [...state.tags.values()]
  const allExcluded = count > 0 && modes.every((m) => m === 'exc')

  return (
    <Popover>
      <Pill
        icon={Tags}
        active={count > 0}
        tone={allExcluded ? 'exc' : 'inc'}
        label={count > 0 ? String(count) : undefined}
        title={count > 0 ? `${count} tag filters` : 'Filter by tag'}
      />
      <PopoverContent align="start" className="w-[278px]">
        <PillSearch
          value={search}
          onChange={setSearch}
          placeholder="Search tags…"
        />
        {/* One line at 278px — the longer wording wrapped, and a two-line
            heading above a scrolling list reads as a paragraph. */}
        <PopoverHeading>Tags · click twice to exclude</PopoverHeading>
        <PickList
          facets={facets}
          empty={matching.length === 0}
          clearLabel={count > 0 && `Clear ${count} selected`}
          onClear={() => onChange({ ...state, tags: new Map() })}
        >
          {[...chosen, ...rest].map((tag) => (
            <PopoverItem key={tag.value} onClick={() => cycleTag(tag.value)}>
              <PopoverDot state={state.tags.get(tag.value)} />
              {tag.value}
              <PopoverCount>{tag.count.toLocaleString()}</PopoverCount>
            </PopoverItem>
          ))}
        </PickList>
      </PopoverContent>
    </Popover>
  )
}

export function CreatorPill({
  state,
  onChange,
}: {
  state: BrowseState
  onChange: (next: BrowseState) => void
}) {
  const [search, setSearch] = useState('')
  const facets = useFacets()

  // One creator at a time: the API matches a single name exactly, and picking a
  // second would mean "cards by both", which no card satisfies.
  const pick = (creator: string) =>
    onChange({ ...state, creator: state.creator === creator ? '' : creator })

  const needle = search.trim().toLowerCase()
  const matching = (facets.data?.creators ?? []).filter((c) =>
    c.value.toLowerCase().includes(needle),
  )
  const chosen = matching.filter((c) => c.value === state.creator)
  const rest = matching.filter((c) => c.value !== state.creator)

  return (
    <Popover>
      <Pill
        icon={User}
        active={state.creator !== ''}
        label={state.creator || undefined}
        title={
          state.creator ? `Cards by ${state.creator}` : 'Filter by creator'
        }
      />
      <PopoverContent align="start" className="w-[278px]">
        <PillSearch
          value={search}
          onChange={setSearch}
          placeholder="Search creators…"
        />
        <PopoverHeading>Creator</PopoverHeading>
        <PickList
          facets={facets}
          empty={matching.length === 0}
          clearLabel={state.creator && 'Any creator'}
          onClear={() => onChange({ ...state, creator: '' })}
        >
          {[...chosen, ...rest].map((creator) => (
            <PopoverItem
              key={creator.value}
              onClick={() => pick(creator.value)}
            >
              <PopoverDot
                state={state.creator === creator.value ? 'inc' : undefined}
              />
              <span className="truncate">{creator.value}</span>
              <PopoverCount>{creator.count.toLocaleString()}</PopoverCount>
            </PopoverItem>
          ))}
        </PickList>
      </PopoverContent>
    </Popover>
  )
}

export function SourcePill({
  state,
  onChange,
}: {
  state: BrowseState
  onChange: (next: BrowseState) => void
}) {
  const facets = useFacets()
  const groups = groupSources(facets.data?.sources ?? [])

  // A platform is on when every kind it covers is selected. Toggling adds or
  // removes the whole group, so the two-kind platforms behave as one choice.
  const isOn = (kinds: string[]) =>
    kinds.every((k) => state.sources.includes(k))
  const toggle = (kinds: string[]) => {
    const next = new Set(state.sources)
    if (isOn(kinds)) for (const kind of kinds) next.delete(kind)
    else for (const kind of kinds) next.add(kind)
    onChange({ ...state, sources: [...next] })
  }

  const on = groups.filter((g) => isOn(g.kinds))
  const label =
    on.length === 0
      ? undefined
      : on.length === 1
        ? on[0].label
        : `${on.length} sources`

  return (
    <Popover>
      <Pill
        icon={Globe}
        active={state.sources.length > 0}
        label={label}
        title={label ? `Source: ${label}` : 'Filter by source'}
      />
      <PopoverContent align="start" className="w-[230px]">
        <PopoverHeading>Source</PopoverHeading>
        {facets.isPending && <Pending />}
        {facets.error && <Failed message={facets.error.message} />}
        {state.sources.length > 0 && (
          <>
            <PopoverItem
              className="text-[12.5px] text-faint"
              onClick={() => onChange({ ...state, sources: [] })}
            >
              Any source
            </PopoverItem>
            <PopoverSeparator />
          </>
        )}
        {groups.map((group) => (
          <PopoverItem key={group.platform} onClick={() => toggle(group.kinds)}>
            <PopoverDot state={isOn(group.kinds) ? 'inc' : undefined} />
            {group.label}
            <PopoverCount>{group.count.toLocaleString()}</PopoverCount>
          </PopoverItem>
        ))}
      </PopoverContent>
    </Popover>
  )
}

/**
 * The body of a facet pill: loading/error state, an optional reset row on top
 * (shown while `clearLabel` is set), then the scrolling list of options.
 */
function PickList({
  facets,
  empty,
  clearLabel,
  onClear,
  children,
}: {
  facets: { isPending: boolean; error: Error | null }
  empty: boolean
  clearLabel: string | false
  onClear: () => void
  children: React.ReactNode
}) {
  return (
    <>
      {facets.isPending && <Pending />}
      {facets.error && <Failed message={facets.error.message} />}
      {clearLabel && (
        <>
          <PopoverItem className="text-[12.5px] text-faint" onClick={onClear}>
            {clearLabel}
          </PopoverItem>
          <PopoverSeparator />
        </>
      )}
      <div className="max-h-[300px] overflow-y-auto">
        {!facets.isPending && !facets.error && empty && <NoMatches />}
        {children}
      </div>
    </>
  )
}
