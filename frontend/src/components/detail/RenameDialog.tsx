import { useEffect, useState } from 'react'
import { Dialog } from 'radix-ui'
import { X } from 'lucide-react'
import { setField } from '@/lib/card-edit'
import { useEdit } from './edit-context'

/**
 * A one-field rename, for `name` and `creator` -- the two identity fields the
 * clean header/portrait layout deliberately has no inline edit affordance for
 * (docs/UI_REWRITE_PLAN.md keeps that area read-only prose). It reuses the
 * section editors' whole-document `save`, so the same ETag guard and stale-write
 * handling apply; unlike the section editors it is a modal rather than inline,
 * since neither field has a "section" of its own to expand into.
 *
 * `name` has no sane empty value -- the server 422s a blank one (it is what the
 * card is called everywhere) -- but `creator` may be cleared to "unknown".
 */
export function RenameDialog({
  field,
  title,
  value,
  allowEmpty,
  open,
  onOpenChange,
}: {
  field: 'name' | 'creator'
  title: string
  value: string
  allowEmpty?: boolean
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { data, save, isSaving } = useEdit()
  const [draft, setDraft] = useState(value)

  // Reset to the current value each time the dialog opens, so a cancelled
  // edit never leaks into the next opening.
  useEffect(() => {
    if (open) setDraft(value)
  }, [open, value])

  const trimmed = draft.trim()
  const invalid = !allowEmpty && !trimmed
  const unchanged = trimmed === value.trim()

  const onSave = async () => {
    if (invalid || unchanged) return
    const ok = await save(setField(data, field, trimmed))
    if (ok) onOpenChange(false)
  }

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[90] bg-black/55 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
        <Dialog.Content className="fixed top-1/2 left-1/2 z-[91] w-[min(420px,calc(100vw-32px))] -translate-x-1/2 -translate-y-1/2 rounded-[15px] border border-line bg-surface p-5 shadow-[0_30px_80px_#000000c0] data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95">
          <div className="flex items-center justify-between">
            <Dialog.Title className="font-serif text-[21px]">
              {title}
            </Dialog.Title>
            <Dialog.Close className="grid size-7 place-items-center rounded-full text-faint hover:bg-raised hover:text-text">
              <X className="size-4" />
            </Dialog.Close>
          </div>
          <Dialog.Description className="sr-only">
            Change the card&rsquo;s {field}.
          </Dialog.Description>
          <input
            autoFocus
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void onSave()
              }
            }}
            className="mt-4 w-full rounded-xl border border-sage-line bg-raised px-[15px] py-[11px] text-[15px] text-text outline-none focus:border-sage"
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
              onClick={onSave}
              disabled={invalid || unchanged || isSaving}
              className="rounded-[10px] border border-sage bg-sage px-3.5 py-2 text-[13px] font-semibold text-on-sage hover:bg-[#68d0b1] disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isSaving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
