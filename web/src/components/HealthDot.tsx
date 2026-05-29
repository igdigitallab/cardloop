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
  if (status === 'gray') return 'Git недоступен'
  const g = health.git!
  const parts = [`ветка: ${g.branch}`]
  if (g.dirty > 0) parts.push(`изменено: ${g.dirty}`)
  if (g.unpushed > 0) parts.push(`не отправлено: ${g.unpushed}`)
  return parts.join(' · ')
}
