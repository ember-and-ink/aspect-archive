import {
  Popover,
  PopoverContent,
  PopoverDot,
  PopoverHeading,
  PopoverItem,
  PopoverTrigger,
} from '@/components/ui/popover'
import { FLAG_LABELS, type BrowseState, type Flag } from '@/lib/browse'

/** Flags in the popover, including the three that are not preset chips. */
const FLAGS: Flag[] = [
  'fav',
  'lore',
  'greets',
  'new',
  'untagged',
  'media',
  'fork',
  'lowres',
]

/**
 * ＋ Filter: the six flags, in full wording.
 *
 * Three of them are preset chips already; the popover is how the other three —
 * Favorites, Untagged, Needs media — are reached at all, and turning one on
 * adds it to the strip as a removable chip.
 *
 * The tag catalogue used to live here too. It moved to the Tags pill when
 * creator and source needed the same treatment: three long searchable lists in
 * one popover would have been a menu to scroll rather than a menu to read.
 */
export function FilterPopover({
  state,
  onChange,
  pinned = [],
}: {
  state: BrowseState
  onChange: (next: BrowseState) => void
  pinned?: Flag[]
}) {
  const toggleFlag = (flag: Flag) => {
    const flags = new Set(state.flags)
    if (!flags.delete(flag)) flags.add(flag)
    onChange({ ...state, flags })
  }

  return (
    <Popover>
      <PopoverTrigger className="inline-flex h-[30px] flex-none items-center gap-[7px] rounded-full border border-dashed border-white/15 px-3.5 text-[13px] text-faint hover:border-sage-line hover:text-sage">
        ＋ Filter
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[230px]">
        <PopoverHeading>Filters</PopoverHeading>
        {FLAGS.filter((flag) => !pinned.includes(flag)).map((flag) => (
          <PopoverItem key={flag} onClick={() => toggleFlag(flag)}>
            <PopoverDot state={state.flags.has(flag) ? 'inc' : undefined} />
            {FLAG_LABELS[flag]}
          </PopoverItem>
        ))}
      </PopoverContent>
    </Popover>
  )
}
