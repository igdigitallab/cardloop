import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

interface Skill {
  name: string
  description: string
}

interface Props {
  projectId: string
  /** Insert text into the chat input (parent sets cursor/focus). */
  onSelect: (text: string) => void
  onClose: () => void
}

type Section = 'project' | 'global'

/** Insertion template. Claude Code understands "use skill <name>:" as a hint to the agent. */
function insertText(name: string): string {
  return `use skill ${name}: `
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
        // Open 'Project' section if there is something to show, otherwise 'Global'
        setOpenSection(r.project.length > 0 ? 'project' : 'global')
      })
      .catch(e => setError(e?.message || 'loading error'))
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
                  ? 'no project skills (add one at <cwd>/.claude/skills/<name>/SKILL.md)'
                  : 'no global skills'}
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
        <span className="prompt-picker-title">🛠 Agent skills</span>
        <button className="prompt-picker-close" onClick={onClose} title="Close">✕</button>
      </div>

      <div className="prompt-picker-list">
        {loading && <div className="prompt-picker-empty">Loading…</div>}
        {error && <div className="prompt-picker-empty">⚠ {error}</div>}
        {!loading && !error && (
          <>
            {renderSection('project', 'Project', '📁', skills.project)}
            {renderSection('global', 'Global', '🌍', skills.global)}
          </>
        )}
      </div>
    </div>
  )
}
