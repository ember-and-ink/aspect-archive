import { useState } from 'react'
import {
  Check,
  ChevronDown,
  Copy,
  Download,
  Plus,
  SquareMousePointer,
  Trash2,
  X,
} from 'lucide-react'
import { CardTile } from '@/components/CardTile'
import { CardGrid } from '@/components/CardGrid'
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
  useCharacterDetail,
  useExpressionFiles,
  useForksOf,
  useGalleryFiles,
  useSameCreator,
  useSharesTag,
  type CardDetail,
} from '@/hooks/use-character-detail'
import { useBulkDeleteGalleryFiles } from '@/hooks/use-gallery-mutations'
import { toast } from '@/lib/toast'
import {
  dialogueBlocks,
  estimateTokens,
  extension,
  formatBytes,
  formatDate,
  formatTokens,
  greetings,
  loreEntries,
  lorebookName,
  sourceLabel,
  str,
  type DialogueBlock,
  type GalleryFile,
  type LoreEntry,
} from '@/lib/card'
import { setDialogue, setField, setGreetings } from '@/lib/card-edit'
import { copyText } from '@/lib/clipboard'
import { groupExpressions } from '@/lib/expressions'
import { cn } from '@/lib/utils'
import { AvatarSize } from './AvatarRefetch'
import { CreatorNotes } from './CreatorNotes'
import { EditActions, EditButton, useEdit } from './edit-context'
import { InlineTextField } from './editors'
import { Lightbox } from './Lightbox'
import { MediaDiscovery } from './MediaDiscovery'
import { MediaUpload } from './MediaUpload'
import { ClampedProse, EmptyState, ProseBox, Section } from './Section'

/**
 * The subset of a card the four content panes actually read.
 *
 * `CardDetailOut` describes a card *on disk* -- filename, size, mtime,
 * thumbnail, gallery. A Discover preview is the same card before any of that
 * exists (`POST /api/v1/discover/preview`), so it cannot satisfy that type and
 * should not pretend to. Naming what the panes need instead lets Overview,
 * Notes, Greetings and Lorebook serve both without a single fake field: an
 * archived card is structurally a `PaneCard`, so `CharacterDetailPage` keeps
 * passing its `CardDetail` unchanged.
 *
 * The other three panes stay on `CardDetail` on purpose -- Gallery, Related and
 * Info are *about* being in the archive, which is exactly why the preview hides
 * them.
 */
export type PaneCard = Pick<
  CardDetail,
  | 'card'
  | 'name'
  | 'page_name'
  | 'description_chars'
  | 'greetings'
  | 'lore_entries'
  | 'create_date'
>

/** Save / Cancel row shared by the editors, right-aligned under the field. */
function SaveRow({
  onSave,
  disabled,
}: {
  onSave: () => void
  disabled?: boolean
}) {
  return (
    <div className="mt-2.5 flex">
      <EditActions onSave={onSave} disabled={disabled} />
    </div>
  )
}

// ---- Overview --------------------------------------------------------------

export function OverviewPane({ card }: { card: PaneCard }) {
  const data = card.card
  // The source page's title blurb is the closest thing a JAI/Chub card has to a
  // tagline; hide it when it merely repeats the name (common on JAI, where the
  // page title is often just the character's name).
  const tagline =
    card.page_name && card.page_name !== card.name ? card.page_name : ''

  return (
    <>
      {tagline && (
        <Section title="Tagline">
          <div className="mt-2.5 font-serif text-[19px] leading-[1.45] text-[#d2d8da]">
            {tagline}
          </div>
        </Section>
      )}
      <Section title="At a glance">
        <div className="mt-2.5 grid grid-cols-[repeat(auto-fit,minmax(116px,1fr))] gap-2.5">
          <Stat
            value={estimateTokens(card.description_chars).toLocaleString()}
            label="description tokens"
          />
          <Stat value={card.greetings} label="greetings" />
          <Stat value={card.lore_entries} label="lore entries" />
          <Stat value={formatDate(card.create_date)} label="created" />
        </div>
      </Section>
      <CardTextSection data={data} field="description" title="Description" />
      <CardTextSection data={data} field="scenario" title="Scenario" />
      <CardTextSection data={data} field="personality" title="Personality" />
      {/* No "First message" here. It is greeting 1, which the Greetings tab
          already shows in full and is the only place it can be edited --
          rendering it twice made a long greeting dominate Overview and left two
          copies that could disagree mid-edit (Stage 6B D7). */}
    </>
  )
}

/**
 * One of the card's three main prose fields (description, scenario,
 * personality). Always rendered, even when the field is empty -- an empty
 * field is still worth showing, as an empty text box ready for an edit,
 * rather than hidden behind a separate empty state. Long text clamps to 20
 * lines with a click to expand, same as a long greeting.
 */
function CardTextSection({
  data,
  field,
  title,
}: {
  data: PaneCard['card']
  field: string
  title: string
}) {
  const { editing, save } = useEdit()
  const value = str(data, field)
  const isEditing = editing === field
  return (
    <Section
      title={title}
      count={value ? `(${formatTokens(value.length)})` : undefined}
      action={<EditButton section={field} />}
    >
      {isEditing ? (
        <TextFieldEditor
          initial={value}
          onSave={(next) => save(setField(data, field, next))}
        />
      ) : (
        <ClampedProse lines={20}>{value}</ClampedProse>
      )}
    </Section>
  )
}

function TextFieldEditor({
  initial,
  onSave,
}: {
  initial: string
  onSave: (value: string) => void
}) {
  const [draft, setDraft] = useState(initial)
  return (
    <>
      <InlineTextField value={draft} onChange={setDraft} autoFocus />
      <SaveRow onSave={() => onSave(draft)} />
    </>
  )
}

function Stat({ value, label }: { value: React.ReactNode; label: string }) {
  return (
    <div className="rounded-xl border border-line-soft bg-surface px-[13px] py-[11px]">
      <b className="block font-mono text-[18px] font-semibold">{value}</b>
      <span className="text-[11.5px] text-faint">{label}</span>
    </div>
  )
}

// ---- Creator notes ---------------------------------------------------------

export function NotesPane({ card }: { card: PaneCard }) {
  const notes = str(card.card, 'creator_notes')
  if (!notes.trim())
    return <EmptyState>This card carries no creator notes.</EmptyState>
  return (
    <Section title="Creator notes">
      <div className="mt-2.5">
        <CreatorNotes notes={notes} />
        <p className="mt-3 border-t border-line-soft pt-3 text-[12.5px] text-faint">
          Rendered from the card's{' '}
          <span className="font-mono">creator_notes</span> in an isolated frame.
          Layout is kept; the creator's palette is not.
        </p>
      </div>
    </Section>
  )
}

// ---- Greetings -------------------------------------------------------------

export function GreetingsPane({ card }: { card: PaneCard }) {
  const { editing, save } = useEdit()
  const all = greetings(card.card)
  const isEditing = editing === 'greetings'

  if (isEditing)
    return (
      <Section title="Greetings" action={<EditButton section="greetings" />}>
        <GreetingsEditor
          initial={all}
          onSave={(next) => save(setGreetings(card.card, next))}
        />
      </Section>
    )

  if (all.length === 0)
    return (
      <Section title="Greetings" action={<EditButton section="greetings" />}>
        <EmptyState>This card has no greeting.</EmptyState>
      </Section>
    )

  return (
    <>
      {all.map((greeting, index) => (
        <Section
          key={index}
          title={index === 0 ? 'Greeting 1 · primary' : `Greeting ${index + 1}`}
          count={`(${formatTokens(greeting.length)})`}
          action={index === 0 ? <EditButton section="greetings" /> : undefined}
        >
          <ClampedProse>{greeting}</ClampedProse>
        </Section>
      ))}
    </>
  )
}

function GreetingsEditor({
  initial,
  onSave,
}: {
  initial: string[]
  onSave: (greetings: string[]) => void
}) {
  const [drafts, setDrafts] = useState<string[]>(
    initial.length ? initial : [''],
  )
  const set = (index: number, value: string) =>
    setDrafts(drafts.map((g, i) => (i === index ? value : g)))

  return (
    <div className="mt-2.5 flex flex-col gap-3">
      {drafts.map((greeting, index) => (
        <div key={index}>
          <div className="flex items-center gap-2.5 text-[11.5px] text-faint">
            <span>
              {index === 0 ? 'Greeting 1 · primary' : `Greeting ${index + 1}`}
            </span>
            {drafts.length > 1 && (
              <button
                type="button"
                onClick={() => setDrafts(drafts.filter((_, i) => i !== index))}
                className="text-faint hover:text-bad"
              >
                remove
              </button>
            )}
          </div>
          <InlineTextField
            value={greeting}
            onChange={(value) => set(index, value)}
            autoFocus={index === 0}
          />
        </div>
      ))}
      <button
        type="button"
        onClick={() => setDrafts([...drafts, ''])}
        className="flex items-center justify-center gap-1.5 rounded-xl border border-dashed border-line py-2.5 text-[12.5px] text-muted hover:border-sage-line hover:text-sage"
      >
        <Plus className="size-3.5" /> Add greeting
      </button>
      <SaveRow onSave={() => onSave(drafts)} />
    </div>
  )
}

// ---- Dialogue ---------------------------------------------------------------

/**
 * `mes_example`, read as a transcript rather than raw prose -- each `<START>`
 * block is its own Section, and each `{{user}}:` / `{{char}}:` turn inside it
 * is its own bubble. The speaker label is the one thing resolved off the
 * macro (the card's name for `{{char}}`, "You" for `{{user}}`); the turn text
 * itself is left exactly as written, the same as a greeting's body.
 */
export function DialoguePane({ card }: { card: PaneCard }) {
  const { editing, save } = useEdit()
  const blocks = dialogueBlocks(card.card)
  const isEditing = editing === 'dialogue'

  if (isEditing)
    return (
      <Section title="Dialogue" action={<EditButton section="dialogue" />}>
        <DialogueEditor
          initial={blocks.map((b) => b.raw)}
          onSave={(next) => save(setDialogue(card.card, next))}
        />
      </Section>
    )

  if (blocks.length === 0)
    return (
      <Section title="Dialogue" action={<EditButton section="dialogue" />}>
        <EmptyState>This card has no example dialogue.</EmptyState>
      </Section>
    )

  return (
    <>
      {blocks.map((block, index) => (
        <Section
          key={index}
          title={
            blocks.length > 1 ? `Example ${index + 1}` : 'Example dialogue'
          }
          count={`(${formatTokens(block.raw.length)})`}
          action={index === 0 ? <EditButton section="dialogue" /> : undefined}
        >
          <DialogueTranscript block={block} name={card.name} />
        </Section>
      ))}
    </>
  )
}

function DialogueTranscript({
  block,
  name,
}: {
  block: DialogueBlock
  name: string
}) {
  return (
    <div className="mt-2.5 flex flex-col gap-2.5">
      {block.turns.map((turn, index) => (
        <div
          key={index}
          className="rounded-xl border border-line-soft bg-surface px-[17px] py-[13px]"
        >
          {turn.speaker && (
            <div className="mb-1 text-[11px] font-semibold tracking-[0.06em] text-faint uppercase">
              {turn.speaker === 'char' ? name || 'Character' : 'You'}
            </div>
          )}
          <div className="text-[14.5px] leading-[1.68] whitespace-pre-wrap text-[#d2d8da]">
            {turn.text}
          </div>
        </div>
      ))}
    </div>
  )
}

function DialogueEditor({
  initial,
  onSave,
}: {
  initial: string[]
  onSave: (blocks: string[]) => void
}) {
  const [drafts, setDrafts] = useState<string[]>(
    initial.length ? initial : [''],
  )
  const set = (index: number, value: string) =>
    setDrafts(drafts.map((d, i) => (i === index ? value : d)))

  return (
    <div className="mt-2.5 flex flex-col gap-3">
      {drafts.map((draft, index) => (
        <div key={index}>
          <div className="flex items-center gap-2.5 text-[11.5px] text-faint">
            <span>
              {drafts.length > 1 ? `Example ${index + 1}` : 'Example dialogue'}
            </span>
            {drafts.length > 1 && (
              <button
                type="button"
                onClick={() => setDrafts(drafts.filter((_, i) => i !== index))}
                className="text-faint hover:text-bad"
              >
                remove
              </button>
            )}
          </div>
          <InlineTextField
            value={draft}
            onChange={(value) => set(index, value)}
            autoFocus={index === 0}
          />
        </div>
      ))}
      <button
        type="button"
        onClick={() => setDrafts([...drafts, ''])}
        className="flex items-center justify-center gap-1.5 rounded-xl border border-dashed border-line py-2.5 text-[12.5px] text-muted hover:border-sage-line hover:text-sage"
      >
        <Plus className="size-3.5" /> Add block
      </button>
      <SaveRow onSave={() => onSave(drafts)} />
    </div>
  )
}

// ---- Lorebook --------------------------------------------------------------

export function LorebookPane({ card }: { card: PaneCard }) {
  const entries = loreEntries(card.card)
  const name = lorebookName(card.card)

  if (entries.length === 0)
    return (
      <Section title={name || 'Lorebook'}>
        <EmptyState>No lorebook on this card.</EmptyState>
      </Section>
    )
  return (
    <Section title={name || 'Lorebook'} count={`${entries.length} entries`}>
      <div className="mt-2 flex flex-col gap-2">
        {entries.map((entry) => (
          <LoreEntryRow key={entry.id} entry={entry} />
        ))}
      </div>
    </Section>
  )
}

/**
 * One lorebook entry. `name` is the entry's title, `comment` is a short
 * description of it, `keys` are what makes it fire, and `content` is what
 * actually gets sent to the LLM when it does. Collapsed, the comment
 * truncates to one line and keys/content show a short preview; the chevron
 * expands to the full keys list and content.
 */
function LoreEntryRow({ entry }: { entry: LoreEntry }) {
  const [open, setOpen] = useState(false)
  const title = entry.name || entry.comment || 'Untitled entry'
  return (
    <button
      type="button"
      onClick={() => setOpen(!open)}
      className="flex flex-col gap-1 rounded-xl border border-line-soft bg-surface px-[13px] py-[11px] text-left hover:border-sage-line"
    >
      <div className="flex items-start gap-3">
        <span className="min-w-0 flex-1 truncate text-[13.5px] font-semibold text-[#d2d8da]">
          {title}
        </span>
        <ChevronDown
          className={cn(
            'mt-0.5 size-3.5 flex-none text-faint transition-transform',
            open && 'rotate-180',
          )}
        />
      </div>
      {entry.comment && (
        <div
          className={cn('text-[12px] text-faint italic', !open && 'truncate')}
        >
          {entry.comment}
        </div>
      )}
      <div className="mt-1 flex items-start gap-3">
        <span
          className={cn(
            'w-[150px] flex-none font-mono text-[11.5px] text-sage',
            open ? 'whitespace-normal break-words' : 'line-clamp-2',
          )}
          title={entry.keys.join(', ')}
        >
          {entry.keys.join(', ') || '—'}
        </span>
        <span
          className={cn(
            'flex-1 text-[13px] whitespace-pre-wrap text-[#c3cacd]',
            !open && 'line-clamp-2',
          )}
        >
          {entry.content || 'Entry text…'}
        </span>
      </div>
    </button>
  )
}

// ---- Gallery ---------------------------------------------------------------

export function GalleryPane({ card }: { card: CardDetail }) {
  const gallery = useGalleryFiles(card.gallery.folder, card.gallery.exists)
  const [open, setOpen] = useState<number | null>(null)
  const [selectMode, setSelectMode] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [confirmOpen, setConfirmOpen] = useState(false)
  const bulkDelete = useBulkDeleteGalleryFiles(card.gallery.folder)
  const files = gallery.data?.items ?? []

  const toggleSelectMode = () => {
    setSelectMode((was) => !was)
    setSelected(new Set())
  }
  const toggleSelected = (name: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  const onDelete = () =>
    bulkDelete.mutate([...selected], {
      onSuccess: (result) => {
        setSelectMode(false)
        setSelected(new Set())
        const failedCount = Object.keys(result.failed).length
        if (failedCount)
          toast(
            `Removed ${result.deleted.length}, ${failedCount} failed.`,
            'bad',
          )
        else
          toast(
            `Removed ${result.deleted.length} file${result.deleted.length === 1 ? '' : 's'}.`,
          )
      },
      onError: (error) => toast(error.message, 'bad'),
    })

  // The two ingest controls sit above the conditional body, in one fixed
  // position: a first upload flips the pane from its empty state to the grid,
  // and a control rendered inside those branches would unmount mid-write —
  // taking its own report ("3 added, 2 skipped") with it, exactly when there
  // is most to say.
  return (
    <>
      <MediaDiscovery cardId={card.id} />
      <MediaUpload kind="galleries" folder={card.gallery.folder} />
      {!card.gallery.exists ? (
        <EmptyState>No gallery has been downloaded for this card.</EmptyState>
      ) : gallery.isPending ? (
        <p className="mt-4 text-center text-faint">reading gallery…</p>
      ) : files.length === 0 ? (
        <EmptyState>The gallery folder is empty.</EmptyState>
      ) : (
        <Section
          title={`${files.length} files`}
          count={formatBytes(gallery.data!.bytes)}
          action={
            <div className="flex items-center gap-2">
              {selectMode && (
                <button
                  type="button"
                  onClick={() => setConfirmOpen(true)}
                  disabled={selected.size === 0}
                  className="flex h-7 items-center gap-1.5 rounded-lg border border-bad/50 px-3 text-[12.5px] text-bad hover:border-bad hover:bg-bad/10 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Trash2 className="size-3.5" />
                  Delete{selected.size ? ` (${selected.size})` : ''}
                </button>
              )}
              <button
                type="button"
                title={selectMode ? 'Cancel selection' : 'Select files'}
                onClick={toggleSelectMode}
                className={cn(
                  'grid size-7 place-items-center rounded-lg border',
                  selectMode
                    ? 'border-sage-line bg-sage/15 text-sage'
                    : 'border-line text-faint hover:border-sage-line hover:text-sage',
                )}
              >
                {selectMode ? (
                  <X className="size-3.5" />
                ) : (
                  <SquareMousePointer className="size-3.5" />
                )}
              </button>
            </div>
          }
        >
          <div className="mt-2.5 grid grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-2.5">
            {files.map((file, index) => (
              <GalleryThumb
                key={file.name}
                file={file}
                selectMode={selectMode}
                selected={selected.has(file.name)}
                onToggleSelect={() => toggleSelected(file.name)}
                onOpen={() => setOpen(index)}
              />
            ))}
          </div>
          {open !== null && (
            <Lightbox
              files={files}
              index={open}
              onIndex={setOpen}
              onClose={() => setOpen(null)}
            />
          )}
        </Section>
      )}

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogTitle>
            Delete {selected.size} file{selected.size === 1 ? '' : 's'}?
          </AlertDialogTitle>
          <AlertDialogDescription>
            Files are moved to the bin, not erased — they can be recovered from{' '}
            <span className="font-mono">data/.trash/</span> until the bin is
            emptied. A plain "Localize media" run won't re-fetch them; only a
            full rescan will.
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

function GalleryThumb({
  file,
  selectMode = false,
  selected = false,
  onToggleSelect,
  onOpen,
}: {
  file: GalleryFile
  /** Batch-select mode -- the Gallery pane's own, unlike expressions (which
   *  has no batch delete and just renders this with the mode left off). */
  selectMode?: boolean
  selected?: boolean
  onToggleSelect?: () => void
  onOpen: () => void
}) {
  return (
    <button
      type="button"
      onClick={selectMode ? onToggleSelect : onOpen}
      className={cn(
        'relative aspect-square overflow-hidden rounded-lg border border-line-soft bg-raised hover:border-sage-line',
        selected && 'ring-2 ring-sage',
      )}
    >
      {selectMode && (
        <span
          className={cn(
            'absolute top-1.5 left-1.5 z-1 grid size-5 place-items-center rounded-[6px] border backdrop-blur-[6px]',
            selected
              ? 'border-sage bg-sage text-on-sage'
              : 'border-white/30 bg-ground/72 text-transparent',
          )}
        >
          <Check className="size-3" />
        </span>
      )}
      {file.thumb_url ? (
        <img
          src={file.thumb_url}
          alt=""
          loading="lazy"
          className="size-full object-cover"
        />
      ) : (
        <span className="grid size-full place-items-center text-[11px] text-faint uppercase">
          {file.kind}
        </span>
      )}
    </button>
  )
}

// ---- Expressions -------------------------------------------------------------

/**
 * A character's expression sprites, grouped by parsed label (`neutral`
 * first). `Download all` streams the flat zip ST's own *Import Expressions
 * Pack* button expects, and `MediaUpload` takes that same shape back in
 * (docs/FORKS_AND_EXTRAS_PLAN.md §9) -- an export round-trips.
 */
export function ExpressionsPane({ card }: { card: CardDetail }) {
  const expressions = useExpressionFiles(
    card.expressions.folder,
    card.expressions.exists,
  )
  const [open, setOpen] = useState<number | null>(null)
  const files = expressions.data?.items ?? []

  const groups = groupExpressions(files)
  const flat = groups.flatMap((group) => group.files)

  // Above the conditional body and never inside it -- see `GalleryPane` for
  // why the position has to be fixed.
  return (
    <>
      <MediaUpload kind="expressions" folder={card.expressions.folder} zip />
      {!card.expressions.exists ? (
        <EmptyState>No expression sprites for this character.</EmptyState>
      ) : expressions.isPending ? (
        <p className="mt-4 text-center text-faint">reading expressions…</p>
      ) : files.length === 0 ? (
        <EmptyState>The expressions folder is empty.</EmptyState>
      ) : (
        <Section
          title={`${files.length} sprites · ${groups.length} expressions`}
          count={formatBytes(expressions.data!.bytes)}
          action={
            card.expressions_zip_url && (
              <a
                href={card.expressions_zip_url}
                download
                className="flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 text-[12px] text-muted hover:border-sage-line hover:text-sage"
              >
                <Download className="size-3" /> Download all
              </a>
            )
          }
        >
          <div className="mt-2.5 flex flex-col gap-5">
            {groups.map((group) => (
              <div key={group.label}>
                <h4 className="mb-2 text-[11px] font-semibold tracking-[0.08em] text-faint uppercase">
                  {group.label}{' '}
                  <span className="text-faint/70 normal-case">
                    ({group.files.length})
                  </span>
                </h4>
                <div className="grid grid-cols-[repeat(auto-fill,minmax(96px,1fr))] gap-2">
                  {group.files.map((file) => (
                    <GalleryThumb
                      key={file.name}
                      file={file}
                      onOpen={() => setOpen(flat.indexOf(file))}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
          {open !== null && (
            <Lightbox
              files={flat}
              index={open}
              onIndex={setOpen}
              onClose={() => setOpen(null)}
            />
          )}
        </Section>
      )}
    </>
  )
}

// ---- Related ---------------------------------------------------------------

export function RelatedPane({ card }: { card: CardDetail }) {
  // The most specific tag is the best single "shares tags" signal — the last
  // one, since importers tend to lead with broad tags (Female, SFW) and end on
  // the character-specific ones. The list route ANDs tags, so we send just one.
  const tag = card.tags.at(-1)
  const sameCreator = useSameCreator(card.creator, card.id)
  const sharesTag = useSharesTag(tag, card.id)

  // Flattened lineage (docs/FORKS_AND_EXTRAS_PLAN.md §3): a card that is
  // itself a fork points `useForksOf` at its own root, not at itself, so
  // "forks of this card" always means "every sibling", at any depth.
  const rootFragment = card.is_fork
    ? str(extension(card.card, 'fork'), 'of')
    : card.fragment
  const forksOf = useForksOf(rootFragment, card.id)
  const parent = useCharacterDetail(card.forked_from?.id)

  const creatorCards = sameCreator.data ?? []
  const tagCards = (sharesTag.data ?? []).filter(
    (other) => !creatorCards.some((c) => c.id === other.id),
  )
  const forkCards = forksOf.data ?? []

  return (
    <>
      {card.is_fork && (
        <Section title="Forked from">
          {parent.data ? (
            <CardGrid className="pt-2.5">
              <CardTile card={parent.data.card} />
            </CardGrid>
          ) : (
            <EmptyState>The original is no longer in the archive.</EmptyState>
          )}
        </Section>
      )}
      {forkCards.length > 0 && (
        <Section title={`Forks of this card (${forkCards.length})`}>
          <CardGrid className="pt-2.5">
            {forkCards.map((c) => (
              <CardTile key={c.id} card={c} />
            ))}
          </CardGrid>
        </Section>
      )}
      <Section title={`Same creator · ${card.creator || 'unknown'}`}>
        {creatorCards.length ? (
          <CardGrid className="pt-2.5">
            {creatorCards.map((c) => (
              <CardTile key={c.id} card={c} />
            ))}
          </CardGrid>
        ) : (
          <EmptyState>No other cards by this creator.</EmptyState>
        )}
      </Section>
      {tag && (
        <Section title={`Shares the tag “${tag}”`}>
          {tagCards.length ? (
            <CardGrid className="pt-2.5">
              {tagCards.slice(0, 12).map((c) => (
                <CardTile key={c.id} card={c} />
              ))}
            </CardGrid>
          ) : (
            <EmptyState>Nothing else carries that tag.</EmptyState>
          )}
        </Section>
      )}
    </>
  )
}

// ---- Info ------------------------------------------------------------------

export function InfoPane({
  card,
  etag,
  onAvatarReplaced,
}: {
  card: CardDetail
  etag: string | null
  onAvatarReplaced: () => void
}) {
  const data = card.card
  return (
    <>
      <Section title="Card">
        <InfoRows
          rows={[
            ['File', card.id, true],
            ['Card id', card.card_id, true],
            ['Gallery id', card.gallery_id || '—', true],
            ['Spec', `${card.spec} ${card.spec_version}`.trim() || '—'],
            ['Size on disk', formatBytes(card.size), true],
            [
              'Image',
              <AvatarSize
                key="i"
                card={card}
                etag={etag}
                onReplaced={onAvatarReplaced}
              />,
              true,
            ],
          ]}
        />
      </Section>
      <Section title="Provenance">
        <InfoRows
          rows={[
            ['Source', sourceLabel(card.source_kind)],
            // The title the card was listed under upstream, which is often not
            // the character's name -- kept whether or not it repeats the name,
            // unlike Overview's tagline, because here it is a provenance record
            // and "the same as the name" is itself the answer.
            ['Listing name', card.page_name || '—'],
            ['Creator', card.creator || 'unknown'],
            ['Created', formatDate(card.create_date), true],
            ['Added to archive', formatDate(card.linked_at), true],
            ['Version', card.character_version || '—', true],
            card.source_url
              ? ['Link', <SourceLink key="l" url={card.source_url} />]
              : null,
          ]}
        />
      </Section>
      <Section title="Content">
        <InfoRows
          rows={[
            ['Description', formatTokens(card.description_chars), true],
            ['Prompt weight', formatTokens(card.prompt_chars), true],
            ['Greetings', String(card.greetings), true],
            ['Lore entries', String(card.lore_entries), true],
            ['Example dialogue', card.has_example_dialogue ? 'yes' : 'none'],
          ]}
        />
      </Section>
      <Section title="card.json" action={<CopyJsonButton data={data} />}>
        <ProseBox className="max-h-[420px] overflow-auto font-mono text-[12.5px] leading-[1.6]">
          {JSON.stringify(data, null, 2)}
        </ProseBox>
      </Section>
    </>
  )
}

function CopyJsonButton({ data }: { data: unknown }) {
  const [copied, setCopied] = useState(false)

  return (
    <button
      type="button"
      onClick={() => {
        void copyText(JSON.stringify(data, null, 2)).then((ok) => {
          if (!ok) return toast('Could not copy to the clipboard.', 'bad')
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        })
      }}
      className="flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11.5px] text-muted hover:text-text"
    >
      {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
      {copied ? 'Copied' : 'Copy to Clipboard'}
    </button>
  )
}

type Row = [label: string, value: React.ReactNode, mono?: boolean] | null

function InfoRows({ rows }: { rows: Row[] }) {
  return (
    <div className="mt-2.5 grid grid-cols-[repeat(auto-fit,minmax(250px,1fr))] gap-x-[26px]">
      {rows
        .filter((row): row is [string, React.ReactNode, boolean?] => !!row)
        .map(([label, value, mono]) => (
          <div
            key={label}
            className="flex items-baseline gap-3 border-b border-line-soft px-0.5 py-[9px]"
          >
            <span className="min-w-[118px] text-[12.5px] text-faint">
              {label}
            </span>
            <b
              className={
                'ml-auto text-right text-[13.5px] font-medium' +
                (mono ? ' font-mono break-all' : '')
              }
            >
              {value}
            </b>
          </div>
        ))}
    </div>
  )
}

function SourceLink({ url }: { url: string }) {
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer noopener"
      className="text-sage hover:underline"
    >
      open ↗
    </a>
  )
}
