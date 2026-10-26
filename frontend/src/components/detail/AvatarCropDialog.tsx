import { useCallback, useEffect, useState } from 'react'
import { Dialog } from 'radix-ui'
import { X } from 'lucide-react'
import Cropper, { type Area } from 'react-easy-crop'
import { cropImageToBlob } from '@/lib/crop-image'

/** The card portrait's own shape -- the same 2:3 ratio SillyTavern's upload cropper locks to. */
const CARD_ASPECT = 2 / 3

/**
 * Steps between picking a file and uploading it: crop to the card's portrait
 * shape first, the way SillyTavern's own `popup-crop-wrap` does, so a replaced
 * avatar frames the same way the rest of the archive's cards do instead of
 * whatever rectangle the source image happened to be.
 */
export function AvatarCropDialog({
  file,
  open,
  onOpenChange,
  onCropped,
}: {
  file: File | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onCropped: (file: File) => void
}) {
  const [imageSrc, setImageSrc] = useState<string | null>(null)
  const [crop, setCrop] = useState({ x: 0, y: 0 })
  const [zoom, setZoom] = useState(1)
  const [area, setArea] = useState<Area | null>(null)
  const [working, setWorking] = useState(false)

  // A fresh object URL (and a reset crop/zoom) each time a new file comes in,
  // so reopening the picker after a cancel never shows the previous image.
  useEffect(() => {
    if (!file) {
      setImageSrc(null)
      return
    }
    const url = URL.createObjectURL(file)
    setImageSrc(url)
    setCrop({ x: 0, y: 0 })
    setZoom(1)
    setArea(null)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const onCropComplete = useCallback((_area: Area, pixels: Area) => {
    setArea(pixels)
  }, [])

  const onConfirm = async () => {
    if (!imageSrc || !area || !file) return
    setWorking(true)
    try {
      const blob = await cropImageToBlob(imageSrc, area)
      const name = file.name.replace(/\.\w+$/, '') || 'avatar'
      onCropped(new File([blob], `${name}.png`, { type: 'image/png' }))
      onOpenChange(false)
    } finally {
      setWorking(false)
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[90] bg-black/55 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
        <Dialog.Content className="fixed top-1/2 left-1/2 z-[91] w-[min(480px,calc(100vw-32px))] -translate-x-1/2 -translate-y-1/2 rounded-[15px] border border-line bg-surface p-5 shadow-[0_30px_80px_#000000c0] data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95">
          <div className="flex items-center justify-between">
            <Dialog.Title className="font-serif text-[21px]">
              Crop portrait
            </Dialog.Title>
            <Dialog.Close className="grid size-7 place-items-center rounded-full text-faint hover:bg-raised hover:text-text">
              <X className="size-4" />
            </Dialog.Close>
          </div>
          <Dialog.Description className="mt-1 text-[12.5px] text-faint">
            Drag to reposition, scroll to zoom.
          </Dialog.Description>

          {imageSrc && (
            <div className="relative mt-4 h-[360px] w-full overflow-hidden rounded-xl bg-black/40">
              <Cropper
                image={imageSrc}
                crop={crop}
                zoom={zoom}
                aspect={CARD_ASPECT}
                onCropChange={setCrop}
                onZoomChange={setZoom}
                onCropComplete={onCropComplete}
              />
            </div>
          )}

          <input
            type="range"
            min={1}
            max={3}
            step={0.01}
            value={zoom}
            onChange={(event) => setZoom(Number(event.target.value))}
            className="mt-4 w-full accent-[var(--sage)]"
            aria-label="Zoom"
          />

          <div className="mt-5 flex justify-end gap-2.5">
            <Dialog.Close asChild>
              <button
                type="button"
                className="rounded-[10px] border border-line px-3.5 py-2 text-[13px] hover:bg-raised"
              >
                Cancel
              </button>
            </Dialog.Close>
            <button
              type="button"
              onClick={onConfirm}
              disabled={!area || working}
              className="rounded-[10px] border border-sage bg-sage px-3.5 py-2 text-[13px] font-semibold text-on-sage hover:bg-[#68d0b1] disabled:cursor-not-allowed disabled:opacity-60"
            >
              {working ? 'Cropping…' : 'Use crop'}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
