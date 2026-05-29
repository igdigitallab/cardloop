import { useState } from 'react'
import { Project } from '../types'
import { HealthDot } from './HealthDot'

interface Props {
  projects: Project[]
  selectedId: string | null
  onSelect: (id: string) => void
  onLogout: () => void
  loading: boolean
}

export function Sidebar({ projects, selectedId, onSelect, onLogout, loading }: Props) {
  const [search, setSearch] = useState('')

  const filtered = projects.filter(p =>
    p.name.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <div className="sidebar-logo">
          <div className="sidebar-logo-icon">⚡</div>
          <span className="sidebar-logo-text">Claude-Ops</span>
        </div>
        <input
          className="search-input"
          type="text"
          placeholder="Поиск проектов..."
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </div>

      <div className="sidebar-section-label">Проекты</div>

      <div className="projects-list">
        {loading ? (
          <div className="projects-empty">Загрузка...</div>
        ) : filtered.length === 0 ? (
          <div className="projects-empty">
            {search ? 'Ничего не найдено' : 'Нет проектов'}
          </div>
        ) : (
          filtered.map(p => (
            <div
              key={p.id}
              className={`project-item ${selectedId === p.id ? 'active' : ''}`}
              onClick={() => onSelect(p.id)}
              title={p.cwd}
            >
              <HealthDot health={p.health} />
              <span className="project-name">{p.name}</span>
            </div>
          ))
        )}
      </div>

      <div className="sidebar-footer">
        <button className="logout-btn" onClick={onLogout}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
            <polyline points="16 17 21 12 16 7"/>
            <line x1="21" y1="12" x2="9" y2="12"/>
          </svg>
          Выйти
        </button>
      </div>
    </div>
  )
}
