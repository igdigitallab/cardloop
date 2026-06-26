import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

interface RawLimit {
  status: string
  resets_at: number | null
  utilization: number | null
  ts: number
}

interface UsageData {
  limits: Record<string, RawLimit>
  now: number
}

/** Formats "in 2h 15m" or "12m". */
function fmtReset(resetsAt: number | null, now: number): string {
  if (!resetsAt) return '—'
  const delta = resetsAt - now
  if (delta <= 0) return 'soon'
  const h = Math.floor(delta / 3600)
  const m = Math.floor((delta % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

/** Color by utilization: <50% green, 50–80% yellow, ≥80% red. */
function pickClass(d: RawLimit | undefined): string {
  if (!d) return 'usage-dim'
  if (d.status === 'rejected') return 'usage-red'
  if (d.status === 'allowed_warning') return 'usage-yellow'
  const u = d.utilization ?? 0
  if (u >= 0.8) return 'usage-red'
  if (u >= 0.5) return 'usage-yellow'
  return 'usage-green'
}

const USAGE_URL = 'https://claude.ai/settings/usage'

/** Open in a new tab. In an installed PWA `target=_blank` navigates inside the window —
 *  an explicit window.open on a user gesture more reliably opens an external browser. */
function openUsage(e: React.MouseEvent) {
  e.preventDefault()
  window.open(USAGE_URL, '_blank', 'noopener,noreferrer')
}

function fmtPct(u: number | null): string {
  if (u == null) return ''
  return `${Math.round(u * 100)}%`
}

export function UsageBadge({ compact = false }: { compact?: boolean } = {}) {
  const [data, setData] = useState<UsageData | null>(null)
  const [hover, setHover] = useState(false)
  // compact (mobile): tap toggles the full breakdown instead of opening an external link.
  const [expanded, setExpanded] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!expanded) return
    function onOut(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setExpanded(false)
    }
    document.addEventListener('mousedown', onOut)
    return () => document.removeEventListener('mousedown', onOut)
  }, [expanded])

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await api.usage()
        if (!cancelled) setData(res)
      } catch {
        if (!cancelled) setData(null)
      }
    }
    load()
    const id = setInterval(load, 30_000)
    const onFocus = () => load()
    window.addEventListener('focus', onFocus)
    return () => {
      cancelled = true
      clearInterval(id)
      window.removeEventListener('focus', onFocus)
    }
  }, [])

  if (!data) return null

  const fiveH = data.limits.five_hour
  const week  = data.limits.seven_day
  const now   = data.now

  // Nothing from SDK yet — show placeholder
  if (!fiveH && !week) {
    return (
      <a
        className="usage-badge usage-dim"
        href={USAGE_URL}
        target="_blank"
        rel="noopener noreferrer"
        onClick={openUsage}
        title="Limits loading… (click — claude.ai/settings/usage)"
      >
        ⏱ —
      </a>
    )
  }

  // Primary indicator — 5-hour window, otherwise weekly.
  const primary = fiveH ?? week!
  const icon = fiveH ? '⏱' : '📅'
  const pct = fmtPct(primary.utilization)

  const showDropdown = hover || expanded

  return (
    <div
      className={`usage-badge-wrap${compact ? ' usage-compact' : ''}`}
      ref={wrapRef}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      {compact ? (
        // Mobile: a button that toggles the full breakdown (no external navigation).
        <button
          className={`usage-badge ${pickClass(primary)}`}
          onClick={() => setExpanded(e => !e)}
          title="Subscription limits — tap for the full breakdown"
          aria-expanded={expanded}
        >
          <span className="usage-icon">{icon}</span>
          {pct && <span>{pct}</span>}
        </button>
      ) : (
        <a
          className={`usage-badge ${pickClass(primary)}`}
          href={USAGE_URL}
          target="_blank"
          rel="noopener noreferrer"
          onClick={openUsage}
          title="Claude Code subscription limits (click — claude.ai/settings/usage)"
        >
          <span className="usage-icon">{icon}</span>
          {pct && <span>{pct}</span>}
          {pct && <span className="usage-sep">—</span>}
          <span>{fmtReset(primary.resets_at, now)}</span>
        </a>
      )}

      {showDropdown && (
        <div className="usage-dropdown">
          {['five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'overage'].map(k => {
            const d = data.limits[k]
            if (!d) return null
            const label = ({
              five_hour: '5-hour window',
              seven_day: 'Week (all)',
              seven_day_opus: 'Week Opus',
              seven_day_sonnet: 'Week Sonnet',
              overage: 'Overage',
            } as Record<string, string>)[k]
            return (
              <div key={k} className={`usage-row ${pickClass(d)}`}>
                <span className="usage-row-label">{label}</span>
                <span className="usage-row-pct">{fmtPct(d.utilization) || d.status}</span>
                <span className="usage-row-reset">resets {fmtReset(d.resets_at, now)}</span>
              </div>
            )
          })}
          {compact && (
            <a
              className="usage-dropdown-link"
              href={USAGE_URL}
              target="_blank"
              rel="noopener noreferrer"
              onClick={openUsage}
            >
              Open on claude.ai →
            </a>
          )}
        </div>
      )}
    </div>
  )
}
