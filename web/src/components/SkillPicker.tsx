import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

interface Skill {
  name: string
  description: string
}

interface Props {
  projectId: string
  /** Вставка текста в чат-инпут (родитель ставит курсор/фокус). */
  onSelect: (text: string) => void
  onClose: () => void
}

type Section = 'project' | 'global'

/** Шаблон вставки. Claude Code понимает «используй скилл <name>» как намёк агенту. */
function insertText(name: string): string {
  return `используй скилл ${name}: `
}

export function SkillPicker({ projectId, onSelect, onClose }: Props) {
  const [skills, setSkills] = useState<{ project: Skill[]; global: Skill[] }>({ project: [], global: [] })
  const [loading, setLoading] = useState(true)
  const [openSection, setOpenSection] = useState<Section | null>(null)
  const [error, setError] = useState('')
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setLoading(true)
    api.projectSkills(projectId)
      .then(r => {
        setSkills(r)
        // Открываем «Проекта» если есть что показать, иначе «Глобальные»
        setOpenSection(r.project.length > 0 ? 'project' : 'global')
      })
      .catch(e => setError(e?.message || 'ошибка загрузки'))
      .finally(() => setLoading(false))
  }, [projectId])

  // Outside click → close
  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

  function renderSection(key: Section, label: string, icon: string, items: Skill[]) {
    const isOpen = openSection === key
    return (
      <div key={key} className="prompt-group">
        <button
          className={`prompt-group-header${isOpen ? ' open' : ''}`}
          onClick={() => setOpenSection(prev => (prev === key ? null : key))}
        >
          <span className="prompt-group-caret">{isOpen ? '▾' : '▸'}</span>
          <span className="prompt-group-name">{icon} {label}</span>
          <span className="prompt-group-count">{items.length}</span>
        </button>
        {isOpen && (
          <div className="prompt-group-items">
            {items.length === 0 && (
              <div className="prompt-picker-empty" style={{ padding: '8px 12px' }}>
                {key === 'project'
                  ? 'нет проектных скиллов (добавь в <cwd>/.claude/skills/<name>/SKILL.md)'
                  : 'нет глобальных скиллов'}
              </div>
            )}
            {items.map(s => (
              <div
                key={s.name}
                className="prompt-card"
                onClick={() => onSelect(insertText(s.name))}
                title={s.description}
              >
                <div className="prompt-card-inner">
                  <div className="prompt-card-title">{s.name}</div>
                  <div className="prompt-card-preview">
                    {s.description.slice(0, 140)}{s.description.length > 140 ? '…' : ''}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="prompt-picker" ref={ref}>
      <div className="prompt-picker-header">
        <span className="prompt-picker-title">🛠 Скиллы агента</span>
        <button className="prompt-picker-close" onClick={onClose} title="Закрыть">✕</button>
      </div>

      <div className="prompt-picker-list">
        {loading && <div className="prompt-picker-empty">Загрузка…</div>}
        {error && <div className="prompt-picker-empty">⚠ {error}</div>}
        {!loading && !error && (
          <>
            {renderSection('project', 'Проекта', '📁', skills.project)}
            {renderSection('global', 'Глобальные', '🌍', skills.global)}
          </>
        )}
      </div>
    </div>
  )
}
