import { useCallback, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import type { GalleryFile } from '@/lib/card'

/**
 * A full-bleed image viewer over the gallery.
 *
 * Its own keyboard nav (~40 lines) rather than a carousel dependency, exactly
 * as the plan's primitive map calls for (§4.2). Arrow keys and Escape only make
 * sense while it is open, so the listener is bound only then and captures
 * arrows before the detail page's J/K handler can also see them.
 */
export function Lightbox({
  files,
  index,
  onIndex,
  onClose,
}: {
  files: GalleryFile[]
  index: number
  onIndex: (index: number) => void
  onClose: () => void
}) {
  const step = useCallback(
    (delta: number) => {
      const next = index + delta
      if (next >= 0 && next < files.length) onIndex(next)
    },
    [index, files.length, onIndex],
  )

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      // Arrows step the gallery. J/K are swallowed rather than acted on: they
      // page the *card* underneath, and doing that while a lightbox is open
      // leaves the viewer showing images that belong to a card that is no
      // longer on screen.
      if (event.key === 'j' || event.key === 'k') {
        event.stopPropagation()
        return
      }
      if (event.key === 'ArrowRight') {
        event.stopPropagation()
        step(1)
      } else if (event.key === 'ArrowLeft') {
        event.stopPropagation()
        step(-1)
      }
    }
    // Capture phase so the detail page's window-level J/K handler does not also
    // fire while the lightbox owns the keyboard.
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [step, onClose])

  // The page scrolls inside a container in `AppShell`, and the detail route
  // stacks a fixed top bar over a sticky back-bar. Rendering the viewer inline
  // left it competing with those for stacking order, so a tall image showed
  // *through* and under them. A portal to <body> takes it out of that subtree
  // entirely, and `z-60` clears both bars (Stage 6B D3).
  const file = files[index]
  if (!file) return null

  return createPortal(
    <div
      className="fixed inset-0 z-60 flex items-center justify-center bg-black/88"
      onClick={onClose}
    >
      <button
        type="button"
        onClick={onClose}
        className="absolute top-4 right-4 grid size-10 place-items-center rounded-full bg-white/8 text-text hover:bg-white/16"
        aria-label="Close"
      >
        <X className="size-5" />
      </button>

      {index > 0 && (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            step(-1)
          }}
          className="absolute left-4 grid size-11 place-items-center rounded-full bg-white/8 text-text hover:bg-white/16"
          aria-label="Previous"
        >
          <ChevronLeft className="size-6" />
        </button>
      )}
      {index < files.length - 1 && (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            step(1)
          }}
          className="absolute right-4 grid size-11 place-items-center rounded-full bg-white/8 text-text hover:bg-white/16"
          aria-label="Next"
        >
          <ChevronRight className="size-6" />
        </button>
      )}

      {file.kind === 'video' ? (
        <video
          src={file.url}
          controls
          className="max-h-[90vh] max-w-[92vw] rounded-lg"
          onClick={(event) => event.stopPropagation()}
        />
      ) : (
        <img
          src={file.url}
          alt={file.name}
          className="max-h-[90vh] max-w-[92vw] rounded-lg object-contain"
          onClick={(event) => event.stopPropagation()}
        />
      )}

      <span className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full bg-black/60 px-3 py-1 font-mono text-[12px] text-muted">
        {index + 1} / {files.length}
      </span>
    </div>,
    document.body,
  )
}
