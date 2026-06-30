import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { fmtReset, pickClass, fmtPct, LIMIT_LABELS, type RawLimit } from './usageFormat'

interface UsageData {
  limits: Record<string, RawLimit>
  now: number
}

const USAGE_URL = 'https://claude.ai/settings/usage'

/** Open in a new tab. In an installed PWA `target=_blank` navigates inside the window —
 *  an explicit window.open on a user gesture more reliably opens an external browser. */
function openUsage(e: React.MouseEvent) {
  e.preventDefault()
  window.open(USAGE_URL, '_blank', 'noopener,noreferrer')
}

export function UsageBadge({ compact = false, onOpen }: { compact?: boolean; onOpen?: () => void } = {}) {
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
          onClick={(e) => { if (onOpen) { e.preventDefault(); onOpen() } else { openUsage(e) } }}
          title={onOpen ? 'Usage & cost dashboard (hover for live limits)' : 'Claude Code subscription limits (click — claude.ai/settings/usage)'}
        >
          <span className="usage-icon">{icon}</span>
          {pct && <span>{pct}</span>}
          {pct && <span className="usage-sep">—</span>}
          <span>{fmtReset(primary.resets_at, now)}</span>
        </a>
      )}

      {showDropdown && (
        <div className="usage-dropdown">
          {/* Headline action: our own usage & cost dashboard — now the badge's primary
              destination (replaces the old claude.ai link). The desktop tab-bar badge
              passes onOpen directly; the mobile composer badge (deep in ChatTab, no
              handler) falls back to a window event App listens for. */}
          <button
            className="usage-dropdown-cta"
            onClick={() => {
              setExpanded(false); setHover(false)
              if (onOpen) onOpen()
              else window.dispatchEvent(new CustomEvent('cops:open-usage'))
            }}
          >
            <span>📊 Usage &amp; cost</span>
            <span className="usage-dropdown-cta-arrow">→</span>
          </button>
          {['five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'overage'].map(k => {
            const d = data.limits[k]
            if (!d) return null
            const label = LIMIT_LABELS[k]
            return (
              <div key={k} className={`usage-row ${pickClass(d)}`}>
                <span className="usage-row-label">{label}</span>
                <span className="usage-row-pct">{fmtPct(d.utilization) || d.status}</span>
                <span className="usage-row-reset">resets {fmtReset(d.resets_at, now)}</span>
              </div>
            )
          })}
          <a
            className="usage-dropdown-claude"
            href={USAGE_URL}
            target="_blank"
            rel="noopener noreferrer"
            onClick={openUsage}
          >
            Limits on claude.ai ↗
          </a>
        </div>
      )}
    </div>
  )
}
