import { Project } from '../types'

interface Props {
  projects: Project[]
  activeId: string | null
  unreadBySession: Record<string, number>
  onActivate: (id: string) => void
  onClose: (id: string) => void
}

export function ProjectTabBar({ projects, activeId, unreadBySession, onActivate, onClose }: Props) {
  return (
    <div className="project-tabbar">
      {projects.map(p => {
        const sk = p.tg_thread != null ? String(p.tg_thread) : null
        const unread = sk ? (unreadBySession[sk] || 0) : 0
        const isActive = p.id === activeId
        return (
          <div
            key={p.id}
            className={`ptab ${isActive ? 'active' : ''}`}
            onClick={() => onActivate(p.id)}
            title={p.cwd}
          >
            <span className="ptab-name">{p.name}</span>
            {unread > 0 && !isActive && (
              <span className="ptab-unread" title={`${unread} новых`}>{unread > 99 ? '99+' : unread}</span>
            )}
            <button
              className="ptab-close"
              onClick={(e) => { e.stopPropagation(); onClose(p.id) }}
              title="Закрыть вкладку"
            >
              ✕
            </button>
          </div>
        )
      })}
    </div>
  )
}
