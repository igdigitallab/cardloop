import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Prompt } from '../types'

interface Props {
  onSelect: (text: string) => void
  onClose: () => void
}

export function PromptPicker({ onSelect, onClose }: Props) {
  const [prompts, setPrompts] = useState<Prompt[]>([])
  const [loading, setLoading] = useState(true)
  const [adding, setAdding] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newText, setNewText] = useState('')
  const [saving, setSaving] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const titleRef = useRef<HTMLInputElement>(null)

  const load = useCallback(() => {
    setLoading(true)
    api.prompts()
      .then(r => setPrompts(r.prompts))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  // Автофокус на поле названия при открытии формы
  useEffect(() => {
    if (adding) setTimeout(() => titleRef.current?.focus(), 0)
  }, [adding])

  // Закрыть при клике снаружи
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
      const res = await api.createPrompt({ title: newTitle.trim(), text: newText.trim() })
      setPrompts(prev => [...prev, res.prompt])
      setNewTitle('')
      setNewText('')
      setAdding(false)
    } catch {}
    finally { setSaving(false) }
  }

  function handleKeyDownForm(e: React.KeyboardEvent) {
    if (e.key === 'Escape') { setAdding(false); setNewTitle(''); setNewText('') }
  }

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
        {prompts.map(p => (
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

      {adding ? (
        <div className="prompt-add-form" onKeyDown={handleKeyDownForm}>
          <input
            ref={titleRef}
            className="prompt-add-title"
            placeholder="Название шаблона"
            value={newTitle}
            onChange={e => setNewTitle(e.target.value)}
          />
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
              onClick={() => { setAdding(false); setNewTitle(''); setNewText('') }}
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
