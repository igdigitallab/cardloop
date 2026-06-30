/** Shared formatting helpers for the live subscription-limit rows.
 *  Used by UsageBadge (dropdown) and UsageTab (live limits panel). */

export interface RawLimit {
  status: string
  resets_at: number | null
  utilization: number | null
  ts: number
}

/** Formats "in 2h 15m" or "12m". */
export function fmtReset(resetsAt: number | null, now: number): string {
  if (!resetsAt) return '—'
  const delta = resetsAt - now
  if (delta <= 0) return 'soon'
  const h = Math.floor(delta / 3600)
  const m = Math.floor((delta % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

/** Color class by utilization: <50% green, 50–80% yellow, ≥80% red. */
export function pickClass(d: RawLimit | undefined): string {
  if (!d) return 'usage-dim'
  if (d.status === 'rejected') return 'usage-red'
  if (d.status === 'allowed_warning') return 'usage-yellow'
  const u = d.utilization ?? 0
  if (u >= 0.8) return 'usage-red'
  if (u >= 0.5) return 'usage-yellow'
  return 'usage-green'
}

export function fmtPct(u: number | null): string {
  if (u == null) return ''
  return `${Math.round(u * 100)}%`
}

export const LIMIT_LABELS: Record<string, string> = {
  five_hour: '5-hour window',
  seven_day: 'Week (all)',
  seven_day_opus: 'Week Opus',
  seven_day_sonnet: 'Week Sonnet',
  overage: 'Overage',
}
