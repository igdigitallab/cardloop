/**
 * ProjectNameDialog — shared dialog used for:
 *   - Rename: pre-fills the current label, validates 1–80 chars, calls onSubmit.
 *   - Create: single text field, empty is allowed (scratch project).
 *
 * Uses the existing Modal + ModalHead so the mobile bottom-sheet CSS
 * (run-modal-overlay + .run-modal at ≤640px → bottom sheet) works automatically.
 */
import { useEffect, useRef, useState } from 'react'
import { Modal, ModalHead } from './Modal'
import { t } from '../i18n'

interface Props {
  mode: 'rename' | 'create'
  /** Initial value (pre-fill for rename; empty string for create). */
  initialValue?: string
  onSubmit: (name: string) => Promise<void>
  onClose: () => void
}

export function ProjectNameDialog({ mode, initialValue = '', onSubmit, onClose }: Props) {
  const [value, setValue] = useState(initialValue)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    // Focus + select-all so the user can immediately type a replacement
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [])

  const title = mode === 'rename' ? t['project.rename_label_title'] : t['project.create_title']
  const placeholder = mode === 'rename'
    ? t['project.rename_label_placeholder']
    : t['project.create_placeholder']
  const submitLabel = mode === 'rename' ? t['project.rename_label_save'] : t['project.create_btn']
  const cancelLabel = mode === 'rename' ? t['project.rename_label_cancel'] : t['project.create_cancel']

  function validate(v: string): string | null {
    if (mode === 'rename' && v.trim() === '') return t['project.rename_label_error_empty']
    if (v.length > 80) return t['project.rename_label_error_long']
    return null
  }

  async function handleSubmit() {
    const trimmed = value.trim()
    const err = validate(trimmed)
    if (err) { setError(err); return }
    setBusy(true)
    setError('')
    try {
      await onSubmit(trimmed)
      onClose()
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      if (status === 409) {
        setError(t['project.rename_label_error_busy'])
      } else {
        setError(e instanceof Error ? e.message : String(e))
      }
      setBusy(false)
    }
  }

  return (
    <Modal onClose={onClose}>
      <ModalHead title={title} onClose={onClose} />
      <div className="run-modal-body" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <input
          ref={inputRef}
          className="rename-input"
          style={{
            width: '100%',
            fontSize: 15,
            padding: '8px 10px',
            boxSizing: 'border-box',
            minHeight: 44,
          }}
          type="text"
          value={value}
          placeholder={placeholder}
          onChange={e => { setValue(e.target.value); setError('') }}
          onKeyDown={e => {
            if (e.key === 'Enter') { e.preventDefault(); void handleSubmit() }
            if (e.key === 'Escape') { e.preventDefault(); onClose() }
          }}
          disabled={busy}
          maxLength={80}
          aria-label={title}
        />
        {error && (
          <div style={{ fontSize: 12, color: 'var(--red)' }}>{error}</div>
        )}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button
            className="btn-secondary"
            onClick={onClose}
            disabled={busy}
            style={{ minHeight: 44, minWidth: 80, fontSize: 14 }}
          >
            {cancelLabel}
          </button>
          <button
            className="btn-primary"
            onClick={() => void handleSubmit()}
            disabled={busy}
            style={{ minHeight: 44, minWidth: 80, fontSize: 14, width: 'auto' }}
          >
            {busy ? '…' : submitLabel}
          </button>
        </div>
      </div>
    </Modal>
  )
}
