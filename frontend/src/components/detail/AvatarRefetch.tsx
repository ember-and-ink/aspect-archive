import { useState } from 'react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import type { CardDetail } from '@/hooks/use-character-detail'
import { useRefetchAvatar } from '@/hooks/use-card-mutations'
import { toast } from '@/lib/toast'

/** Mirrors `LOW_RES_PX` in proxy/cards/avatar_refetch.py. */
const LOW_RES_PX = 250

const ORIGIN_LABELS = {
  chub: 'Chub',
  datacat: 'the original JanitorAI / saucepan upload, via DataCat',
} as const

/**
 * The Info pane's Image row: the portrait's pixel size, and — when the card
 * carries an id its source can resolve — a quiet way to re-download the
 * original. Deliberately here rather than beside Replace image: it is a repair
 * for the odd thumbnail-sized card, not something a card normally needs.
 */
export function AvatarSize({
  card,
  etag,
  onReplaced,
}: {
  card: CardDetail
  etag: string | null
  onReplaced: () => void
}) {
  const refetch = useRefetchAvatar(card.id)
  const [open, setOpen] = useState(false)
  const { avatar_width: w, avatar_height: h, avatar_origin: origin } = card
  const lowRes = w > 0 && h > 0 && w < LOW_RES_PX && h < LOW_RES_PX

  const onConfirm = () =>
    refetch.mutate(
      { etag },
      {
        onSuccess: ({ card: next }) => {
          toast(
            `Image re-fetched: ${w}×${h} → ${next.avatar_width}×${next.avatar_height}.`,
          )
          onReplaced()
        },
        onError: (error) => toast(error.message, 'bad'),
      },
    )

  return (
    <span className="inline-flex items-baseline gap-2">
      <span className={lowRes ? 'text-bad' : undefined}>
        {w && h ? `${w} × ${h}` : '—'}
      </span>
      {origin && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          disabled={refetch.isPending}
          className="font-sans text-[12px] font-normal text-sage hover:underline disabled:opacity-60"
        >
          {refetch.isPending ? 're-fetching…' : 're-fetch original'}
        </button>
      )}
      {origin && (
        <AlertDialog open={open} onOpenChange={setOpen}>
          <AlertDialogContent>
            <AlertDialogTitle>Re-fetch the original image?</AlertDialogTitle>
            <AlertDialogDescription>
              Downloads the full-size image from {ORIGIN_LABELS[origin]} and
              replaces this card’s portrait with it. Only the image changes —
              the card’s text is kept exactly as it is. You get the source’s
              current art, which may differ if the creator has changed it. The
              card is left alone unless the download is larger than the {w}×{h}{' '}
              image it has now.
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
                  onClick={onConfirm}
                  className="rounded-[10px] border border-sage bg-sage px-3.5 py-2 text-[13px] font-semibold text-on-sage hover:bg-[#68d0b1]"
                >
                  Re-fetch
                </button>
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </span>
  )
}
