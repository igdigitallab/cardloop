import { useState, useEffect, useCallback, useRef } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { api } from '../api'
import { t } from '../i18n'
import { Modal, ModalHead } from '../components/Modal'
import { ConfirmModal } from '../components/ConfirmModal'

// ── Types ──────────────────────────────────────────────────────

interface SecretMeta {
  name: string
  category: string
}

interface SecretFull {
  name: string
  value: string
  category: string
  notes: string
  updated_at: string
}

interface ModalState {
  mode: 'add' | 'edit'
  name: string
  value: string
  category: string
  notes: string
  /** Original name — used to identify the record being edited */
  originalName?: string
}

// ── Helpers ────────────────────────────────────────────────────

function groupByCategory(secrets: SecretMeta[]): Map<string, SecretMeta[]> {
  const map = new Map<string, SecretMeta[]>()
  for (const s of secrets) {
    const key = s.category?.trim() || t['vault.uncategorized']
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(s)
  }
  // Sort: Uncategorized last, rest alphabetically
  const uncatLabel = t['vault.uncategorized']
  const sorted = new Map<string, SecretMeta[]>()
  const keys = [...map.keys()].sort((a, b) => {
    if (a === uncatLabel) return 1
    if (b === uncatLabel) return -1
    return a.localeCompare(b)
  })
  for (const k of keys) sorted.set(k, map.get(k)!)
  return sorted
}

// ── TOTP Enrollment Panel ──────────────────────────────────────

interface EnrollState {
  secret: string
  otpauth_uri: string
  recovery_codes: string[]
}

function TotpPanel({ onToast }: { onToast: (msg: string) => void }) {
  const [loading, setLoading] = useState(true)
  const [enabled, setEnabled] = useState(false)

  // Enrollment in-progress state
  const [enroll, setEnroll] = useState<EnrollState | null>(null)
  const [enrolling, setEnrolling] = useState(false)

  // Activation code input
  const [activateCode, setActivateCode] = useState('')
  const [activating, setActivating] = useState(false)
  const [activateError, setActivateError] = useState<string | null>(null)

  // Recovery codes copy feedback
  const [codesCopied, setCodesCopied] = useState(false)

  // Disable confirm modal
  const [showDisableConfirm, setShowDisableConfirm] = useState(false)

  const codeInputRef = useRef<HTMLInputElement>(null)

  // Load status on mount
  useEffect(() => {
    api.totpStatus()
      .then(res => setEnabled(res.enabled))
      .catch(() => {/* silently ignore — section will stay loading */})
      .finally(() => setLoading(false))
  }, [])

  const handleEnroll = useCallback(async () => {
    setEnrolling(true)
    try {
      const res = await api.totpEnroll()
      setEnroll({ secret: res.secret, otpauth_uri: res.otpauth_uri, recovery_codes: res.recovery_codes })
      setActivateCode('')
      setActivateError(null)
      setTimeout(() => codeInputRef.current?.focus(), 50)
    } catch (e: unknown) {
      onToast(e instanceof Error ? e.message : String(e))
    } finally {
      setEnrolling(false)
    }
  }, [onToast])

  const handleActivate = useCallback(async () => {
    if (!activateCode.trim()) return
    setActivating(true)
    setActivateError(null)
    try {
      await api.totpActivate(activateCode.trim())
      setEnabled(true)
      setEnroll(null)
      setActivateCode('')
      onToast(t['totp.enabled_toast'])
    } catch (e: unknown) {
      const apiErr = e as { status?: number; body?: { error?: string } }
      if (apiErr.status === 400 && apiErr.body?.error === 'totp_invalid') {
        setActivateError(t['totp.error_invalid'])
        setActivateCode('')
      } else {
        setActivateError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setActivating(false)
    }
  }, [activateCode, onToast])

  const handleDisable = useCallback(async () => {
    setShowDisableConfirm(false)
    try {
      await api.totpDisable()
      setEnabled(false)
      onToast(t['totp.disabled_toast'])
    } catch (e: unknown) {
      onToast(e instanceof Error ? e.message : String(e))
    }
  }, [onToast])

  const handleCopyRecovery = useCallback(async () => {
    if (!enroll) return
    try {
      await navigator.clipboard.writeText(enroll.recovery_codes.join('\n'))
      setCodesCopied(true)
      setTimeout(() => setCodesCopied(false), 2000)
    } catch {
      onToast('Copy failed')
    }
  }, [enroll, onToast])

  if (loading) {
    return (
      <div className="totp-panel totp-panel--loading">
        <span className="totp-status-label">{t['totp.loading']}</span>
      </div>
    )
  }

  return (
    <div className="totp-panel">
      <div className="totp-panel-header">
        <span className="totp-section-title">{t['totp.section_title']}</span>
        <span className={`totp-status-badge ${enabled ? 'totp-status-badge--on' : 'totp-status-badge--off'}`}>
          {enabled ? t['totp.status_on'] : t['totp.status_off']}
        </span>
      </div>

      {!enabled && !enroll && (
        <div className="totp-panel-body">
          <button
            className="btn btn-secondary btn-sm"
            onClick={handleEnroll}
            disabled={enrolling}
          >
            {enrolling ? '…' : t['totp.enable_btn']}
          </button>
        </div>
      )}

      {!enabled && enroll && (
        <div className="totp-enroll-body">
          {/* QR Code */}
          <p className="totp-hint">{t['totp.scan_qr']}</p>
          <div className="totp-qr-wrap">
            <QRCodeSVG value={enroll.otpauth_uri} size={160} />
          </div>

          {/* Manual secret */}
          <p className="totp-hint">{t['totp.manual_secret']}</p>
          <code className="totp-secret-code">{enroll.secret}</code>

          {/* Recovery codes */}
          <div className="totp-recovery-block">
            <div className="totp-recovery-header">
              <span className="totp-recovery-title">{t['totp.recovery_title']}</span>
              <button
                className={`vault-btn${codesCopied ? ' vault-btn-copied' : ''}`}
                onClick={handleCopyRecovery}
              >
                {codesCopied ? t['totp.recovery_copied'] : t['totp.recovery_copy']}
              </button>
            </div>
            <p className="totp-recovery-warning">{t['totp.recovery_warning']}</p>
            <div className="totp-recovery-codes">
              {enroll.recovery_codes.map(code => (
                <span key={code} className="totp-recovery-code">{code}</span>
              ))}
            </div>
          </div>

          {/* Activation */}
          <div className="totp-activate-row">
            <label className="vault-form-label">{t['totp.activate_label']}</label>
            <div className="totp-activate-input-row">
              <input
                ref={codeInputRef}
                className="vault-form-input totp-code-input"
                type="text"
                inputMode="numeric"
                value={activateCode}
                onChange={e => setActivateCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                placeholder={t['totp.activate_placeholder']}
                maxLength={6}
                autoComplete="one-time-code"
                disabled={activating}
                onKeyDown={e => { if (e.key === 'Enter') handleActivate() }}
              />
              <button
                className="btn btn-primary btn-sm"
                onClick={handleActivate}
                disabled={activating || activateCode.length < 6}
              >
                {activating ? t['totp.activating'] : t['totp.activate_btn']}
              </button>
            </div>
            {activateError && (
              <div className="totp-error">{activateError}</div>
            )}
          </div>
        </div>
      )}

      {enabled && (
        <div className="totp-panel-body">
          <button
            className="btn btn-sm"
            style={{ color: 'var(--red, #ef4444)', borderColor: 'var(--red, #ef4444)' }}
            onClick={() => setShowDisableConfirm(true)}
          >
            {t['totp.disable_btn']}
          </button>
        </div>
      )}

      {showDisableConfirm && (
        <ConfirmModal
          title={t['totp.disable_confirm_title']}
          message={t['totp.disable_confirm_msg']}
          confirmLabel={t['totp.disable_confirm_btn']}
          danger
          onConfirm={handleDisable}
          onCancel={() => setShowDisableConfirm(false)}
        />
      )}
    </div>
  )
}

// ── Component ──────────────────────────────────────────────────

export function VaultTab() {
  const [secrets, setSecrets] = useState<SecretMeta[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')

  // Per-secret revealed values: name → SecretFull
  const [revealed, setRevealed] = useState<Record<string, SecretFull>>({})
  // Which secrets are currently loading their value
  const [revealing, setRevealing] = useState<Set<string>>(new Set())
  // Copy feedback: name → boolean (briefly true)
  const [copiedMap, setCopiedMap] = useState<Record<string, boolean>>({})
  // Collapsed categories
  const [collapsedCats, setCollapsedCats] = useState<Set<string>>(new Set())

  // Modal state (add / edit)
  const [modal, setModal] = useState<ModalState | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  // Delete confirm
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)

  // Local toast (no global useToast available — match SchedulesTab pattern)
  const [toast, setToast] = useState<string | null>(null)

  const showToast = useCallback((msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 4000)
  }, [])

  // ── Load list (names + categories only) ───────────────────────
  const loadSecrets = useCallback(async () => {
    try {
      const res = await api.secretsList()
      setSecrets(res.secrets)
      setError(null)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadSecrets()
  }, [loadSecrets])

  // ── Reveal (on-demand, single secret) ─────────────────────────
  const handleReveal = useCallback(async (name: string) => {
    if (revealed[name] || revealing.has(name)) return
    setRevealing(prev => new Set(prev).add(name))
    try {
      const full = await api.secretReveal(name)
      setRevealed(prev => ({ ...prev, [name]: full }))
    } catch (e: unknown) {
      showToast(e instanceof Error ? e.message : String(e))
    } finally {
      setRevealing(prev => { const next = new Set(prev); next.delete(name); return next })
    }
  }, [revealed, revealing, showToast])

  const handleHide = useCallback((name: string) => {
    setRevealed(prev => { const next = { ...prev }; delete next[name]; return next })
  }, [])

  // ── Copy value ────────────────────────────────────────────────
  const handleCopy = useCallback(async (name: string) => {
    const full = revealed[name]
    if (!full) return
    try {
      await navigator.clipboard.writeText(full.value)
      setCopiedMap(prev => ({ ...prev, [name]: true }))
      setTimeout(() => setCopiedMap(prev => { const next = { ...prev }; delete next[name]; return next }), 2000)
    } catch {
      showToast('Copy failed')
    }
  }, [revealed, showToast])

  // ── Category collapse toggle ──────────────────────────────────
  const toggleCategory = useCallback((cat: string) => {
    setCollapsedCats(prev => {
      const next = new Set(prev)
      if (next.has(cat)) next.delete(cat)
      else next.add(cat)
      return next
    })
  }, [])

  // ── Open add modal ────────────────────────────────────────────
  const openAdd = useCallback(() => {
    setModal({ mode: 'add', name: '', value: '', category: '', notes: '' })
    setSaveError(null)
  }, [])

  // ── Open edit modal (pre-fill from revealed or just meta) ─────
  const openEdit = useCallback(async (secret: SecretMeta) => {
    // Reveal value first if not already revealed
    let full = revealed[secret.name]
    if (!full) {
      try {
        full = await api.secretReveal(secret.name)
        setRevealed(prev => ({ ...prev, [secret.name]: full }))
      } catch (e: unknown) {
        showToast(e instanceof Error ? e.message : String(e))
        return
      }
    }
    setModal({
      mode: 'edit',
      originalName: secret.name,
      name: secret.name,
      value: full.value,
      category: full.category || '',
      notes: full.notes || '',
    })
    setSaveError(null)
  }, [revealed, showToast])

  // ── Save (add or edit) ────────────────────────────────────────
  const handleSave = useCallback(async () => {
    if (!modal) return
    const name = modal.name.trim()
    const value = modal.value
    if (!name) { setSaveError('Name is required'); return }
    if (!value) { setSaveError('Value is required'); return }

    setSaving(true)
    setSaveError(null)
    try {
      await api.secretSet({
        name,
        value,
        category: modal.category.trim() || undefined,
        notes: modal.notes.trim() || undefined,
      })
      // If renamed (edit mode, name changed), delete old record
      if (modal.mode === 'edit' && modal.originalName && modal.originalName !== name) {
        await api.secretDelete(modal.originalName)
        // Drop revealed cache for old name
        setRevealed(prev => { const next = { ...prev }; delete next[modal.originalName!]; return next })
      }
      await loadSecrets()
      setModal(null)
      showToast(modal.mode === 'add' ? 'Secret added' : 'Secret updated')
    } catch (e: unknown) {
      setSaveError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }, [modal, loadSecrets, showToast])

  // ── Delete ────────────────────────────────────────────────────
  const handleDeleteConfirm = useCallback(async () => {
    if (!deleteTarget) return
    try {
      await api.secretDelete(deleteTarget)
      setRevealed(prev => { const next = { ...prev }; delete next[deleteTarget]; return next })
      await loadSecrets()
      showToast('Secret deleted')
    } catch (e: unknown) {
      showToast(e instanceof Error ? e.message : String(e))
    } finally {
      setDeleteTarget(null)
    }
  }, [deleteTarget, loadSecrets, showToast])

  // ── Filter + group ────────────────────────────────────────────
  const filtered = search
    ? secrets.filter(s => s.name.toLowerCase().includes(search.toLowerCase()) ||
        (s.category || '').toLowerCase().includes(search.toLowerCase()))
    : secrets

  const grouped = groupByCategory(filtered)

  // ── Render ────────────────────────────────────────────────────
  return (
    <div className="vault-container">
      {/* 2FA panel — always at the top */}
      <TotpPanel onToast={showToast} />

      {/* Header */}
      <div className="vault-header">
        <h2>{t['vault.title']}</h2>
        <div className="vault-header-actions">
          <button className="btn btn-secondary btn-sm" onClick={openAdd}>
            {t['vault.addSecret']}
          </button>
        </div>
      </div>

      {/* Search */}
      <div className="vault-search">
        <input
          className="vault-search-input"
          type="text"
          placeholder={t['vault.searchPlaceholder']}
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </div>

      {/* Content */}
      <div className="vault-content">
        {loading && (
          <div className="vault-state-msg">{t['vault.loading']}</div>
        )}
        {!loading && error && (
          <div className="vault-error-msg">{t['vault.error']}: {error}</div>
        )}
        {!loading && !error && filtered.length === 0 && (
          <div className="vault-state-msg">{t['vault.noSecrets']}</div>
        )}
        {!loading && !error && filtered.length > 0 && (
          <>
            {[...grouped.entries()].map(([category, items]) => {
              const isCollapsed = collapsedCats.has(category)
              return (
                <div key={category} className="vault-category">
                  <button
                    className="vault-category-header"
                    onClick={() => toggleCategory(category)}
                  >
                    <span className="vault-category-toggle">{isCollapsed ? '▶' : '▼'}</span>
                    <span className="vault-category-label">{category}</span>
                    <span className="vault-category-count">{items.length}</span>
                  </button>
                  {!isCollapsed && items.map(secret => {
                    const isRevealed = !!revealed[secret.name]
                    const isRevealing = revealing.has(secret.name)
                    const isCopied = copiedMap[secret.name]
                    const full = revealed[secret.name]
                    return (
                      <div key={secret.name} className="vault-secret-row">
                        <span className="vault-secret-name" title={secret.name}>
                          {secret.name}
                        </span>
                        <div className="vault-secret-value-area">
                          {isRevealed && full ? (
                            <span className="vault-secret-value" title={full.value}>
                              {full.value}
                            </span>
                          ) : (
                            <span className="vault-secret-masked">••••••••</span>
                          )}
                        </div>
                        <div className="vault-secret-actions">
                          {isRevealed ? (
                            <>
                              <button
                                className={`vault-btn${isCopied ? ' vault-btn-copied' : ''}`}
                                onClick={() => handleCopy(secret.name)}
                                title={t['vault.copy']}
                              >
                                {isCopied ? t['vault.copied'] : t['vault.copy']}
                              </button>
                              <button
                                className="vault-btn"
                                onClick={() => handleHide(secret.name)}
                                title={t['vault.hide']}
                              >
                                {t['vault.hide']}
                              </button>
                            </>
                          ) : (
                            <button
                              className="vault-btn"
                              onClick={() => handleReveal(secret.name)}
                              disabled={isRevealing}
                              title={t['vault.reveal']}
                            >
                              {isRevealing ? '…' : t['vault.reveal']}
                            </button>
                          )}
                          <button
                            className="vault-btn"
                            onClick={() => openEdit(secret)}
                            title={t['vault.edit']}
                          >
                            {t['vault.edit']}
                          </button>
                          <button
                            className="vault-btn vault-btn-danger"
                            onClick={() => setDeleteTarget(secret.name)}
                            title={t['vault.delete']}
                          >
                            {t['vault.delete']}
                          </button>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )
            })}
          </>
        )}
      </div>

      {/* Add / Edit modal */}
      {modal && (
        <Modal onClose={() => { if (!saving) setModal(null) }}>
          <ModalHead
            title={modal.mode === 'add' ? t['vault.addSecret'] : t['vault.edit']}
            onClose={() => { if (!saving) setModal(null) }}
          />
          <div className="run-modal-body">
            <div className="vault-form">
              <div className="vault-form-row">
                <label className="vault-form-label">{t['vault.name']}</label>
                <input
                  className="vault-form-input"
                  type="text"
                  value={modal.name}
                  onChange={e => setModal(m => m ? { ...m, name: e.target.value } : m)}
                  placeholder="MY_SECRET_KEY"
                  disabled={saving}
                  autoFocus
                />
              </div>
              <div className="vault-form-row">
                <label className="vault-form-label">{t['vault.value']}</label>
                <input
                  className="vault-form-input value-input"
                  type="text"
                  value={modal.value}
                  onChange={e => setModal(m => m ? { ...m, value: e.target.value } : m)}
                  placeholder="sk-..."
                  disabled={saving}
                />
              </div>
              <div className="vault-form-row">
                <label className="vault-form-label">{t['vault.category']}</label>
                <input
                  className="vault-form-input"
                  type="text"
                  value={modal.category}
                  onChange={e => setModal(m => m ? { ...m, category: e.target.value } : m)}
                  placeholder="API keys, Telegram, etc."
                  disabled={saving}
                />
              </div>
              <div className="vault-form-row">
                <label className="vault-form-label">{t['vault.notes']}</label>
                <textarea
                  className="vault-form-textarea"
                  value={modal.notes}
                  onChange={e => setModal(m => m ? { ...m, notes: e.target.value } : m)}
                  placeholder="Optional notes..."
                  disabled={saving}
                />
              </div>
              {saveError && (
                <div style={{ color: 'var(--red, #ef4444)', fontSize: 13 }}>{saveError}</div>
              )}
              <div className="vault-form-actions">
                <button
                  className="btn-secondary"
                  onClick={() => setModal(null)}
                  disabled={saving}
                >
                  {t['common.cancel']}
                </button>
                <button
                  className="btn-primary"
                  onClick={handleSave}
                  disabled={saving || !modal.name.trim() || !modal.value}
                >
                  {saving ? t['common.saving'] : t['vault.saveSecret']}
                </button>
              </div>
            </div>
          </div>
        </Modal>
      )}

      {/* Delete confirm modal */}
      {deleteTarget && (
        <ConfirmModal
          title={t['vault.delete']}
          message={t['vault.deleteConfirm'].replace('{name}', deleteTarget)}
          confirmLabel={t['vault.delete']}
          danger
          onConfirm={handleDeleteConfirm}
          onCancel={() => setDeleteTarget(null)}
        />
      )}

      {/* Toast */}
      {toast && (
        <div style={{
          position: 'fixed',
          bottom: 24,
          right: 24,
          padding: '10px 18px',
          background: 'var(--bg-card, var(--bg2))',
          border: '1px solid var(--border2)',
          borderRadius: 8,
          fontSize: 13,
          zIndex: 9999,
          color: 'var(--text)',
        }}>
          {toast}
        </div>
      )}
    </div>
  )
}
