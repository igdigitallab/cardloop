import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { ProjectSecrets } from '../types'
import { Spinner } from '../components/Spinner'
import { ConfirmModal } from '../components/ConfirmModal'
import { Modal, ModalHead } from '../components/Modal'
import { t } from '../i18n'

interface Props {
  projectId: string
}

// ── Key validation (mirrors backend: ^[A-Z_][A-Z0-9_]*$) ─────────────────────
const KEY_RE = /^[A-Z_][A-Z0-9_]*$/

// ── Add modal ─────────────────────────────────────────────────────────────────

interface AddModalProps {
  projectId: string
  onSaved: (data: ProjectSecrets) => void
  onClose: () => void
}

function AddModal({ projectId, onSaved, onClose }: AddModalProps) {
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const keyRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    keyRef.current?.focus()
  }, [])

  async function doSave() {
    setError('')
    const k = key.trim().toUpperCase()
    if (!k) { setError('Key name is required'); return }
    if (!KEY_RE.test(k)) { setError(t['secrets.key_invalid']); return }
    if (!value) { setError(t['secrets.value_empty']); return }
    setSaving(true)
    try {
      const data = await api.setSecret(projectId, k, value)
      onSaved(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); doSave() }
    else if (e.key === 'Escape') { e.preventDefault(); onClose() }
  }

  return (
    <Modal onClose={onClose} className="memory-edit-modal">
      <ModalHead title={t['secrets.add_btn']} onClose={onClose} />
      <div className="run-modal-body" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <label style={{ fontSize: 13 }}>
          <span style={{ display: 'block', marginBottom: 4, color: 'var(--text2)' }}>
            {t['secrets.key_label']}
          </span>
          <input
            ref={keyRef}
            type="text"
            className="doc-textarea"
            style={{ height: 'auto', padding: '6px 8px', fontSize: 13, fontFamily: 'monospace', textTransform: 'uppercase' }}
            placeholder={t['secrets.key_placeholder']}
            value={key}
            onChange={e => setKey(e.target.value.toUpperCase())}
            onKeyDown={onKeyDown}
            aria-label={t['secrets.key_label']}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        <label style={{ fontSize: 13 }}>
          <span style={{ display: 'block', marginBottom: 4, color: 'var(--text2)' }}>
            {t['secrets.value_label']}
          </span>
          <input
            type="password"
            className="doc-textarea"
            style={{ height: 'auto', padding: '6px 8px', fontSize: 13, fontFamily: 'monospace' }}
            placeholder={t['secrets.value_placeholder']}
            value={value}
            onChange={e => setValue(e.target.value)}
            onKeyDown={onKeyDown}
            aria-label={t['secrets.value_label']}
            autoComplete="new-password"
            spellCheck={false}
          />
        </label>
        {error && <div className="error-state" style={{ fontSize: 12 }}>⚠ {error}</div>}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="doc-btn ghost" onClick={onClose} disabled={saving}>{t['secrets.cancel']}</button>
          <button className="doc-btn primary" onClick={doSave} disabled={saving}>
            {saving ? t['secrets.saving'] : t['secrets.save_btn']}
          </button>
        </div>
        <p style={{ margin: 0, fontSize: 11, color: 'var(--text3)' }}>
          Ctrl+Enter — save · Esc — cancel
        </p>
      </div>
    </Modal>
  )
}

// ── Main tab ──────────────────────────────────────────────────────────────────

export function SecretsTab({ projectId }: Props) {
  const [data, setData] = useState<ProjectSecrets | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showAddModal, setShowAddModal] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setData(null)

    api.secrets(projectId).then(d => {
      if (!cancelled) { setData(d); setLoading(false) }
    }).catch(e => {
      if (!cancelled) { setError(String(e.message || e)); setLoading(false) }
    })

    return () => { cancelled = true }
  }, [projectId])

  function onSaved(updated: ProjectSecrets) {
    setData(updated)
    setShowAddModal(false)
  }

  async function confirmDelete() {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      const updated = await api.deleteSecret(projectId, deleteTarget)
      setData(updated)
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setDeleting(false)
      setDeleteTarget(null)
    }
  }

  if (loading) return <Spinner label={t['secrets.loading']} />
  if (error) return <div className="error-state">⚠ {error}</div>

  const keys = data?.keys ?? []

  return (
    <>
      <div className="memory-empty" style={{ maxWidth: 600, margin: '0 auto' }}>
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16, width: '100%' }}>
          <span style={{ fontWeight: 600, fontSize: 14 }}>{t['secrets.list_label']}</span>
          <button
            className="doc-btn primary"
            style={{ padding: '4px 12px', fontSize: 12 }}
            onClick={() => setShowAddModal(true)}
            aria-label={t['secrets.add_btn_aria']}
          >
            {t['secrets.add_btn']}
          </button>
        </div>

        {/* Empty state */}
        {keys.length === 0 && (
          <div style={{ textAlign: 'center', padding: '32px 0' }}>
            <div className="memory-empty-icon">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
              </svg>
            </div>
            <div className="memory-empty-title">{t['secrets.empty_title']}</div>
            <p className="memory-empty-text">{t['secrets.empty_text']}</p>
          </div>
        )}

        {/* Keys list */}
        {keys.length > 0 && (
          <div style={{ width: '100%', marginBottom: 16 }}>
            {keys.map(k => (
              <div
                key={k}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: '10px 12px',
                  marginBottom: 6,
                  background: 'var(--surface2)',
                  borderRadius: 6,
                  fontFamily: 'monospace',
                  fontSize: 13,
                }}
                role="listitem"
                aria-label={`Secret: ${k}`}
              >
                <span style={{ fontWeight: 600, color: 'var(--text1)' }}>{k}</span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <span style={{ color: 'var(--text3)', fontSize: 12, letterSpacing: 2 }}>
                    {t['secrets.masked']}
                  </span>
                  <button
                    className="memory-action-btn memory-action-btn--danger"
                    title={t['secrets.delete_btn_aria']}
                    aria-label={`${t['secrets.delete_btn_aria']}: ${k}`}
                    onClick={() => setDeleteTarget(k)}
                    style={{ flexShrink: 0 }}
                  >
                    ✕
                  </button>
                </span>
              </div>
            ))}
          </div>
        )}

        {/* Hint */}
        <p className="memory-empty-note" style={{ textAlign: 'left', marginTop: 8 }}>
          💡 {t['secrets.hint']}
        </p>
      </div>

      {/* Add modal */}
      {showAddModal && (
        <AddModal
          projectId={projectId}
          onSaved={onSaved}
          onClose={() => setShowAddModal(false)}
        />
      )}

      {/* Delete confirm */}
      {deleteTarget && (
        <ConfirmModal
          title={t['secrets.confirm_delete_title']}
          message={t['secrets.confirm_delete_body']}
          confirmLabel={deleting ? '…' : t['secrets.confirm_delete_yes']}
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
          danger
        />
      )}
    </>
  )
}
