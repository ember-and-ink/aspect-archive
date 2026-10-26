import { AlertDialog as A } from 'radix-ui'
import { cn } from '@/lib/utils'

/**
 * The confirm dialog, on radix's AlertDialog — used for delete, the one archive
 * action with no undo the user reaches from this app. Styled to the mock's
 * surfaces rather than pulled from a registry.
 */
export const AlertDialog = A.Root
export const AlertDialogTrigger = A.Trigger
export const AlertDialogCancel = A.Cancel
export const AlertDialogAction = A.Action

export function AlertDialogContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof A.Content>) {
  return (
    <A.Portal>
      <A.Overlay className="fixed inset-0 z-[90] bg-black/55 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
      <A.Content
        className={cn(
          'fixed top-1/2 left-1/2 z-[91] w-[min(440px,calc(100vw-32px))] -translate-x-1/2 -translate-y-1/2',
          'rounded-[15px] border border-line bg-surface p-5 shadow-[0_30px_80px_#000000c0]',
          'data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95',
          className,
        )}
        {...props}
      >
        {children}
      </A.Content>
    </A.Portal>
  )
}

export function AlertDialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof A.Title>) {
  return (
    <A.Title className={cn('font-serif text-[21px]', className)} {...props} />
  )
}

export function AlertDialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof A.Description>) {
  return (
    <A.Description
      className={cn('mt-2 text-[13.5px] leading-[1.5] text-muted', className)}
      {...props}
    />
  )
}

export function AlertDialogFooter({
  className,
  ...props
}: React.ComponentProps<'div'>) {
  return (
    <div
      className={cn('mt-5 flex justify-end gap-2.5', className)}
      {...props}
    />
  )
}
