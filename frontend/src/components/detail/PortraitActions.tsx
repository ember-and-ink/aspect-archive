import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import {
  Copy,
  FileUp,
  GitFork,
  ImageUp,
  Pencil,
  Star,
  Trash2,
} from 'lucide-react'
import type { CardDetail } from '@/hooks/use-character-detail'
import {
  useDeleteCharacter,
  useForkCharacter,
  useImportFork,
  useReplaceAvatar,
  useSetFavorite,
} from '@/hooks/use-card-mutations'
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { RenameDialog } from './RenameDialog'
import { AvatarCropDialog } from './AvatarCropDialog'

/**
 * The portrait's own actions: favourite, replace image, rename, delete. These
 * sit outside the section edit model — a star is a targeted write, the avatar
 * is a separate endpoint, and name/creator have no section of their own for an
 * inline editor to expand into (the header stays clean, read-only prose; the
 * rename affordance lives down here instead).
 *
 * The mock's "⋯ More · export, duplicate, delete" is gone (Stage 6B D5). Of its
 * three promised items, Export *is* the Download button above and Duplicate has
 * no endpoint, so the menu had been wrapping a single Delete in an extra click
 * for two stages. Delete is now a plain button; the confirm dialog, which is
 * what actually guards the action, is unchanged.
 */
export function PortraitActions({
  card,
  etag,
  onAvatarReplaced,
  autoOpenRename = false,
}: {
  card: CardDetail
  etag: string | null
  onAvatarReplaced: () => void
  /** Open "Rename character" pre-focused, right after a fork — see
   *  CharacterDetailPage, which is also what clears this once consumed. */
  autoOpenRename?: boolean
}) {
  const navigate = useNavigate()
  const location = useLocation()
  const favorite = useSetFavorite(card.id)
  const avatar = useReplaceAvatar(card.id)
  const remove = useDeleteCharacter(card.id)
  const fork = useForkCharacter(card.id)
  const importFork = useImportFork(card.fragment)
  const fileRef = useRef<HTMLInputElement>(null)
  const forkFileRef = useRef<HTMLInputElement>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [alsoGallery, setAlsoGallery] = useState(false)
  const [renaming, setRenaming] = useState<'name' | 'creator' | null>(null)
  const [cropFile, setCropFile] = useState<File | null>(null)
  const [cropOpen, setCropOpen] = useState(false)

  useEffect(() => {
    if (!autoOpenRename) return
    setRenaming('name')
    // Consume it: an unrelated re-render (a favourite toggle, a tab change)
    // must not reopen the dialog, and neither should a later back/forward to
    // this same history entry.
    navigate(`${location.pathname}${location.search}`, { replace: true })
    // Deliberately just `autoOpenRename` — `navigate`/`location` are stable
    // per render from the router and re-running this because *they* changed
    // (e.g. a tab switch) would reopen the dialog on its own.
  }, [autoOpenRename]) // eslint-disable-line react-hooks/exhaustive-deps

  const onFile = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = '' // let the same file be picked again after a failure
    if (!file) return
    setCropFile(file)
    setCropOpen(true)
  }

  const onCropped = (file: File) => {
    avatar.mutate(
      { file, etag },
      {
        onSuccess: () => {
          toast('Portrait replaced.')
          onAvatarReplaced()
        },
        onError: (error) => toast(error.message, 'bad'),
      },
    )
  }

  const onFork = () =>
    fork.mutate(undefined, {
      onSuccess: (created) => {
        toast(`Forked as “${created.name}”.`)
        navigate(
          {
            pathname: `/characters/${encodeURIComponent(created.id)}`,
            search: location.search,
          },
          { state: { openRename: true } },
        )
      },
      onError: (error) => toast(error.message, 'bad'),
    })

  // A rewrite made outside the archive (in ST, by hand) handed back as a PNG.
  // Unlike a born-here fork it already has its own name, so no rename prompt.
  const onForkFile = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = '' // let the same file be picked again after a failure
    if (!file) return
    importFork.mutate(file, {
      onSuccess: (result) => {
        toast(
          result.duplicate
            ? `“${result.name}” is already in the archive.`
            : `Added “${result.name}” as a fork.`,
        )
        navigate({
          pathname: `/characters/${encodeURIComponent(result.id)}`,
          search: location.search,
        })
      },
      onError: (error) => toast(error.message, 'bad'),
    })
  }

  const forking = fork.isPending || importFork.isPending

  const onDelete = () =>
    remove.mutate(alsoGallery ? 'delete' : 'keep', {
      onSuccess: () => {
        toast(`Moved “${card.name}” to the bin.`)
        navigate('/')
      },
      onError: (error) => toast(error.message, 'bad'),
    })

  return (
    <>
      <button
        type="button"
        onClick={() => favorite.mutate(!card.favorite)}
        className={cn(
          'flex h-[38px] items-center justify-center gap-2 rounded-[10px] border text-[13.5px] font-medium hover:bg-raised',
          card.favorite
            ? 'border-sage-line text-sage'
            : 'border-line text-text',
        )}
      >
        <Star className={cn('size-4', card.favorite && 'fill-current')} />
        {card.favorite ? 'Favourited' : 'Favourite'}
      </button>

      <button
        type="button"
        onClick={() => fileRef.current?.click()}
        disabled={avatar.isPending}
        className="flex h-[38px] items-center justify-center gap-2 rounded-[10px] border border-line text-[13.5px] font-medium text-text hover:bg-raised disabled:opacity-60"
      >
        <ImageUp className="size-4" />
        {avatar.isPending ? 'Replacing…' : 'Replace image'}
      </button>
      <input
        ref={fileRef}
        type="file"
        accept="image/*"
        hidden
        onChange={onFile}
      />
      <AvatarCropDialog
        file={cropFile}
        open={cropOpen}
        onOpenChange={setCropOpen}
        onCropped={onCropped}
      />

      <button
        type="button"
        onClick={() => setRenaming('name')}
        className="flex h-[34px] items-center justify-center gap-2 rounded-[10px] text-[12.5px] text-faint hover:bg-raised hover:text-sage"
      >
        <Pencil className="size-3.5" /> Rename character
      </button>
      <button
        type="button"
        onClick={() => setRenaming('creator')}
        className="flex h-[34px] items-center justify-center gap-2 rounded-[10px] text-[12.5px] text-faint hover:bg-raised hover:text-sage"
      >
        <Pencil className="size-3.5" /> Rename creator
      </button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            disabled={forking}
            className="flex h-[34px] items-center justify-center gap-2 rounded-[10px] text-[12.5px] text-faint hover:bg-raised hover:text-sage disabled:opacity-60"
          >
            <GitFork className="size-3.5" />
            {forking ? 'Forking…' : 'Fork this card'}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="center">
          <DropdownMenuItem onSelect={onFork}>
            <Copy className="size-4" /> Copy this card
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => forkFileRef.current?.click()}>
            <FileUp className="size-4" /> Upload an edited PNG…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <input
        ref={forkFileRef}
        type="file"
        accept="image/png"
        hidden
        onChange={onForkFile}
      />

      <button
        type="button"
        onClick={() => {
          setAlsoGallery(false)
          setConfirmOpen(true)
        }}
        className="flex h-[34px] items-center justify-center gap-2 rounded-[10px] text-[12.5px] text-faint hover:bg-raised hover:text-bad"
      >
        <Trash2 className="size-4" /> Delete card
      </button>

      <RenameDialog
        field="name"
        title="Rename character"
        value={card.name}
        open={renaming === 'name'}
        onOpenChange={(next) => setRenaming(next ? 'name' : null)}
      />
      <RenameDialog
        field="creator"
        title="Rename creator"
        value={card.creator}
        allowEmpty
        open={renaming === 'creator'}
        onOpenChange={(next) => setRenaming(next ? 'creator' : null)}
      />

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogTitle>Delete “{card.name}”?</AlertDialogTitle>
          <AlertDialogDescription>
            The card is moved to the bin, not erased — it can be recovered from{' '}
            <span className="font-mono">data/.trash/</span> until the bin is
            emptied.
          </AlertDialogDescription>
          {card.gallery.exists && (
            <label className="mt-3.5 flex items-center gap-2.5 rounded-lg border border-line-soft bg-raised px-3 py-2.5 text-[13px]">
              <input
                type="checkbox"
                checked={alsoGallery}
                onChange={(e) => setAlsoGallery(e.target.checked)}
                className="size-4 accent-[var(--sage)]"
              />
              Delete its gallery folder too
            </label>
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
                onClick={onDelete}
                disabled={remove.isPending}
                className="rounded-[10px] border border-bad bg-bad/90 px-3.5 py-2 text-[13px] font-semibold text-white hover:bg-bad disabled:opacity-60"
              >
                {remove.isPending ? 'Deleting…' : 'Delete'}
              </button>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
