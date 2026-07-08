import { ProjectHealth } from '../types'

export function getHealthStatus(health?: ProjectHealth): 'green' | 'yellow' | 'gray' {
  if (!health?.git) return 'gray'
  if (health.git.dirty > 0 || health.git.unpushed > 0) return 'yellow'
  return 'green'
}

function healthTitle(health: ProjectHealth | undefined, status: string): string | undefined {
  if (status === 'gray' || !health?.git) return 'Git not available'
  const g = health.git
  const parts = [`branch: ${g.branch}`]
  if (g.dirty > 0) parts.push(`changed: ${g.dirty}`)
  if (g.unpushed > 0) parts.push(`unpushed: ${g.unpushed}`)
  return parts.join(' · ')
}

interface Props {
  name: string
  health?: ProjectHealth
  isFree?: boolean
}

/** Unified leading tile for every sidebar row.
 *  Project  → first-letter monogram + bottom-right health pip.
 *  Free chat → "#" glyph, no pip (a free chat has no git — the missing pip is the tell). */
export function ProjectAvatar({ name, health, isFree }: Props) {
  if (isFree) {
    return (
      <span className="project-avatar project-avatar-free" aria-hidden="true">
        <span className="project-avatar-glyph">#</span>
      </span>
    )
  }
  const status = getHealthStatus(health)
  const letter = (name.trim()[0] || '?').toUpperCase()
  return (
    <span className="project-avatar" title={healthTitle(health, status)}>
      <span className="project-avatar-glyph">{letter}</span>
      <span className={`project-avatar-pip ${status}`} />
    </span>
  )
}
