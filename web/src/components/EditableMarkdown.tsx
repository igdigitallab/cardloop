import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { mdComponents } from './markdown'
import { ClaudeMd } from '../types'
import { Spinner } from './Spinner'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'
import { t } from '../i18n'

interface Props {
  projectId: string
  load: (id: string) => Promise<ClaudeMd>
  save: (id: string, content: string) => Promise<ClaudeMd>
  spinnerLabel: string
  emptyLabel: string
}

/** Markdown viewer with inline editor: double-click → textarea → save to file.
 *  Shared engine for CLAUDE.md and README tabs (ops:455557). */
export function EditableMarkdown({ projectId, load, save, spinnerLabel, emptyLabel }: Props) {
  const [data, setData] = useState<ClaudeMd | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)

  // While editing — background reloads (run-end/focus) do NOT overwrite the draft.
  const editingRef = useRef(false)
  editingRef.current = editing

  const reload = useCallback(() => {
    if (editingRef.current) return
    load(projectId).then(d => { setData(d); setError('') })
      .catch(e => setError(String(e.message || e)))
  }, [projectId, load])

  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(''); setData(null); setEditing(false)
    load(projectId).then(d => {
      if (!cancelled) { setData(d); setLoading(false) }
    }).catch(e => {
      if (!cancelled) { setError(String(e.message || e)); setLoading(false) }
    })
    return () => { cancelled = true }
  }, [projectId, load])

  useOnRunEnd(reload)
  useFocusRefresh(reload)

  function startEdit() {
    setDraft(data?.content || '')
    setError('')
    setEditing(true)
  }

  async function doSave() {
    setSaving(true); setError('')
    try {
      const d = await save(projectId, draft)
      setData(d)
      setEditing(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  function cancel() {
    setEditing(false); setError('')
  }

  if (loading) return <Spinner label={spinnerLabel} />

  if (editing) {
    return (
      <div className="doc-editor">
        <div className="doc-editor-bar">
          <span className="doc-editor-hint">Ctrl+Enter — save · Esc — cancel</span>
          <div className="doc-editor-actions">
            <button className="doc-btn ghost" onClick={cancel} disabled={saving}>{t['common.cancel']}</button>
            <button className="doc-btn primary" onClick={doSave} disabled={saving}>
              {saving ? t['editable.saving'] : t['common.save']}
            </button>
          </div>
        </div>
        {error && <div className="error-state">⚠ {error}</div>}
        <textarea
          className="doc-textarea"
          value={draft}
          autoFocus
          spellCheck={false}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); doSave() }
            else if (e.key === 'Escape') { e.preventDefault(); cancel() }
          }}
        />
      </div>
    )
  }

  if (error) return <div className="error-state">⚠ {error}</div>

  if (!data || !data.exists) {
    return (
      <div className="no-content">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
        {emptyLabel}
        {data && <span style={{ color: 'var(--text3)', marginLeft: 6, fontSize: 11 }}>({data.path})</span>}
        <button className="doc-btn primary doc-create-btn" onClick={startEdit}>✏ Create</button>
      </div>
    )
  }

  return (
    <div className="markdown-wrap doc-view" onDoubleClick={startEdit}
      title={t['editable.dblclick_title']}>
      <button className="doc-edit-fab" onClick={startEdit} title={t['editable.edit_title']}>✎</button>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>{data.content}</ReactMarkdown>
    </div>
  )
}
