// Pure logic of the project-health pill (features/project_health): which findings it shows,
// how it is labelled, and how stale a result is.  Kept free of React so it is unit-testable
// (see healthFindings.test.ts for the run command).
import type { HealthFinding } from '../types'

/**
 * Findings another header element already renders.  The `.env exposed` pill is driven by the same
 * rule on the server (api_project_health shares it with the check), so showing it again in the
 * health pill would be the same warning twice.
 */
export const COVERED_ELSEWHERE: readonly string[] = ['env_exposed']

export interface PillState {
  count: number
  crit: boolean
  label: string
  title: string
}

/** The findings the health pill counts and lists. */
export function pillFindings(findings: readonly HealthFinding[] | null | undefined): HealthFinding[] {
  return (findings ?? []).filter(f => !COVERED_ELSEWHERE.includes(f.id))
}

/** null when there is nothing to show: a healthy project must render no pill at all. */
export function pillState(findings: readonly HealthFinding[] | null | undefined): PillState | null {
  const shown = pillFindings(findings)
  if (shown.length === 0) return null
  const crit = shown.filter(f => f.severity === 'crit').length
  const parts = [`${shown.length} issue${shown.length === 1 ? '' : 's'}`]
  if (crit > 0) parts.push(`${crit} critical`)
  return { count: shown.length, crit: crit > 0, label: `⚠ ${shown.length}`, title: parts.join(', ') }
}

/** Stable React key: one check can raise several findings (one per memory index / settings file). */
export function findingKey(f: HealthFinding): string {
  return `${f.id}:${f.subject}`
}

/** "just now" / "5m ago" / "3h ago" / "2d ago" for a unix-seconds timestamp; '' when unknown. */
export function ageLabel(checkedAt: number | null | undefined, nowSec: number): string {
  if (!checkedAt) return ''
  const s = Math.max(0, nowSec - checkedAt)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}
