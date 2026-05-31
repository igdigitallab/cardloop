/**
 * Lightweight toast notification system.
 * Usage:
 *   const { toasts, showToast } = useToast()
 *   <ToastContainer toasts={toasts} />
 *   showToast('Something went wrong', 'error')
 */
import { useState, useCallback } from 'react'

export interface Toast {
  id: string
  message: string
  kind: 'error' | 'info' | 'success'
}

let _toastCounter = 0

export function useToast() {
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: string) => {
    setToasts(prev => prev.filter(t => t.id !== id))
  }, [])

  const showToast = useCallback((message: string, kind: Toast['kind'] = 'error', durationMs = 6000) => {
    const id = `toast-${++_toastCounter}`
    setToasts(prev => [...prev, { id, message, kind }])
    setTimeout(() => dismiss(id), durationMs)
  }, [dismiss])

  return { toasts, showToast, dismiss }
}

export function ToastContainer({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: string) => void }) {
  if (toasts.length === 0) return null
  return (
    <div className="toast-container" aria-live="polite" aria-atomic="false">
      {toasts.map(t => (
        <div key={t.id} className={`toast toast-${t.kind}`}>
          <span className="toast-msg">{t.message}</span>
          <button
            className="toast-close"
            onClick={() => onDismiss(t.id)}
            aria-label="Закрыть уведомление"
          >✕</button>
        </div>
      ))}
    </div>
  )
}
