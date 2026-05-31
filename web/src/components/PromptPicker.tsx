import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Prompt } from '../types'

interface Props {
  onSelect: (text: string) => void
  onClose: () => void
}

const NO_CATEGORY = 'Без категории'

function groupPrompts(prompts: Prompt[]): [string, Prompt[]][] {
  const map = new Map<string, Prompt[]>()
  for (const p of prompts) {
    const key = p.category || NO_CATEGORY
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(p)
  }
  // Без категории — в конец
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
  const [adding, setAdding] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newText, setNewText] = useState('')
  const [newCategory, setNewCategory] = useState('')
  const [saving, setSaving] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const titleRef = useRef<HTMLInputElement>(null)

  const load = useCallback(() => {
    setLoading(true)
    api.prompts()
      .then(r => {
        setPrompts(r.prompts)
        // Открываем первую группу по умолчанию
        const groups = groupPrompts(r.prompts)
        if (groups.length > 0) setOpenCategory(prev => prev ?? groups[0][0])
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    if (adding) setTimeout(() => titleRef.current?.focus(), 0)
  }, [adding])

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

  async function handleDelete(id: string, e: React.MouseEvent) {
    e.stopPropagation()
    try {
      await api.deletePrompt(id)
      setPrompts(prev => prev.filter(p => p.id !== id))
    } catch {}
  }

  async function handleSave() {
    if (!newTitle.trim() || !newText.trim()) return
    setSaving(true)
    try {
      const res = await api.createPrompt({
        title: newTitle.trim(),
        text: newText.trim(),
        category: newCategory.trim() || undefined,
      })
      setPrompts(prev => [...prev, res.prompt])
      const cat = res.prompt.category || NO_CATEGORY
      setOpenCategory(cat)
      setNewTitle(''); setNewText(''); setNewCategory(''); setAdding(false)
    } catch {}
    finally { setSaving(false) }
  }

  function handleKeyDownForm(e: React.KeyboardEvent) {
    if (e.key === 'Escape') { setAdding(false); setNewTitle(''); setNewText(''); setNewCategory('') }
  }

  const groups = groupPrompts(prompts)
  const existingCategories = groups.map(([k]) => k).filter(k => k !== NO_CATEGORY)

  return (
    <div className="prompt-picker" ref={ref}>
      <div className="prompt-picker-header">
        <span className="prompt-picker-title">📋 Шаблоны</span>
        <button className="prompt-picker-close" onClick={onClose} title="Закрыть">✕</button>
      </div>

      <div className="prompt-picker-list">
        {loading && <div className="prompt-picker-empty">Загрузка…</div>}
        {!loading && prompts.length === 0 && !adding && (
          <div className="prompt-picker-empty">Нет шаблонов. Добавьте первый!</div>
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
                    className="prompt-card"
                    onClick={() => onSelect(p.text)}
                    title={p.text}
                  >
                    <div className="prompt-card-inner">
                      <div className="prompt-card-title">{p.title}</div>
                      <div className="prompt-card-preview">
                        {p.text.replace(/\n/g, ' ').slice(0, 90)}{p.text.length > 90 ? '…' : ''}
                      </div>
                    </div>
                    <button
                      className="prompt-delete-btn"
                      onClick={e => handleDelete(p.id, e)}
                      title="Удалить шаблон"
                    >✕</button>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {adding ? (
        <div className="prompt-add-form" onKeyDown={handleKeyDownForm}>
          <input
            ref={titleRef}
            className="prompt-add-title"
            placeholder="Название шаблона"
            value={newTitle}
            onChange={e => setNewTitle(e.target.value)}
          />
          <input
            className="prompt-add-title"
            placeholder="Категория (необязательно)"
            list="prompt-categories"
            value={newCategory}
            onChange={e => setNewCategory(e.target.value)}
          />
          <datalist id="prompt-categories">
            {existingCategories.map(c => <option key={c} value={c} />)}
          </datalist>
          <textarea
            className="prompt-add-text"
            placeholder={"Текст промта…\nИспользуй [ПЕРЕМЕННАЯ] для мест заполнения"}
            value={newText}
            onChange={e => setNewText(e.target.value)}
            rows={5}
          />
          <div className="prompt-add-actions">
            <button
              className="btn-primary"
              onClick={handleSave}
              disabled={saving || !newTitle.trim() || !newText.trim()}
            >Сохранить</button>
            <button
              className="btn-secondary"
              onClick={() => { setAdding(false); setNewTitle(''); setNewText(''); setNewCategory('') }}
            >Отмена</button>
          </div>
        </div>
      ) : (
        <button className="prompt-add-btn" onClick={() => setAdding(true)}>
          ＋ Новый шаблон
        </button>
      )}
    </div>
  )
}
