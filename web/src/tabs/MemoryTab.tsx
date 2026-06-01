import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { MemoryFile, ProjectMemory } from '../types'
import { Spinner } from '../components/Spinner'
import { ConfirmModal } from '../components/ConfirmModal'
import { Modal, ModalHead } from '../components/Modal'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'
import { t } from '../i18n'

interface Props {
  projectId: string
}

// ── New/Edit modal ────────────────────────────────────────────────────────────

interface EditModalProps {
  projectId: string
  /** Existing file name when editing; null when creating new */
  existingName: string | null
  initialContent: string
  onSaved: (data: ProjectMemory) => void
  onClose: () => void
}

function EditModal({ projectId, existingName, initialContent, onSaved, onClose }: EditModalProps) {
  const isNew = existingName === null
  const [name, setName] = useState(existingName ?? '')
  const [content, setContent] = useState(initialContent)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const nameRef = useRef<HTMLInputElement>(null)
  const contentRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (isNew) nameRef.current?.focus()
    else contentRef.current?.focus()
  }, [isNew])

  async function doSave() {
    setError('')
    let finalName = name.trim()
    if (!finalName) { setError('Имя файла обязательно'); return }
    // Auto-append .md if needed
    if (!finalName.endsWith('.md')) finalName = finalName + '.md'
    // Basic slug validation
    if (!/^[a-z0-9][a-z0-9-]{0,60}\.md$/.test(finalName)) {
      setError('Имя: строчные a-z, 0-9, дефис, 2–62 символа до .md')
      return
    }
    setSaving(true)
    try {
      const data = await api.saveMemory(projectId, finalName, content)
      onSaved(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal onClose={onClose} className="memory-edit-modal">
      <ModalHead
        title={isNew ? '+ Новая запись памяти' : `Редактировать: ${existingName}`}
        onClose={onClose}
      />
      <div className="run-modal-body" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {isNew && (
          <label style={{ fontSize: 13 }}>
            <span style={{ display: 'block', marginBottom: 4, color: 'var(--text2)' }}>
              {t['memory.new_name_label']}
            </span>
            <input
              ref={nameRef}
              type="text"
              className="doc-textarea"
              style={{ height: 'auto', padding: '6px 8px', fontSize: 13 }}
              placeholder={t['memory.new_name_placeholder']}
              value={name}
              onChange={e => setName(e.target.value)}
              onKeyDown={e => {
                if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); doSave() }
                else if (e.key === 'Escape') { e.preventDefault(); onClose() }
              }}
            />
          </label>
        )}
        <label style={{ fontSize: 13 }}>
          <span style={{ display: 'block', marginBottom: 4, color: 'var(--text2)' }}>
            {t['memory.new_content_label']}
          </span>
          <textarea
            ref={contentRef}
            className="doc-textarea"
            style={{ minHeight: 220 }}
            placeholder={t['memory.new_content_placeholder']}
            spellCheck={false}
            value={content}
            onChange={e => setContent(e.target.value)}
            onKeyDown={e => {
              if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); doSave() }
              else if (e.key === 'Escape') { e.preventDefault(); onClose() }
            }}
          />
        </label>
        {error && <div className="error-state" style={{ fontSize: 12 }}>⚠ {error}</div>}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="doc-btn ghost" onClick={onClose} disabled={saving}>{t['common.cancel']}</button>
          <button className="doc-btn primary" onClick={doSave} disabled={saving}>
            {saving ? t['memory.saving'] : t['memory.save_btn']}
          </button>
        </div>
        <p style={{ margin: 0, fontSize: 11, color: 'var(--text3)' }}>
          Ctrl+Enter — сохранить · Esc — отмена
        </p>
      </div>
    </Modal>
  )
}

// ── Main tab ──────────────────────────────────────────────────────────────────

export function MemoryTab({ projectId }: Props) {
  const [data, setData] = useState<ProjectMemory | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string | null>(null)

  // Edit state
  const [editTarget, setEditTarget] = useState<MemoryFile | null>(null)  // null = create new
  const [showEditModal, setShowEditModal] = useState(false)

  // Delete confirm state
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)

  const reload = useCallback(() => {
    api.memory(projectId).then(d => {
      setData(d); setError('')
    }).catch(e => setError(String(e.message || e)))
  }, [projectId])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setData(null)
    setSelected(null)

    api.memory(projectId).then(d => {
      if (!cancelled) {
        setData(d)
        setLoading(false)
        if (d.files.length > 0) {
          setSelected(d.files[0].name)
        }
      }
    }).catch(e => {
      if (!cancelled) { setError(String(e.message || e)); setLoading(false) }
    })

    return () => { cancelled = true }
  }, [projectId])

  useOnRunEnd(reload)
  useFocusRefresh(reload)

  function openCreate() {
    setEditTarget(null)
    setShowEditModal(true)
  }

  function openEdit(f: MemoryFile) {
    setEditTarget(f)
    setShowEditModal(true)
  }

  function onSaved(updated: ProjectMemory) {
    setData(updated)
    setShowEditModal(false)
    // Auto-select the most recently edited file (non-MEMORY.md)
    if (updated.files.length > 0) {
      const nonIndex = updated.files.find(f => f.name !== 'MEMORY.md' && (
        editTarget ? f.name === editTarget.name : true
      ))
      setSelected(nonIndex?.name ?? updated.files[0].name)
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      const updated = await api.deleteMemory(projectId, deleteTarget)
      setData(updated)
      if (selected === deleteTarget) {
        setSelected(updated.files.length > 0 ? updated.files[0].name : null)
      }
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setDeleting(false)
      setDeleteTarget(null)
    }
  }

  if (loading) return <Spinner label={t['memory.loading']} />
  if (error) return <div className="error-state">⚠ {error}</div>

  if (!data || !data.exists || data.files.length === 0) {
    return (
      <>
        <div className="memory-empty">
          <div className="memory-empty-icon">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z"/>
              <path d="M12 8v4l3 3"/>
            </svg>
          </div>
          <div className="memory-empty-title">{t['memory.empty_title']}</div>
          <p className="memory-empty-text">
            Файлы из <code>{t['memory.path_hint']}</code> — накапливаемые знания проекта,
            которые <strong>коммитятся в git</strong> и путешествуют с репо.
          </p>
          <p className="memory-empty-text">
            Агент пишет сюда сам (через Write), или создай запись вручную кнопкой ниже.
            Типы: <code>decision</code> / <code>gotcha</code> / <code>rejected</code> / <code>convention</code>.
          </p>
          <p className="memory-empty-note">
            💡 Индекс <code>MEMORY.md</code> обновляется автоматически при каждой записи.
          </p>
          <button
            className="doc-btn primary"
            style={{ marginTop: 12 }}
            onClick={openCreate}
            aria-label={t['memory.new_btn_aria']}
          >
            {t['memory.new_btn']}
          </button>
        </div>
        {showEditModal && (
          <EditModal
            projectId={projectId}
            existingName={null}
            initialContent={'---\ntype: decision\ncreated: ' + new Date().toISOString().slice(0, 10) + '\n---\n'}
            onSaved={onSaved}
            onClose={() => setShowEditModal(false)}
          />
        )}
      </>
    )
  }

  const selectedFile: MemoryFile | undefined = data.files.find(f => f.name === selected)

  return (
    <>
      <div className="specs-layout">
        {/* Sidebar */}
        <div className="specs-list">
          <div className="specs-list-label" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span>{t['memory.files_label']}</span>
            <button
              className="doc-btn primary"
              style={{ padding: '2px 8px', fontSize: 11 }}
              onClick={openCreate}
              aria-label={t['memory.new_btn_aria']}
            >
              {t['memory.new_btn']}
            </button>
          </div>
          {data.files.map(f => (
            <div
              key={f.name}
              className={`spec-item ${selected === f.name ? 'active' : ''}`}
              onClick={() => setSelected(f.name)}
              title={f.name}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                style={{ flexShrink: 0, opacity: 0.5 }}>
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {f.name}
              </span>
              {f.name === 'MEMORY.md' && (
                <span style={{ marginLeft: 4, fontSize: 10, color: 'var(--text3)', flexShrink: 0 }}>{t['memory.index']}</span>
              )}
              {/* Edit / Delete buttons — visible on hover via CSS */}
              {f.name !== 'MEMORY.md' && (
                <span className="memory-item-actions" onClick={e => e.stopPropagation()}>
                  <button
                    className="memory-action-btn"
                    title={t['memory.edit_btn_aria']}
                    aria-label={t['memory.edit_btn_aria']}
                    onClick={() => openEdit(f)}
                  >✎</button>
                  <button
                    className="memory-action-btn memory-action-btn--danger"
                    title={t['memory.delete_btn_aria']}
                    aria-label={t['memory.delete_btn_aria']}
                    onClick={() => setDeleteTarget(f.name)}
                  >✕</button>
                </span>
              )}
            </div>
          ))}
        </div>

        {/* Content area */}
        <div className="spec-content">
          {!selectedFile && (
            <div className="no-content" style={{ paddingTop: 4 }}>← Выберите файл</div>
          )}
          {selectedFile && (
            <div className="markdown-wrap" style={{ position: 'relative' }}>
              {selectedFile.name !== 'MEMORY.md' && (
                <button
                  className="doc-edit-fab"
                  onClick={() => openEdit(selectedFile)}
                  aria-label={t['memory.edit_btn_aria']}
                  title={t['memory.edit_btn_aria']}
                >✎</button>
              )}
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{selectedFile.content}</ReactMarkdown>
            </div>
          )}
        </div>
      </div>

      {/* Edit modal */}
      {showEditModal && (
        <EditModal
          projectId={projectId}
          existingName={editTarget?.name ?? null}
          initialContent={editTarget?.content ?? ('---\ntype: decision\ncreated: ' + new Date().toISOString().slice(0, 10) + '\n---\n')}
          onSaved={onSaved}
          onClose={() => setShowEditModal(false)}
        />
      )}

      {/* Delete confirm */}
      {deleteTarget && (
        <ConfirmModal
          title={t['memory.confirm_delete_title']}
          message={t['memory.confirm_delete_body']}
          confirmLabel={deleting ? '…' : t['memory.confirm_delete_yes']}
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
          danger
        />
      )}
    </>
  )
}
