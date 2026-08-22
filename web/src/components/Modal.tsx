import { ReactNode, useEffect, useRef } from 'react'
import { useBackDismiss } from '../hooks/useBackDismiss'

interface ModalProps {
  /** Content of the modal body */
  children: ReactNode
  /** Called when the overlay or an explicit close action fires */
  onClose: () => void
  /** Extra className for the inner .run-modal div */
  className?: string
}

/**
 * Generic overlay modal.
 * - Click outside (overlay) → onClose
 * - Escape key → onClose
 * - Android back gesture → onClose (every dialog in the app routes through here, so wiring
 *   it once means Back never jumps straight out of the app with a dialog still open)
 */
export function Modal({ children, onClose, className }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  useBackDismiss(true, onClose)

  // Close on Escape
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="run-modal-overlay"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        className={`run-modal${className ? ' ' + className : ''}`}
        onClick={e => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  )
}

interface ModalHeadProps {
  title: ReactNode
  onClose: () => void
  extra?: ReactNode
}

export function ModalHead({ title, onClose, extra }: ModalHeadProps) {
  return (
    <div className="run-modal-head">
      <span>{title}</span>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        {extra}
        <button
          className="run-modal-close"
          onClick={onClose}
          aria-label="Close"
        >✕</button>
      </div>
    </div>
  )
}
