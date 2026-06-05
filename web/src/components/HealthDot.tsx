import { ProjectHealth } from '../types'

interface Props {
  health: ProjectHealth
}

function getStatus(health: ProjectHealth): 'green' | 'yellow' | 'gray' {
  if (!health.git) return 'gray'
  if (health.git.dirty > 0 || health.git.unpushed > 0) return 'yellow'
  return 'green'
}

export function HealthDot({ health }: Props) {
  const status = getStatus(health)
  return <span className={`health-dot ${status}`} title={healthTitle(health, status)} />
}

function healthTitle(health: ProjectHealth, status: string): string {
  if (status === 'gray') return 'Git not available'
  const g = health.git!
  const parts = [`branch: ${g.branch}`]
  if (g.dirty > 0) parts.push(`changed: ${g.dirty}`)
  if (g.unpushed > 0) parts.push(`unpushed: ${g.unpushed}`)
  return parts.join(' · ')
}
