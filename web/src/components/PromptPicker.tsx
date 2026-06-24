import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Prompt } from '../types'
import { t } from '../i18n'

interface Props {
  onSelect: (text: string) => void
  onClose: () => void
}

const NO_CATEGORY = t['prompts.no_category']

function groupPrompts(prompts: Prompt[]): [string, Prompt[]][] {
  const map = new Map<string, Prompt[]>()
  for (const p of prompts) {
    const key = p.category || NO_CATEGORY
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(p)
  }
  const result: [string, Prompt[]][] = []
  for (const [k, v] of map) {
    if (k !== NO_CATEGORY) result.push([k, v])
  }
  if (map.has(NO_CATEGORY)) result.push([NO_CATEGORY, map.get(NO_CATEGORY)!])
  return result
}

export function PromptPicker({ onSelect, onClose }: Props) {
  const [prompts, setPrompts] = useState<Prompt[]>([])
  const [loading, setLoading] = useState(true)
  const [openCategory, setOpenCategory] = useState<string | null>(null)

  // add / edit form
  const [formMode, setFormMode] = useState<'idle' | 'add' | 'edit'>('idle')
  const [editId, setEditId] = useState<string | null>(null)
  const [formTitle, setFormTitle] = useState('')
  const [formText, setFormText] = useState('')
  const [formCategory, setFormCategory] = useState('')
  const [saving, setSaving] = useState(false)

  const ref = useRef<HTMLDivElement>(null)
  const titleRef = useRef<HTMLInputElement>(null)

  const load = useCallback(() => {
    setLoading(true)
    api.prompts()
      .then(r => {
        setPrompts(r.prompts)
        const groups = groupPrompts(r.prompts)
        if (groups.length > 0) setOpenCategory(prev => prev ?? groups[0][0])
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    if (formMode !== 'idle') setTimeout(() => titleRef.current?.focus(), 0)
  }, [formMode])

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

  function openAdd() {
    setFormMode('add')
    setEditId(null)
    setFormTitle(''); setFormText(''); setFormCategory('')
  }

  function openEdit(p: Prompt, e: React.MouseEvent) {
    e.stopPropagation()
    setFormMode('edit')
    setEditId(p.id)
    setFormTitle(p.title)
    setFormText(p.text)
    setFormCategory(p.category || '')
  }

  function closeForm() {
    setFormMode('idle')
    setEditId(null)
    setFormTitle(''); setFormText(''); setFormCategory('')
  }

  async function handleDelete(id: string, e: React.MouseEvent) {
    e.stopPropagation()
    try {
      await api.deletePrompt(id)
      setPrompts(prev => prev.filter(p => p.id !== id))
      if (editId === id) closeForm()
    } catch {}
  }

  async function handleSave() {
    if (!formTitle.trim() || !formText.trim()) return
    setSaving(true)
    try {
      if (formMode === 'add') {
        const res = await api.createPrompt({
          title: formTitle.trim(),
          text: formText.trim(),
          category: formCategory.trim() || undefined,
        })
        setPrompts(prev => [...prev, res.prompt])
        setOpenCategory(res.prompt.category || NO_CATEGORY)
      } else if (formMode === 'edit' && editId) {
        const res = await api.updatePrompt(editId, {
          title: formTitle.trim(),
          text: formText.trim(),
          category: formCategory.trim() || '',
        })
        setPrompts(prev => prev.map(p => p.id === editId ? res.prompt : p))
        setOpenCategory(res.prompt.category || NO_CATEGORY)
      }
      closeForm()
    } catch {}
    finally { setSaving(false) }
  }

  function handleKeyDownForm(e: React.KeyboardEvent) {
    if (e.key === 'Escape') closeForm()
    if (e.key === 's' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleSave() }
  }

  const groups = groupPrompts(prompts)
  const existingCategories = groups.map(([k]) => k).filter(k => k !== NO_CATEGORY)

  return (
    <div className="prompt-picker" ref={ref}>
      <div className="prompt-picker-header">
        <span className="prompt-picker-title">📋 Templates</span>
        <button className="prompt-picker-close" onClick={onClose} title={t['prompts.close']}>✕</button>
      </div>

      <div className="prompt-picker-list">
        {loading && <div className="prompt-picker-empty">{t['prompts.loading']}</div>}
        {!loading && prompts.length === 0 && formMode === 'idle' && (
          <div className="prompt-picker-empty">{t['prompts.empty']}</div>
        )}
        {!loading && groups.map(([cat, items]) => (
          <div key={cat} className="prompt-group">
            <button
              className={`prompt-group-header${openCategory === cat ? ' open' : ''}`}
              onClick={() => setOpenCategory(prev => prev === cat ? null : cat)}
            >
              <span className="prompt-group-caret">{openCategory === cat ? '▾' : '▸'}</span>
              <span className="prompt-group-name">{cat}</span>
              <span className="prompt-group-count">{items.length}</span>
            </button>
            {openCategory === cat && (
              <div className="prompt-group-items">
                {items.map(p => (
                  <div
                    key={p.id}
                    className={`prompt-card${editId === p.id ? ' editing' : ''}`}
                    onClick={() => formMode === 'idle' && onSelect(p.text)}
                    title={formMode === 'idle' ? p.text : undefined}
                  >
                    <div className="prompt-card-inner">
                      <div className="prompt-card-title">{p.title}</div>
                      <div className="prompt-card-preview">
                        {p.text.replace(/\n/g, ' ').slice(0, 90)}{p.text.length > 90 ? '…' : ''}
                      </div>
                    </div>
                    <button
                      className="prompt-edit-btn"
                      onClick={e => openEdit(p, e)}
                      title={t['common.edit']}
                    >✎</button>
                    <button
                      className="prompt-delete-btn"
                      onClick={e => handleDelete(p.id, e)}
                      title={t['common.delete']}
                    >✕</button>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {formMode !== 'idle' ? (
        <div className="prompt-add-form" onKeyDown={handleKeyDownForm}>
          <div className="prompt-form-label">{formMode === 'add' ? t['prompts.new'] : t['common.edit']}</div>
          <input
            ref={titleRef}
            className="prompt-add-title"
            placeholder={t['prompts.name_placeholder']}
            value={formTitle}
            onChange={e => setFormTitle(e.target.value)}
          />
          <input
            className="prompt-add-title"
            placeholder={t['prompts.category_placeholder']}
            list="prompt-categories"
            value={formCategory}
            onChange={e => setFormCategory(e.target.value)}
          />
          <datalist id="prompt-categories">
            {existingCategories.map(c => <option key={c} value={c} />)}
          </datalist>
          <textarea
            className="prompt-add-text"
            placeholder={t['prompts.text_placeholder']}
            value={formText}
            onChange={e => setFormText(e.target.value)}
            rows={5}
          />
          <div className="prompt-add-actions">
            <button
              className="btn-primary"
              onClick={handleSave}
              disabled={saving || !formTitle.trim() || !formText.trim()}
            >{saving ? '…' : t['prompts.save']}</button>
            <button className="btn-secondary" onClick={closeForm} disabled={saving}>{t['prompts.cancel']}</button>
          </div>
        </div>
      ) : (
        <button className="prompt-add-btn" onClick={openAdd}>
          ＋ {t['prompts.new']}
        </button>
      )}
    </div>
  )
}
